"""Driver-fenced desktop cache calls; enabled after the fleet runtime rollout."""
import os


def enabled():
    return os.environ.get("OPENBOX_MANAGED_CACHE", "").lower() in {"1", "true", "yes"}


async def command(client, action, data, *, session_id=None, user_id=None):
    from agent.driver import _current_lease
    from sandbox.runtime_operation import run_runtime_operation
    from core.identifier import ascending
    lease = _current_lease.get()
    if lease is None:
        raise RuntimeError("Managed cache needs a current Driver lease")
    if session_id and session_id != lease.session_id or user_id and user_id != lease.user_id:
        raise RuntimeError("Managed cache owner changed")

    async def send():
        await lease.assert_current()
        async with client._client(timeout=180) as http:
            response = await http.post("/cache/" + action, headers=client._request_headers(), json=data)
            response.raise_for_status()
            return response.json()

    return await run_runtime_operation(client, session_id=lease.session_id, user_id=lease.user_id,
        stage="cache_" + action.replace("-", "_"), key=ascending("cache"),
        payload={"action": action, "task_id": lease.session_id,
                 "entry_id": data.get("entry_id"), "asset_id": data.get("asset_id")},
        operation=send, before_request=lease.assert_current)


async def materialize(client, asset, url):
    await command(client, "admit", {})
    item = await command(client, "asset-claim", {"asset_id": asset.id, "name": asset.name,
                                                "size": asset.size, "oss_key": asset.oss_key})
    if not item["ready"]:
        await command(client, "asset-fetch", {"entry_id": item["entry_id"], "url": url})
    return item["path"]


async def rehydrate_command(client, source):
    """Rematerialize referenced assets; resolve old names only when unambiguous."""
    if not enabled() or not isinstance(source, str):
        return source
    import re
    from agent.driver import _current_lease
    lease = _current_lease.get()
    if lease is None:
        return source
    ids = set(re.findall(r"/workspace/\.openbox/cache/assets/([A-Za-z0-9_-]+)/", source))
    names = set(re.findall(r"/workspace/uploads/([^\s\"'`;$|&<>]+)", source))
    if not ids and not names:
        return source
    from sqlalchemy import or_, select
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.session import Session
    from sandbox.assets import deliver
    async with get_db_session() as db:
        session = await db.get(Session, lease.session_id)
        if session is None or session.user_id != lease.user_id:
            raise RuntimeError("Cache destination unavailable")
        assets = list(await db.scalars(select(FileAsset).where(FileAsset.user_id == lease.user_id,
            FileAsset.workspace_id == session.workspace_id, FileAsset.status == "ready", FileAsset.is_deleted.is_(False),
            or_(FileAsset.id.in_(ids), FileAsset.name.in_(names))).limit(128)))
        workspace_id = session.workspace_id
    for asset in assets:
        if asset.id not in ids and sum(a.name == asset.name for a in assets) != 1:
            continue  # Never pick a different upload with the same old name.
        paths = await deliver(client, client.base_url, get_oss(), [asset], install_cli=False,
                              user_id=lease.user_id, workspace_id=workspace_id)
        if not paths:
            raise RuntimeError("Referenced asset could not be rematerialized")
        if asset.name in names:
            source = re.sub(re.escape("/workspace/uploads/" + asset.name) + r"(?=$|[\s\"'`;$|&<>])",
                            lambda _: paths[0], source)
    return source


async def release_completed(client):
    if enabled() and client is not None:
        return await command(client, "release", {})


async def task_directory(ctx, category, identifier, *, legacy):
    if not enabled():
        return legacy
    import re
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in (category, identifier)):
        raise ValueError("Invalid task directory identity")
    admitted = await command(ctx.sandbox, "admit", {})
    return admitted["tmpdir"] + "/" + category + "/" + identifier


async def stage_output(ctx, path, *, entry_id, name, size, oss_key, mime, oss):
    """Keep a completed output in pending until a checksum-verified OSS PUT."""
    import shlex
    import time
    import json
    from sandbox.assets import _use_internal_oss
    from agent.driver import _current_lease
    lease = _current_lease.get()
    incoming = f"/workspace/.openbox/pending/{lease.session_id}/incoming/{lease.run_id}/" if lease else None
    if incoming and path.startswith(incoming):
        pending = await command(ctx.sandbox, "pending-adopt", {"entry_id": entry_id,
            "relative": path.removeprefix("/workspace/"), "size": size, "oss_key": oss_key, "mime": mime})
        uploaded = await command(ctx.sandbox, "pending-upload", {
            "entry_id": entry_id, "url": oss.presign_put(oss_key, mime, expires_sec=1800, internal=_use_internal_oss(oss)),
            "expires": time.time() + 1700})
        if uploaded.get("state") != "archived":
            raise RuntimeError("Output protected in pending; OSS upload will retry")
        return pending["path"]
    await command(ctx.sandbox, "admit", {"reserve": size})
    pending = await command(ctx.sandbox, "pending-prepare", {
        "entry_id": entry_id, "name": name, "size": size, "oss_key": oss_key, "mime": mime})
    # The copy runs as the configured executor and binds the exact empty file
    # the protected journal created. Neither ancestor nor leaf replacement
    # can redirect a write, and a crash leaves a recoverable inode-bound intent.
    script = '''import json,os,pathlib,sys
p=json.loads(sys.argv[1]); src=sys.argv[2]; size=int(sys.argv[3])
relative=pathlib.PurePosixPath(p['path']).relative_to('/workspace')
fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 for index,part in enumerate(relative.parts[:-1]):
  s=os.fstat(fd)
  if [s.st_dev,s.st_ino]!=p['parents'][index]:raise RuntimeError('Pending ancestor changed')
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
 s=os.fstat(fd)
 if [s.st_dev,s.st_ino]!=p['parents'][-1]:raise RuntimeError('Pending parent changed')
 out=os.open(relative.name,os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
 with os.fdopen(out,'wb') as output,open(src,'rb') as source:
  s=os.fstat(output.fileno())
  if [s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink]!=p['fingerprint']:raise RuntimeError('Pending file changed')
  written=0
  for block in iter(lambda:source.read(1024*1024),b''):
   written+=len(block)
   if written>size:raise RuntimeError('Output size changed')
   output.write(block)
  if written!=size:raise RuntimeError('Output size changed')
  output.flush();os.fsync(output.fileno())
finally:os.close(fd)
'''
    copied = await ctx.sandbox.execute("python3 -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(pending))
                                       + " " + shlex.quote(path) + " " + str(size), timeout=120)
    if copied.exit_code != 0:
        raise RuntimeError("Output retained; pending copy failed")
    await command(ctx.sandbox, "pending-register", {"entry_id": entry_id, "name": name, "oss_key": oss_key})
    uploaded = await command(ctx.sandbox, "pending-upload", {
        "entry_id": entry_id, "url": oss.presign_put(oss_key, mime, expires_sec=1800, internal=_use_internal_oss(oss)),
        "expires": time.time() + 1700})
    if uploaded.get("state") != "archived":
        raise RuntimeError("Output protected in pending; OSS upload will retry")
    return pending["path"]
