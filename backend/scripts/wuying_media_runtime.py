"""Build the pinned media runtime locally and deliver it to WUYING via OSS.

WUYING desktops sit on a mainland network where a 300+ MB npm dependency tree
is slow and fragile.  Build in a local linux/amd64 container, upload one
content-addressed temporary archive, then let the desktop download it through
the same-region OSS intranet endpoint.  The temporary object is deleted after
the verified install; no cloud credential is ever copied to the desktop.
"""
from __future__ import annotations

import hashlib
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile


REPO = pathlib.Path(__file__).resolve().parents[2]
MEDIA_SOURCE = REPO / "container" / "media-runtime"
DOCKER_IMAGE = "node:22-bookworm-slim"


def build_linux_bundle() -> pathlib.Path:
    """Return a linux/amd64 tarball in a uniquely-created local temp dir."""
    if not shutil.which("docker"):
        raise SystemExit("docker is required to build the WUYING media runtime locally")
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="openbox-media-runtime-"))
    bundle = temp_dir / "openbox-media-runtime-linux-amd64.tar.gz"
    print("  building pinned linux/amd64 media runtime locally")
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-e",
                "PUPPETEER_SKIP_DOWNLOAD=true",
                "-e",
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
                "-v",
                f"{MEDIA_SOURCE}:/source:ro",
                "-v",
                f"{temp_dir}:/output",
                DOCKER_IMAGE,
                "sh",
                "-ec",
                """
mkdir -p /build && cd /build
cp /source/package.json /source/package-lock.json .
npm config set registry https://registry.npmmirror.com
npm config set replace-registry-host always
npm ci --omit=dev --no-audit --no-fund
npm ls hyperframes gsap --depth=0
mkdir -p node-runtime/bin node-runtime/lib/node_modules
cp /usr/local/bin/node node-runtime/bin/node
cp -a /usr/local/lib/node_modules/npm node-runtime/lib/node_modules/npm
tar -czf /output/openbox-media-runtime-linux-amd64.tar.gz node_modules node-runtime
""",
            ],
            check=True,
        )
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    if not bundle.is_file() or bundle.stat().st_size <= 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit("local media runtime build did not produce an archive")
    print(f"  local bundle ready ({bundle.stat().st_size / 1024 / 1024:.1f} MiB)")
    return bundle


def install_bundle_via_oss(desktop, bundle: pathlib.Path) -> None:
    """Upload one archive, atomically install it remotely, then delete it."""
    backend = REPO / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

    from dotenv import load_dotenv
    import httpx

    load_dotenv(backend / ".env")
    from core.oss import get_oss

    oss = get_oss()
    hasher = hashlib.sha256()
    with bundle.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    object_key = f"_openbox/runtime-tmp/media-runtime-{digest}.tar.gz"
    put_url = oss.presign_put(object_key, "application/gzip", expires_sec=3600)
    uploaded = False
    try:
        print("  uploading media bundle to a temporary OSS object")

        def chunks():
            with bundle.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        response = httpx.put(
            put_url,
            content=chunks(),
            headers={
                "Content-Type": "application/gzip",
                "Content-Length": str(bundle.stat().st_size),
            },
            timeout=httpx.Timeout(120.0, write=1800.0),
        )
        if response.status_code not in (200, 201, 204):
            raise SystemExit(f"temporary OSS upload failed with HTTP {response.status_code}")
        uploaded = True

        internal = bool(oss.region and oss.region == desktop.region)
        get_url = oss.presign_get(object_key, expires_sec=3600, internal=internal)
        archive = "/tmp/openbox-media-runtime-linux-amd64.tar.gz"
        stage = f"/opt/openbox/media/.node_modules.new.{digest[:12]}"
        previous = "/opt/openbox/media/.node_modules.previous"
        node_stage = f"/usr/local/lib/nodejs/.node.new.{digest[:12]}"
        node_previous = "/usr/local/lib/nodejs/.node.previous"
        script = f"""
set -e
archive={shlex.quote(archive)}
stage={shlex.quote(stage)}
previous={shlex.quote(previous)}
node_stage={shlex.quote(node_stage)}
node_previous={shlex.quote(node_previous)}
curl -fsSL --retry 3 --retry-delay 2 -o "$archive" {shlex.quote(get_url)}
printf '%s  %s\n' {shlex.quote(digest)} "$archive" | sha256sum -c -
rm -rf "$stage" "$previous" "$node_stage" "$node_previous"
mkdir -p "$stage"
tar -xzf "$archive" -C "$stage"
test -x "$stage/node_modules/.bin/hyperframes"
test -f "$stage/node_modules/gsap/dist/gsap.min.js"
test -x "$stage/node-runtime/bin/node"
"$stage/node-runtime/bin/node" -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
mkdir -p "$node_stage/bin" "$node_stage/lib/node_modules"
mv "$stage/node-runtime/bin/node" "$node_stage/bin/node"
mv "$stage/node-runtime/lib/node_modules/npm" "$node_stage/lib/node_modules/npm"
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$node_stage/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$node_stage/bin/npx"
if [ -d /opt/openbox/media/node_modules ]; then
  mv /opt/openbox/media/node_modules "$previous"
fi
if [ -d /usr/local/lib/nodejs/node ]; then
  mv /usr/local/lib/nodejs/node "$node_previous"
fi
mv "$node_stage" /usr/local/lib/nodejs/node
for executable in node npm npx; do
  ln -sfn "/usr/local/lib/nodejs/node/bin/$executable" "/usr/local/bin/$executable"
done
mv "$stage/node_modules" /opt/openbox/media/node_modules
if ! (node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)' \
      && cd /opt/openbox/media \
      && npm ls hyperframes gsap --depth=0 \
      && node_modules/.bin/hyperframes --help >/dev/null); then
  rm -rf /opt/openbox/media/node_modules
  [ ! -d "$previous" ] || mv "$previous" /opt/openbox/media/node_modules
  rm -rf /usr/local/lib/nodejs/node
  [ ! -d "$node_previous" ] || mv "$node_previous" /usr/local/lib/nodejs/node
  exit 1
fi
(cd /opt/openbox/media && node_modules/.bin/hyperframes telemetry disable >/dev/null 2>&1) || true
rm -rf "$previous" "$node_previous" "$stage"
rm -f "$archive"
echo "local media bundle installed with node $(node -v)"
"""
        print("  installing media bundle from OSS on WUYING")
        print(desktop.run(script, timeout=1200).strip())
    finally:
        try:
            if uploaded:
                delete = httpx.delete(oss.presign_delete(object_key), timeout=30.0)
                if delete.status_code not in (200, 204):
                    print(f"  warning: temporary OSS cleanup returned HTTP {delete.status_code}")
        finally:
            shutil.rmtree(bundle.parent, ignore_errors=True)


def ensure_local_media_runtime(desktop, *, force: bool = False) -> None:
    """Install only when missing, unless an explicit verified replacement is requested."""
    readiness = desktop.run(
        "if test -x /opt/openbox/media/node_modules/.bin/hyperframes "
        "&& test -f /opt/openbox/media/node_modules/gsap/dist/gsap.min.js; "
        "then echo ready; else echo missing; fi",
        timeout=120,
    ).strip().splitlines()[-1]
    if readiness == "ready" and not force:
        print("  pinned media runtime already present; skipping dependency transfer")
        return
    bundle = build_linux_bundle()
    install_bundle_via_oss(desktop, bundle)
