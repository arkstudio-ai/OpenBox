"""Desktop-local, evidence-based cache reclamation.

The protected SQLite journal is outside the writable workspace. Only explicitly
registered, unchanged files are removed. A lease never expires into permission
to delete; pending outputs require a verified OSS receipt. The minute timer does
one statvfs and indexed work, with bounded deletion/upload batches.
"""
from contextlib import contextmanager
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "managed_cache_v1"
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
LOW = .10
TARGET = .20
BATCH = 128
MAX_SECONDS = 10


class CacheError(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise CacheError("INVALID_CACHE_ID", 400)
    return value


def safe_name(value):
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or any(c in value for c in "/\\\0") or len(value.encode()) > 240):
        raise CacheError("INVALID_CACHE_NAME", 400)
    return value


def signature(s):
    return [s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink]


def protected(path):
    """No user-writable ancestor or symbolic link may host the journal."""
    for part in (path, *path.parents):
        info = part.lstat()
        sticky_ancestor = part != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 and not sticky_ancestor:
            raise CacheError("UNPROTECTED_CACHE_JOURNAL", 503)


class CacheStore:
    def __init__(self, path, workspace="/workspace", *, require_root=True, uid=0, gid=0):
        self.path, self.root = Path(path), Path(workspace)
        self.uid, self.gid = uid, gid
        if require_root and os.geteuid() != 0:
            raise CacheError("CACHE_ROOT_AUTHORITY_REQUIRED", 503)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if require_root:
            protected(self.path.parent)
            if self.path.exists() or self.path.is_symlink():
                protected(self.path)
        self.lock_path = self.path.with_suffix(".lock")
        if require_root and (self.lock_path.exists() or self.lock_path.is_symlink()):
            protected(self.lock_path)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, generation INTEGER NOT NULL, run_id TEXT NOT NULL,
                    state TEXT NOT NULL, reserved INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    task_id TEXT, state TEXT NOT NULL, fingerprint TEXT, parents TEXT,
                    size INTEGER NOT NULL DEFAULT 0, oss_key TEXT, md5 TEXT,
                    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
                    catalogued INTEGER NOT NULL DEFAULT 0,
                    put_url TEXT, url_expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pins (
                    task_id TEXT NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY(task_id,entry_id));
                CREATE TABLE IF NOT EXISTS temp_roots (
                    task_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(task_id,run_id));
                CREATE TABLE IF NOT EXISTS pending_roots (task_id TEXT PRIMARY KEY, parents TEXT NOT NULL, scanned INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS cache_reclaim ON entries(state,updated);
                CREATE INDEX IF NOT EXISTS cache_retry ON entries(state,retry_at);
                CREATE INDEX IF NOT EXISTS cache_pins_entry ON pins(entry_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, entry_id TEXT, action TEXT NOT NULL,
                    detail TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS health (id INTEGER PRIMARY KEY CHECK(id=1), pressure INTEGER NOT NULL);
                INSERT OR IGNORE INTO health VALUES (1,0);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self, *, nonblocking=False):
        # One order for API admission, file CAS, and collector work. Never
        # hold this lock during a network transfer or an async yield.
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        db = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
            os.close(fd)

    def space(self):
        s = os.statvfs(self.root)
        total, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
        return {"total": total, "available": free, "available_fraction": free / max(1, total)}

    def pressure(self):
        space = self.space()
        with self.transaction() as db:
            previous = bool(db.execute("SELECT pressure FROM health WHERE id=1").fetchone()[0])
            pressure = space["available_fraction"] < LOW or previous and space["available_fraction"] < TARGET
            if pressure != previous:
                db.execute("UPDATE health SET pressure=? WHERE id=1", (int(pressure),))
        return pressure

    @contextmanager
    def parent(self, relative, *, create=False):
        path = PurePosixPath(relative)
        if (path.is_absolute() or not path.parts or any(p in {"", ".", ".."} for p in path.parts)
                or str(path) != relative or path.parts[:2] not in {(".openbox", "cache"), (".openbox", "pending")}):
            raise CacheError("PATH_OUTSIDE_MANAGED_CACHE", 400)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parents = [list(signature(os.fstat(fd))[:2])]
        device = parents[0][0]
        try:
            for part in path.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_dev != device:
                    raise CacheError("CACHE_MOUNT_CHANGED")
                parents.append([info.st_dev, info.st_ino])
            yield fd, path.name, parents
        finally:
            os.close(fd)

    def file_evidence(self, relative):
        with self.parent(relative) as (fd, name, parents):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CacheError("CACHE_FILE_NOT_EXCLUSIVE")
            return json.dumps(signature(info)), json.dumps(parents), info.st_size

    def _task(self, db, task, generation, run_id, *, active=True):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (safe_id(task),)).fetchone()
        if (row is None or (row["generation"], row["run_id"]) != (generation, run_id)
                or active and row["state"] != "active"):
            raise CacheError("CACHE_TASK_FENCE_CHANGED")
        return row

    def begin(self, task, generation, run_id, reserve=0):
        safe_id(task); safe_id(run_id)
        if type(generation) is not int or generation < 1 or type(reserve) is not int or reserve < 0:
            raise CacheError("INVALID_CACHE_RESERVATION", 400)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
            if old and (generation < old["generation"] or generation == old["generation"]
                        and (run_id != old["run_id"] or old["state"] != "active")):
                raise CacheError("CACHE_TASK_FENCE_CHANGED")
            space = self.space()
            others = db.execute("SELECT coalesce(sum(reserved),0) FROM tasks WHERE state='active' AND id!=?", (task,)).fetchone()[0]
            reserve = max(reserve, old["reserved"] if old and old["state"] == "active" else 0)
            if (not old or reserve > old["reserved"] or old["state"] != "active") and (
                    space["available"] - others - reserve < space["total"] * LOW):
                raise CacheError("DESKTOP_LOW_SPACE", 507)
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "generation=excluded.generation,run_id=excluded.run_id,state='active',reserved=excluded.reserved,updated=excluded.updated",
                       (task, generation, run_id, "active", reserve, time.time()))
            relative = f".openbox/cache/tasks/{task}/{run_id}/tmp/.anchor"
            with self.parent(relative, create=True) as (fd, _, __):
                os.fchown(fd, self.uid, self.gid)
            db.execute("INSERT OR IGNORE INTO temp_roots VALUES (?,?,'active',?)", (task, run_id, time.time()))
            incoming = f".openbox/pending/{task}/incoming/.anchor"
            with self.parent(incoming, create=True) as (fd, _, parents):
                previous = db.execute("SELECT parents FROM pending_roots WHERE task_id=?", (task,)).fetchone()
                if previous and previous[0] != json.dumps(parents):
                    raise CacheError("PENDING_DIRECTORY_CHANGED")
                os.fchown(fd, self.uid, self.gid)
                db.execute("INSERT OR IGNORE INTO pending_roots(task_id,parents) VALUES (?,?)", (task, json.dumps(parents)))
                db.execute("UPDATE pending_roots SET scanned=0 WHERE task_id=?", (task,))
            incoming = f".openbox/pending/{task}/incoming/{run_id}/.anchor"
            with self.parent(incoming, create=True) as (fd, _, __):
                os.fchown(fd, self.uid, self.gid)
            return {"task_id": task, "tmpdir": str(self.root / PurePosixPath(relative).parent),
                    "pending_dir": str(self.root / PurePosixPath(incoming).parent)}

    def claim_asset(self, task, generation, run_id, asset_id, name, size, oss_key):
        safe_id(asset_id); safe_name(name)
        relative = f".openbox/cache/assets/{asset_id}/{name}"
        entry_id = "asset_" + asset_id
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            entry = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if entry and (entry["path"] != relative or entry["oss_key"] != oss_key or entry["size"] != size):
                raise CacheError("CACHE_ASSET_IDENTITY_CHANGED")
            with self.parent(relative, create=True):
                pass  # Asset directories stay service-owned; consumers only read.
            ready = False
            if entry and entry["state"] == "ready":
                try:
                    evidence = self.file_evidence(relative)
                    ready = evidence[:2] == (entry["fingerprint"], entry["parents"])
                except (FileNotFoundError, CacheError):
                    pass
            if not ready:
                available = self.space()
                reserved = db.execute("SELECT coalesce(sum(reserved),0) FROM tasks WHERE state='active'").fetchone()[0]
                if type(size) is not int or size < 0 or available["available"] - reserved - size < available["total"] * LOW:
                    raise CacheError("DESKTOP_LOW_SPACE", 507)
                if entry and entry["state"] not in {"deleted", "downloading"}:
                    # Modified cache files may be a user's sole new output.
                    # Preserve them rather than overwriting by asset name.
                    if (self.root / relative).exists():
                        raise CacheError("CACHE_ASSET_MODIFIED")
                db.execute("INSERT INTO entries(id,path,kind,state,size,oss_key,updated) VALUES (?,?,?,'downloading',?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET state='downloading',updated=excluded.updated",
                           (entry_id, relative, "asset", size, oss_key, time.time()))
            db.execute("INSERT OR IGNORE INTO pins VALUES (?,?)", (task, entry_id))
            return {"entry_id": entry_id, "path": str(self.root / relative), "ready": ready}

    def fetch_asset(self, task, generation, run_id, entry_id, url):
        parsed = urllib.parse.urlsplit(url)
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            row = db.execute("SELECT e.* FROM entries e JOIN pins p ON p.entry_id=e.id WHERE e.id=? AND p.task_id=?",
                             (entry_id, task)).fetchone()
            if not row or row["kind"] != "asset":
                raise CacheError("CACHE_ASSET_NOT_CLAIMED")
            row = dict(row)
            if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".aliyuncs.com")
                    or urllib.parse.unquote(parsed.path).lstrip("/") != row["oss_key"]):
                raise CacheError("CACHE_ASSET_URL_CHANGED", 400)
            if row["state"] == "ready" and self.file_evidence(row["path"])[:2] == (row["fingerprint"], row["parents"]):
                return {"ready": True, "path": str(self.root / row["path"])}
            space = self.space()
            reserved = db.execute("SELECT coalesce(sum(reserved),0) FROM tasks WHERE state='active'").fetchone()[0]
            if space["available"] - reserved - row["size"] < space["total"] * LOW:
                raise CacheError("DESKTOP_LOW_SPACE", 507)
            db.execute("UPDATE tasks SET reserved=reserved+? WHERE id=?", (row["size"], task))
        try:
            with self.parent(row["path"]) as (directory, name, parents):
                temporary = "." + entry_id + "-" + secrets.token_hex(12) + ".part"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                temporary_identity = signature(os.fstat(fd))
                temporary_id = "download_" + secrets.token_hex(16)
                temporary_path = str(PurePosixPath(row["path"]).parent / temporary)
                with self.transaction() as db:
                    db.execute("INSERT INTO entries(id,path,kind,task_id,state,fingerprint,parents,size,updated) "
                               "VALUES (?,?,'download',?,'downloading_temp',?,?,?,?)",
                               (temporary_id, temporary_path, task, json.dumps(temporary_identity), json.dumps(parents), row["size"], time.time()))
                try:
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *args, **kwargs):
                            return None
                    with os.fdopen(fd, "wb") as out, urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(url, timeout=60) as source:
                        size = 0
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(block)
                            if size > row["size"]:
                                raise CacheError("CACHE_ASSET_SIZE_MISMATCH")
                            out.write(block)
                        if size != row["size"]:
                            raise CacheError("CACHE_ASSET_SIZE_MISMATCH")
                        out.flush(); os.fsync(out.fileno())
                        os.fchown(out.fileno(), self.uid, self.gid)
                        os.fchmod(out.fileno(), 0o644)
                    with self.transaction() as db:
                        self._task(db, task, generation, run_id)
                        with self.parent(row["path"]) as (_, __, actual):
                            if actual != parents:
                                raise CacheError("CACHE_PATH_CHANGED")
                        # Exclusive publication: never overwrite a replacement
                        # output or a concurrent downloader's final file.
                        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                        os.unlink(temporary, dir_fd=directory)
                        temporary = None
                        fingerprint, actual, size = self.file_evidence(row["path"])
                        db.execute("UPDATE entries SET state='ready',fingerprint=?,parents=?,updated=? WHERE id=?",
                                   (fingerprint, actual, time.time(), entry_id))
                        db.execute("UPDATE entries SET state='deleted',updated=? WHERE id=?", (time.time(), temporary_id))
                finally:
                    if temporary is not None:
                        current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                        with self.parent(row["path"]) as (_, __, actual):
                            if actual != parents or signature(current)[:3] != temporary_identity[:3] or current.st_nlink != 1:
                                raise CacheError("DOWNLOAD_TEMP_IDENTITY_CHANGED")
                        os.unlink(temporary, dir_fd=directory)
                        with self.transaction() as db:
                            db.execute("UPDATE entries SET state='deleted',updated=? WHERE id=?", (time.time(), temporary_id))
            return {"ready": True, "path": str(self.root / row["path"])}
        finally:
            with self.transaction() as db:
                db.execute("UPDATE tasks SET reserved=max(0,reserved-?) WHERE id=? AND generation=? AND run_id=?",
                           (row["size"], task, generation, run_id))

    def asset_ready(self, task, generation, run_id, entry_id):
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            row = db.execute("SELECT e.* FROM entries e JOIN pins p ON p.entry_id=e.id WHERE e.id=? AND p.task_id=?", (entry_id, task)).fetchone()
            if not row or row["kind"] != "asset":
                raise CacheError("CACHE_ASSET_NOT_CLAIMED")
            fingerprint, parents, size = self.file_evidence(row["path"])
            if size != row["size"]:
                raise CacheError("CACHE_ASSET_SIZE_MISMATCH")
            db.execute("UPDATE entries SET state='ready',fingerprint=?,parents=?,updated=? WHERE id=?",
                       (fingerprint, parents, time.time(), entry_id))
            return {"ready": True}

    def release(self, task, generation, run_id):
        with self.transaction() as db:
            self._task(db, task, generation, run_id, active=False)
            # Background processes inherit this identity, including detached
            # children. Expiration of a backend lease is never enough.
            if self.task_processes(task):
                return {"released": False, "reason": "background_processes"}
            db.execute("UPDATE tasks SET state='released',reserved=0,updated=? WHERE id=?", (time.time(), task))
            db.execute("DELETE FROM pins WHERE task_id=?", (task,))
            db.execute("UPDATE temp_roots SET state='released',updated=? WHERE task_id=?", (time.time(), task))
            return {"released": True}

    def index_released_temp(self):
        """Only released task TMPDIRs, never projects or the pending tree.

        Directory traversal and registration are bounded; the next tick resumes
        remaining files. Existing entries are not rebound to replacement bytes.
        """
        started, registered = time.monotonic(), 0
        with self.transaction() as db:
            tasks = db.execute("SELECT task_id,run_id FROM temp_roots WHERE state='released' ORDER BY updated LIMIT 16").fetchall()
            for root_row in tasks:
                task = root_row["task_id"]
                if self.task_processes(task):
                    continue
                root = self.root / f".openbox/cache/tasks/{task}/{root_row['run_id']}/tmp"
                db.execute("UPDATE temp_roots SET updated=? WHERE task_id=? AND run_id=?", (time.time(), task, root_row["run_id"]))
                try:
                    with self.parent(str(root.relative_to(self.root)) + "/.anchor"):
                        pass
                except FileNotFoundError:
                    continue
                for base, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = [name for name in sorted(dirs) if not (Path(base) / name).is_symlink()]
                    for name in sorted(names):
                        if registered >= BATCH or time.monotonic() - started > 2:
                            return registered
                        relative = (Path(base) / name).relative_to(self.root).as_posix()
                        entry_id = "temp_" + hashlib.sha256(relative.encode()).hexdigest()
                        if db.execute("SELECT 1 FROM entries WHERE path=?", (relative,)).fetchone():
                            continue
                        try:
                            fingerprint, parents, size = self.file_evidence(relative)
                        except (OSError, CacheError):
                            continue
                        db.execute("INSERT INTO entries(id,path,kind,task_id,state,fingerprint,parents,size,updated) "
                                   "VALUES (?,?, 'temp',?,'ephemeral',?,?,?,?)",
                                   (entry_id, relative, task, fingerprint, parents, size, time.time()))
                        registered += 1
                db.execute("UPDATE temp_roots SET state='indexed' WHERE task_id=? AND run_id=?", (task, root_row["run_id"]))
        return registered

    def prepare_pending(self, task, generation, run_id, entry_id, name, oss_key, size, mime="application/octet-stream"):
        safe_id(entry_id); safe_name(name)
        relative = f".openbox/pending/{safe_id(task)}/{entry_id}/{name}"
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if row and (row["path"], row["oss_key"], row["size"]) != (relative, oss_key, size):
                raise CacheError("PENDING_IDENTITY_CHANGED")
            with self.parent(relative, create=True) as (fd, _, __):
                os.fchown(fd, self.uid, self.gid)
                if row is None:
                    file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    try:
                        os.fchown(file_fd, self.uid, self.gid)
                    finally:
                        os.close(file_fd)
            fingerprint, parents, _ = self.file_evidence(relative)
            db.execute("INSERT OR IGNORE INTO entries(id,path,kind,task_id,state,size,oss_key,mime,updated) "
                       "VALUES (?,?, 'pending',?,'writing',?,?,?,?)", (entry_id, relative, task, size, oss_key, mime, time.time()))
            if row is None:
                db.execute("UPDATE entries SET fingerprint=?,parents=? WHERE id=?", (fingerprint, parents, entry_id))
            else:
                fingerprint, parents = row["fingerprint"], row["parents"]
            db.execute("INSERT OR IGNORE INTO pins VALUES (?,?)", (task, entry_id))
        return {"entry_id": entry_id, "path": str(self.root / relative),
                "fingerprint": json.loads(fingerprint), "parents": json.loads(parents)}

    def adopt_pending(self, task, generation, run_id, entry_id, relative, size, oss_key, mime):
        """Register a completed incoming output in place, without a second copy."""
        safe_id(entry_id)
        prefix = f".openbox/pending/{safe_id(task)}/incoming/{safe_id(run_id)}/"
        if not isinstance(relative, str) or not relative.startswith(prefix):
            raise CacheError("PENDING_NOT_OWNED")
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            fingerprint, parents, actual_size = self.file_evidence(relative)
            if size != actual_size:
                raise CacheError("PENDING_IDENTITY_CHANGED")
            row = db.execute("SELECT * FROM entries WHERE id=? OR path=?", (entry_id, relative)).fetchone()
            if row:
                if (row["id"], row["path"], row["fingerprint"], row["oss_key"]) != (entry_id, relative, fingerprint, oss_key):
                    raise CacheError("PENDING_IDENTITY_CHANGED")
            else:
                db.execute("INSERT INTO entries(id,path,kind,task_id,state,fingerprint,parents,size,oss_key,mime,updated) "
                           "VALUES (?,?,'pending',?,'pending',?,?,?,?,?,?)",
                           (entry_id, relative, task, fingerprint, parents, size, oss_key, mime, time.time()))
            db.execute("INSERT OR IGNORE INTO pins VALUES (?,?)", (task, entry_id))
        return {"entry_id": entry_id, "path": str(self.root / relative), "size": size}

    @staticmethod
    def task_processes(task):
        marker = ("OPENBOX_CACHE_TASK=" + task).encode()
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if marker in (proc / "environ").read_bytes().split(b"\0"):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                return True  # Cannot prove quiescence.
        return False

    @staticmethod
    def open_file(device, inode):
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                for fd in (proc / "fd").iterdir():
                    try:
                        s = fd.stat()
                        if (s.st_dev, s.st_ino) == (device, inode):
                            return True
                    except (FileNotFoundError, ProcessLookupError):
                        continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                return True
        return False

    def register_pending(self, task, generation, run_id, entry_id, name, oss_key):
        """Register an already completed file in pending; no arbitrary source rm."""
        safe_id(entry_id); safe_name(name)
        relative = f".openbox/pending/{safe_id(task)}/{entry_id}/{name}"
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            fingerprint, parents, size = self.file_evidence(relative)
            prior = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if prior:
                if (prior["path"], prior["oss_key"], prior["size"]) != (relative, oss_key, size):
                    raise CacheError("PENDING_IDENTITY_CHANGED")
                if prior["state"] == "writing":
                    if json.loads(prior["fingerprint"])[:2] != json.loads(fingerprint)[:2] or prior["parents"] != parents:
                        raise CacheError("PENDING_IDENTITY_CHANGED")
                    db.execute("UPDATE entries SET state='pending',fingerprint=?,parents=?,updated=? WHERE id=?",
                               (fingerprint, parents, time.time(), entry_id))
                elif prior["fingerprint"] != fingerprint:
                    raise CacheError("PENDING_IDENTITY_CHANGED")
                return {"entry_id": entry_id, "path": str(self.root / relative), "size": size}
            db.execute("INSERT INTO entries(id,path,kind,task_id,state,fingerprint,parents,size,oss_key,updated) "
                       "VALUES (?,?, 'pending',?,'pending',?,?,?,?,?)",
                       (entry_id, relative, task, fingerprint, parents, size, oss_key, time.time()))
            db.execute("INSERT OR IGNORE INTO pins VALUES (?,?)", (task, entry_id))
            return {"entry_id": entry_id, "path": str(self.root / relative), "size": size}

    def recover_terminal(self, task, generation, run_id):
        """Called only with a DB-proven terminal task, never on a TTL alone."""
        released = self.release(task, generation, run_id)
        if not released["released"]:
            return released
        with self.transaction() as db:
            # A new generation between release and this lock cancels recovery.
            self._task(db, task, generation, run_id, active=False)
            for row in db.execute("SELECT * FROM entries WHERE task_id=? AND state IN ('writing','downloading_temp') LIMIT 128", (task,)).fetchall():
                try:
                    fingerprint, parents, size = self.file_evidence(row["path"])
                    before, after = json.loads(row["fingerprint"]), json.loads(fingerprint)
                    if before[:3] != after[:3] or row["parents"] != parents or self.open_file(after[0], after[1]):
                        continue
                    if row["kind"] == "download":
                        state, error = "ephemeral", "terminal_interrupted_download"
                    elif size == row["size"]:
                        state, error = "pending", "recovered_completed_copy"
                    elif size < row["size"]:
                        state, error = "ephemeral", "terminal_incomplete_copy"
                    else:
                        state, error = "quarantined", "unexpected_size"
                    db.execute("UPDATE entries SET state=?,error=?,fingerprint=?,updated=? WHERE id=?",
                               (state, error, fingerprint, time.time(), row["id"]))
                except (OSError, CacheError):
                    continue
        self.index_released_temp()
        self.index_incoming(task)
        return {**released, "collection": self.collect(immediate=True)}

    def index_incoming(self, task):
        """Recover outputs created before share_file could register an asset.

        No completeness guess is made here: even a partially rendered file is
        archived for recovery before its local copy can be reclaimed.
        """
        root = self.root / f".openbox/pending/{safe_id(task)}/incoming"
        started, count = time.monotonic(), 0
        with self.transaction() as db:
            owner = db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()
            expected = db.execute("SELECT parents FROM pending_roots WHERE task_id=?", (task,)).fetchone()
            if not owner or owner[0] != "released" or not expected or self.task_processes(task):
                return 0
            with self.parent(str(root.relative_to(self.root)) + "/.anchor") as (_, __, parents):
                if expected[0] != json.dumps(parents):
                    raise CacheError("PENDING_DIRECTORY_CHANGED")
            for base, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in sorted(dirs) if not (Path(base) / name).is_symlink()]
                for name in sorted(files):
                    if count >= BATCH or time.monotonic() - started >= 2:
                        return count
                    relative = (Path(base) / name).relative_to(self.root).as_posix()
                    if db.execute("SELECT 1 FROM entries WHERE path=?", (relative,)).fetchone():
                        continue
                    try:
                        fingerprint, parents, size = self.file_evidence(relative)
                    except (OSError, CacheError):
                        continue
                    entry_id = "recovery_" + hashlib.sha256((str(self.path) + task + relative + fingerprint).encode()).hexdigest()[:40]
                    db.execute("INSERT INTO entries(id,path,kind,task_id,state,fingerprint,parents,size,updated) "
                               "VALUES (?,?, 'pending',?,'discovered',?,?,?,?)",
                               (entry_id, relative, task, fingerprint, parents, size, time.time()))
                    count += 1
            db.execute("UPDATE pending_roots SET scanned=1 WHERE task_id=?", (task,))
        return count

    def assign_recovery(self, entry_id, oss_key, mime):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM entries WHERE id=? AND kind='pending'", (entry_id,)).fetchone()
            if not row or row["state"] not in {"discovered", "pending"}:
                raise CacheError("RECOVERY_NOT_DISCOVERED")
            if row["oss_key"] and row["oss_key"] != oss_key:
                raise CacheError("RECOVERY_OBJECT_CHANGED")
            if not isinstance(oss_key, str) or not oss_key.startswith("recovery/") or "/" + entry_id + "/" not in oss_key:
                raise CacheError("INVALID_RECOVERY_KEY", 400)
            db.execute("UPDATE entries SET oss_key=?,mime=?,state='pending',updated=? WHERE id=?", (oss_key, mime, time.time(), entry_id))
        return {"assigned": True}

    def authorize_upload(self, entry_id, url, expires):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.hostname.endswith(".aliyuncs.com")):
            raise CacheError("INVALID_RECOVERY_DESTINATION", 400)
        if not time.time() < expires <= time.time() + 3600:
            raise CacheError("INVALID_RECOVERY_EXPIRY", 400)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row or row["kind"] != "pending" or row["state"] not in {"pending", "uploading"}:
                raise CacheError("PENDING_NOT_UPLOADABLE")
            if urllib.parse.unquote(parsed.path).lstrip("/") != row["oss_key"]:
                raise CacheError("PENDING_OBJECT_KEY_CHANGED")
            db.execute("UPDATE entries SET put_url=?,url_expires=?,updated=? WHERE id=?", (url, expires, time.time(), entry_id))
            return {"scheduled": True}

    def retry_one(self, entry_id=None):
        """Same immutable bytes/key can retry a lost PUT acknowledgement safely."""
        if entry_id is None:
            with self.transaction() as db:
                row = db.execute("SELECT id FROM entries WHERE kind='pending' AND state IN ('pending','uploading') "
                                 "AND retry_at<=? AND url_expires>? ORDER BY retry_at LIMIT 1",
                                 (time.time(), time.time() + 35)).fetchone()
            if row is None:
                return None
            entry_id = row[0]
        safe_id(entry_id)
        fd = os.open(str(self.path) + ".upload-" + entry_id, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._retry_one(entry_id)
        except BlockingIOError:
            return None
        finally:
            os.close(fd)

    def _retry_one(self, entry_id):
        now = time.time()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM entries WHERE kind='pending' AND state IN ('pending','uploading') "
                             "AND retry_at<=? AND url_expires>? AND (? IS NULL OR id=?) ORDER BY retry_at LIMIT 1",
                             (now, now + 35, entry_id, entry_id)).fetchone()
            if not row:
                return None
            row = dict(row)
            try:
                evidence = self.file_evidence(row["path"])[:2]
                problem = None if evidence == (row["fingerprint"], row["parents"]) else "file_changed"
            except FileNotFoundError:
                problem = "file_missing"
            except (OSError, CacheError):
                problem = "unsafe_path"
            if problem:
                # One missing or replaced file must not occupy the first retry
                # slot forever. Preserve its audit identity and advance the queue.
                db.execute("UPDATE entries SET state='quarantined',error=?,put_url=NULL,updated=? WHERE id=?",
                           (problem, now, row["id"]))
                return {"entry_id": row["id"], "state": "quarantined"}
            db.execute("UPDATE entries SET state='uploading',retry_at=?,attempts=attempts+1 WHERE id=?", (now + 120, row["id"]))
        # Bind an fd through no-follow ancestors; do not reopen a mutable path.
        try:
            with self.parent(row["path"]) as (fd, name, parents):
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(handle, "rb") as source:
                if json.dumps(signature(os.fstat(source.fileno()))) != row["fingerprint"] or json.dumps(parents) != row["parents"]:
                    raise CacheError("PENDING_IDENTITY_CHANGED")
                digest = hashlib.md5()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                source.seek(0)
                request = urllib.request.Request(row["put_url"], data=source, method="PUT", headers={
                    "Content-Length": str(row["size"]), "Content-Type": row["mime"],
                })
                # urllib's default redirect policy doesn't follow PUT, but
                # make that invariant explicit for signed credential URLs.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None
                with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=30) as response:
                    etag = response.headers.get("ETag", "").strip('"').lower()
                    if response.status != 200 or etag != digest.hexdigest():
                        raise CacheError("OSS_UPLOAD_RECEIPT_MISMATCH")
                if json.dumps(signature(os.fstat(source.fileno()))) != row["fingerprint"]:
                    raise CacheError("PENDING_CHANGED_DURING_UPLOAD")
            with self.transaction() as db:
                db.execute("UPDATE entries SET state='archived',md5=?,put_url=NULL,error=NULL,updated=? WHERE id=? AND fingerprint=?",
                           (etag, time.time(), row["id"], row["fingerprint"]))
            return {"entry_id": row["id"], "state": "archived", "size": row["size"]}
        except Exception as exc:
            # Exception messages can contain signed URLs. Store only a code.
            error = exc.code if isinstance(exc, CacheError) else type(exc).__name__
            with self.transaction() as db:
                db.execute("UPDATE entries SET state='pending',error=?,retry_at=?,updated=? WHERE id=?",
                           (str(error)[:80], now + min(3600, 30 * 2 ** min(row["attempts"], 7)), time.time(), row["id"]))
            return {"entry_id": row["id"], "state": "pending", "error": str(error)[:80]}

    def upload_pending(self, task, generation, run_id, entry_id, url, expires):
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            row = db.execute("SELECT * FROM entries WHERE id=? AND task_id=?", (entry_id, task)).fetchone()
            if not row:
                raise CacheError("PENDING_NOT_OWNED")
            if row["state"] == "archived":
                return {"entry_id": entry_id, "state": "archived", "size": row["size"]}
        self.authorize_upload(entry_id, url, expires)
        return self.retry_one(entry_id) or {"entry_id": entry_id, "state": "pending"}

    def acknowledge_archive(self, entry_id, size, etag):
        """Backend HEAD confirmation; it must match the local PUT receipt."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row or row["state"] not in {"archived", "deleted"} or (row["size"], row["md5"]) != (size, etag):
                raise CacheError("PENDING_ARCHIVE_NOT_VERIFIED")
            db.execute("UPDATE entries SET catalogued=1 WHERE id=?", (entry_id,))
        try:
            collection = self.collect(immediate=True)
        except BlockingIOError:
            collection = {"deferred": True}
        return {"archived": True, "collection": collection}

    def acknowledge_task_archive(self, task, generation, run_id, entry_id, size, etag):
        with self.transaction() as db:
            self._task(db, task, generation, run_id)
            if not db.execute("SELECT 1 FROM entries WHERE id=? AND task_id=?", (entry_id, task)).fetchone():
                raise CacheError("PENDING_NOT_OWNED")
        return self.acknowledge_archive(entry_id, size, etag)

    def collect(self, *, immediate=False):
        start, removed, freed = time.monotonic(), 0, 0
        space = self.space()
        if not immediate and not self.pressure():
            return {"removed": 0, "space": space}
        with self.transaction(nonblocking=True) as db:
            rows = db.execute("SELECT e.* FROM entries e WHERE (state IN ('ready','ephemeral') OR state='archived' AND catalogued=1) "
                              "AND NOT EXISTS(SELECT 1 FROM pins p WHERE p.entry_id=e.id) ORDER BY updated LIMIT ?", (BATCH,)).fetchall()
            for row in rows:
                if time.monotonic() - start >= MAX_SECONDS:
                    break
                if row["task_id"] and self.task_processes(row["task_id"]):
                    continue
                try:
                    with self.parent(row["path"]) as (fd, name, parents):
                        s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if json.dumps(signature(s)) != row["fingerprint"] or json.dumps(parents) != row["parents"]:
                            db.execute("UPDATE entries SET state='quarantined',error='file_changed' WHERE id=?", (row["id"],))
                            continue
                        if self.open_file(s.st_dev, s.st_ino):
                            continue
                        os.unlink(name, dir_fd=fd)
                        removed += 1
                        freed += s.st_blocks * 512
                except FileNotFoundError:
                    pass
                except OSError:
                    db.execute("UPDATE entries SET state='quarantined',error='unsafe_path' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE entries SET state='deleted',put_url=NULL,updated=? WHERE id=?", (time.time(), row["id"]))
                db.execute("INSERT INTO events(entry_id,action,detail,created) VALUES (?,'reclaimed',?,?)",
                           (row["id"], json.dumps({"path": row["path"], "fingerprint": row["fingerprint"]}), time.time()))
                if not immediate and self.space()["available_fraction"] >= TARGET:
                    break
            db.execute("DELETE FROM events WHERE id < (SELECT coalesce(max(id),0)-1000 FROM events)")
        return {"removed": removed, "freed": freed, "space": self.space()}

    def status(self):
        with self.transaction() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM entries GROUP BY state").fetchall())
            pending = [dict(r) for r in db.execute("SELECT id,task_id,path,state,size,oss_key,md5,mime,attempts,retry_at,error,updated "
                                                  "FROM entries WHERE kind='pending' AND (state IN ('discovered','pending','uploading') "
                                                  "OR state='archived' AND catalogued=0) ORDER BY updated LIMIT 128")]
            tasks = [dict(r) for r in db.execute("SELECT id,generation,run_id,state,updated FROM tasks WHERE state='active' "
                                                "OR EXISTS(SELECT 1 FROM pending_roots r WHERE r.task_id=tasks.id AND r.scanned=0) "
                                                "OR EXISTS(SELECT 1 FROM entries e WHERE e.task_id=tasks.id AND e.state IN ('writing','downloading_temp')) ORDER BY updated LIMIT 128")]
        return {"protocol": PROTOCOL, "space": self.space(), "counts": counts, "pending": pending, "tasks": tasks}

    def has_released_work(self):
        with self.transaction() as db:
            return bool(db.execute("SELECT 1 FROM temp_roots WHERE state='released' LIMIT 1").fetchone() or
                        db.execute("SELECT 1 FROM entries e WHERE (state IN ('ready','ephemeral') OR state='archived' AND catalogued=1) "
                                   "AND NOT EXISTS(SELECT 1 FROM pins p WHERE p.entry_id=e.id) LIMIT 1").fetchone())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["tick", "recover", "status"])
    parser.add_argument("--db", default="/var/lib/openbox-cache/control.sqlite3")
    args = parser.parse_args()
    store = CacheStore(args.db)
    if args.command == "status":
        print(json.dumps(store.status()))
        return
    tick_fd = os.open(str(store.path) + "." + args.command, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(tick_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "recover":
            retry = store.retry_one()
            if retry:
                print(json.dumps(retry))
        else:
            # Above the threshold this remains a statvfs-only filesystem read;
            # the SQLite status work is independent of workspace file count.
            released_work = store.has_released_work()
            if store.pressure() or released_work:
                store.index_released_temp()
            result = store.collect(immediate=released_work)
            if result["removed"] or result["space"]["available_fraction"] < LOW:
                print(json.dumps(result))
    except BlockingIOError:
        pass
    finally:
        os.close(tick_fd)


if __name__ == "__main__":
    main()
