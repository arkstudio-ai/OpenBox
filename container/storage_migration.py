"""Offline, in-place ownership migration for existing Action Server volumes.

Stop every service/container using both volumes before invoking this utility.
The explicit offline flag is an operator assertion, not proof of remote effect
drainage. Process checks and directory locks reject common accidental live use.
Nothing here grants control, settles effects, creates a replacement journal,
or deletes user data. The caller must pin the existing journal identity.
"""
import argparse
from contextlib import ExitStack, closing, contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
import stat
import sys


VERSION = "isolated_storage_v1"
CONTROL = "openbox-control"
JOURNAL = "control.sqlite3"
CHECKPOINT = "storage-migration.json"


class MigrationError(RuntimeError):
    pass


def _directory(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


@contextmanager
def _root(path):
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts or target == Path("/"):
        raise MigrationError("Volume roots must be absolute and canonical")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(target.parts[1:], 1):
            parent = os.fstat(fd)
            if parent.st_uid != 0 or (parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX):
                raise MigrationError("Volume root has an unprotected ancestor")
            child = _directory(fd, component)
            if parent.st_mode & stat.S_ISVTX and os.fstat(child).st_uid != 0 and index != len(target.parts) - 1:
                os.close(child)
                raise MigrationError("A volume ancestor can be replaced")
            os.close(fd)
            fd = child
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("Another storage migration holds this volume") from exc
        yield fd
    finally:
        os.close(fd)


def _mounts_below(roots):
    # A nested bind mount may have the same st_dev; checking only device IDs
    # would let ownership changes escape the two approved volume roots.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        raw = line.split()[4]
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), raw))
        if any(mount != root and mount.is_relative_to(root) for root in roots):
            raise MigrationError("Nested mounts must be detached before migration")


def _check_processes(uids):
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            status = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines())
            command = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if uids.intersection(map(int, status["Uid"].split())) or any(
            Path(os.fsdecode(arg)).name.removesuffix(".py").split(":")[0]
            in {"action_server", "file_worker", "execution_identity"}
            for arg in command if arg
        ):
            raise MigrationError("Stop executor and Action Server processes before migration")


def _walk(fd, prefix=(), *, skip_control=False):
    nodes = []
    device = os.fstat(fd).st_dev
    for name in sorted(os.listdir(fd)):
        if skip_control and name == CONTROL:
            continue
        item = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if item.st_dev != device:
            raise MigrationError("Nested filesystems are outside the migration scope")
        if not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)):
            raise MigrationError("Remove live sockets/devices/FIFOs from the offline volume first")
        path = (*prefix, name)
        nodes.append((path, item))
        if stat.S_ISDIR(item.st_mode):
            child = _directory(fd, name)
            try:
                nodes.extend(_walk(child, path))
            finally:
                os.close(child)
    return nodes


def _hardlinks(trees):
    links = {}
    for nodes in trees:
        for _, item in nodes:
            if not stat.S_ISDIR(item.st_mode):
                key = (item.st_dev, item.st_ino)
                count, expected = links.get(key, (0, item.st_nlink))
                links[key] = (count + 1, expected)
    if any(count != expected for count, expected in links.values()):
        raise MigrationError("A user file has hard links outside the migration scope")


def _control_files(fd):
    for name in os.listdir(fd):
        item = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_dev != os.fstat(fd).st_dev:
            raise MigrationError("Control storage contains a link, special file or nested mount")


def _journal(path, expected):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MigrationError("The existing journal must be an independent regular file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise MigrationError("Existing journal integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"identity", "control", "operations"}:
            raise MigrationError("Unsupported existing journal schema")
        if db.execute("SELECT singleton,journal_id FROM identity").fetchall() != [(1, expected)]:
            raise MigrationError("Existing journal identity does not match the pinned identity")
        digest = hashlib.sha256()
        for table, order in (("identity", "singleton"), ("control", "singleton"), ("operations", "id")):
            digest.update(table.encode() + b"\0")
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
        return {"journal_id": expected, "snapshot_sha256": digest.hexdigest(),
                "operation_count": db.execute("SELECT count(*) FROM operations").fetchone()[0],
                "blocking_count": db.execute("SELECT count(*) FROM operations WHERE state NOT IN ('completed','canceled')").fetchone()[0]}


