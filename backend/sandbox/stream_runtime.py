"""Install the stream receipt on new/pool guests before exposing their channel."""
from __future__ import annotations

import hashlib
import inspect
import json
import shlex

from scripts import wuying_patch_stream_output as patch


async def ensure_desktop_stream_output(desktop_id: str, *, authority_check) -> None:
    from sandbox.channel import run_desktop_command

    source = patch.LOCAL.read_text()
    reader, receipt = [patch.fragment(source, n) for n in patch.stream_nodes(source)]
    helpers = "\n\n".join(inspect.getsource(f) for f in (patch.stream_nodes, patch.patched_source))
    probe = f'''import ast,hashlib,json,pathlib,textwrap
{helpers}
before=pathlib.Path({patch.REMOTE!r}).read_bytes()
after=patched_source(before.decode(),{reader!r},{receipt!r}).encode()
print(json.dumps({{'sha256':hashlib.sha256(before).hexdigest(),'current':before==after}}))
'''
    await authority_check()
    result = json.loads(await run_desktop_command(
        desktop_id, "python3 -I -S -c " + shlex.quote(probe), timeout=60))
    if result["current"]:
        return
    # A changed preimage gets its own retained backup; CAS rejects any race.
    identity = hashlib.sha256((reader + receipt).encode()).hexdigest()[:16]
    release = f"channel_stream_{identity}_{result['sha256'][:16]}"
    command = patch.publication_script(result["sha256"], reader, receipt, release)
    await authority_check()
    await run_desktop_command(desktop_id, command, timeout=60)
    # Channel.install starts/restarts the service after all prerequisite files.