def _read_checkpoint(fd):
    try:
        file = os.open(CHECKPOINT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file, "r") as stream:
        item = os.fstat(stream.fileno())
        if not stat.S_ISREG(item.st_mode) or item.st_size > 16384 or item.st_nlink != 1:
            raise MigrationError("Invalid migration checkpoint")
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("version") != VERSION or value.get("state") not in {"preparing", "ready"}:
        raise MigrationError("Unsupported migration checkpoint")
    return value


def _checkpoint(fd, value):
    name = ".storage-migration-" + secrets.token_hex(12)
    file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(file, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, CHECKPOINT, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def _handoff(fd, nodes, uid, gid):
    for path, before in nodes:
        parent = os.dup(fd)
        try:
            for component in path[:-1]:
                child = _directory(parent, component)
                os.close(parent)
                parent = child
            current = os.stat(path[-1], dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode), current.st_nlink) != (
                before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode), before.st_nlink
            ):
                raise MigrationError("A volume entry changed during offline migration")
            if stat.S_ISLNK(current.st_mode):
                link = os.open(path[-1], os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    opened = os.fstat(link)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise MigrationError("A symlink was replaced during migration")
                    libc = ctypes.CDLL(None, use_errno=True)
                    libc.fchownat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
                    # AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW binds the change to
                    # the opened link inode, never its target or a replacement.
                    if libc.fchownat(link, b"", uid, gid, 0x1000 | 0x100) != 0:
                        raise OSError(ctypes.get_errno(), "Cannot migrate symlink ownership")
                    os.fsync(parent)
                finally:
                    os.close(link)
                continue
            item = os.open(path[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                after = os.fstat(item)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise MigrationError("A volume entry was replaced")
                os.fchown(item, uid, gid)
                os.fchmod(item, (before.st_mode & 0o777) | (0o700 if stat.S_ISDIR(before.st_mode) else 0o600))
                os.fsync(item)
            finally:
                os.close(item)
        finally:
            os.close(parent)


def require_ready(journal_path, executor):
    """An interrupted ownership migration must not become a running service.

    Call only after protect_path has validated the journal ancestry. A fresh
    protected volume can have no checkpoint; once present it is mandatory.
    Ready journals can acquire new operation records without another migration.
    """
    path = Path(journal_path)
    control = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        checkpoint = _read_checkpoint(control)
    finally:
        os.close(control)
    if checkpoint is None:
        return
    if checkpoint["state"] != "ready":
        raise MigrationError("Complete the interrupted offline storage migration before startup")
    account = pwd.getpwnam(executor)
    if (checkpoint.get("executor_uid"), checkpoint.get("executor_gid")) != (account.pw_uid, account.pw_gid):
        raise MigrationError("Storage migration belongs to another execution identity")
    roots = checkpoint.get("roots")
    if not isinstance(roots, list) or len(roots) != 2 or roots[0].get("path") != str(path.parent.parent):
        raise MigrationError("Storage migration volume binding changed")
    for root in roots:
        current = Path(root["path"]).stat(follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (root["device"], root["inode"]):
            raise MigrationError("Storage migration volume binding changed")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT singleton,journal_id FROM identity").fetchall() != [(1, checkpoint.get("journal_id"))]:
            raise MigrationError("Storage migration journal identity changed")


def migrate(*, data_root, workspace_root, executor, expected_journal_id, offline=False):
    if not offline or sys.platform != "linux" or os.geteuid() != 0:
        raise MigrationError("Migration requires Linux root and explicit offline volumes")
    if not re.fullmatch(r"[0-9a-f]{32}", expected_journal_id or ""):
        raise MigrationError("Pin the existing 32-character journal identity")
    account = pwd.getpwnam(executor)
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise MigrationError("Executor must be an ordinary account")
    roots = [Path(data_root), Path(workspace_root)]
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        raise MigrationError("Data and workspace must be separate volume roots")
    _check_processes({account.pw_uid})
    _mounts_below(roots)
    with ExitStack() as stack:
        data, work = [stack.enter_context(_root(path)) for path in roots]
        if (os.fstat(data).st_dev, os.fstat(data).st_ino) == (os.fstat(work).st_dev, os.fstat(work).st_ino):
            raise MigrationError("Volume roots alias the same directory")
        control = _directory(data, CONTROL)
        stack.callback(os.close, control)
        _control_files(control)
        trees = [_walk(data, skip_control=True), _walk(work)]
        _hardlinks(trees)
        # Existing volumes can belong to a different ordinary account. Its
        # still-open descriptors would survive chown; check the former owners
        # too, rather than checking only the destination execution identity.
        owners = {s.st_uid for nodes in trees for _, s in nodes}
        owners.update((os.fstat(data).st_uid, os.fstat(work).st_uid, account.pw_uid))
        _check_processes(owners - {0})
        info = _journal(roots[0] / CONTROL / JOURNAL, expected_journal_id)
        binding = {"executor_uid": account.pw_uid, "executor_gid": account.pw_gid,
                   "roots": [{"path": str(path), "device": os.fstat(fd).st_dev, "inode": os.fstat(fd).st_ino}
                             for path, fd in zip(roots, (data, work))], "journal_id": expected_journal_id}
        previous = _read_checkpoint(control)
        if previous and any(previous.get(key) != value for key, value in binding.items()):
            raise MigrationError("Migration checkpoint belongs to different volumes or identities")
        if previous and previous["state"] == "preparing" and previous.get("snapshot_sha256") != info["snapshot_sha256"]:
            raise MigrationError("Journal changed during an interrupted migration; inspect before retrying")
        # Keep the journal at its original persistent path. A root-owned sticky
        # parent still lets the executor create user entries, but cannot let it
        # replace the root-owned control directory. Its own contents stay 0700.
        os.fchown(control, 0, 0)
        os.fchmod(control, 0o700)
        os.fchown(data, 0, account.pw_gid)
        os.fchmod(data, 0o1770)
        _control_files(control)
        for name in os.listdir(control):
            item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=control)
            try:
                os.fchown(item, 0, 0)
                os.fchmod(item, 0o600)
                os.fsync(item)
            finally:
                os.close(item)
        receipt = {"version": VERSION, "state": "preparing", **binding, **info}
        _checkpoint(control, receipt)
        _handoff(data, trees[0], account.pw_uid, account.pw_gid)
        _handoff(work, trees[1], account.pw_uid, account.pw_gid)
        os.fchown(work, account.pw_uid, account.pw_gid)
        os.fchmod(work, (os.fstat(work).st_mode & 0o777) | 0o700)
        os.fsync(work)
        os.fsync(data)
        if _journal(roots[0] / CONTROL / JOURNAL, expected_journal_id) != info:
            raise MigrationError("Journal changed during migration")
        for fd, before, skip in ((data, trees[0], True), (work, trees[1], False)):
            after = _walk(fd, skip_control=skip)
            if [(p, s.st_dev, s.st_ino) for p, s in after] != [(p, s.st_dev, s.st_ino) for p, s in before]:
                raise MigrationError("User tree changed during offline migration")
            for (_, old), (_, new) in zip(before, after):
                if (new.st_uid, new.st_gid) != (account.pw_uid, account.pw_gid):
                    raise MigrationError("User entry ownership was not preserved")
                if stat.S_ISREG(old.st_mode) and (old.st_size, old.st_mtime_ns) != (new.st_size, new.st_mtime_ns):
                    raise MigrationError("User file changed during offline migration")
        receipt["state"] = "ready"
        _checkpoint(control, receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--workspace-root", default="/workspace")
    parser.add_argument("--executor", required=True)
    parser.add_argument("--expected-journal-id", required=True)
    parser.add_argument("--offline", action="store_true", help="Every service/container using both volumes is stopped")
    args = parser.parse_args(argv)
    try:
        result = migrate(**vars(args))
    except (MigrationError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        # File/database errors may include private paths or data. Keep them out
        # of logs while retaining actionable, controlled precondition errors.
        message = str(exc) if isinstance(exc, MigrationError) else type(exc).__name__
        parser.exit(1, f"Storage migration refused: {message}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
