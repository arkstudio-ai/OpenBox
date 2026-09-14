// npm run build && npm run test:nginx
// No real backend, account or cloud is used. Every created Docker resource is
// tracked by its returned ID and removed in finally, including failed runs.
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs"
import http from "node:http"
import { fileURLToPath } from "node:url"
import { randomUUID } from "node:crypto"

const root = fileURLToPath(new URL("../", import.meta.url))
const dist = `${root}dist`
// Set FRONTEND_TEST_IMAGE to exercise the actual release filesystem instead
// of bind-mounting a local build, including the image's public file modes.
const frontendImage = process.env.FRONTEND_TEST_IMAGE
if (!frontendImage) assert(existsSync(`${dist}/index.html`), "Run npm run build before the nginx test")
const nginxImage = process.env.NGINX_TEST_IMAGE || "nginx:alpine"
const pythonImage = process.env.PYTHON_TEST_IMAGE || "python:3.12-alpine"
const prefix = `openbox-ui-qa-${randomUUID().slice(0, 8)}`
const containers = []
const docker = (...args) =>
  execFileSync("docker", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 60_000,
  }).trim()
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const run = (...args) => {
  const id = docker("run", "-d", ...args)
  containers.push(id)
  return id
}
async function until(check, timeout = 20_000) {
  const deadline = Date.now() + timeout
  let error
  do {
    try {
      return await check()
    } catch (e) {
      error = e
    }
    await delay(250)
  } while (Date.now() < deadline)
  throw error
}

/** Request headers nginx sets for every proxied location; the fixture echoes them back. */
const FORWARDED = ["host", "x-real-ip", "x-forwarded-for", "x-forwarded-proto"]

/** ENV of the release stage: what the image supplies when compose sets nothing. */
function releaseEnv() {
  const lines = readFileSync(`${root}Dockerfile`, "utf8").split("\n")
  const env = {}
  for (const line of lines.slice(lines.findLastIndex((text) => /^FROM\s/.test(text)))) {
    const match = /^ENV\s+(\w+)=(\S+)$/.exec(line.trim())
    if (match) env[match[1]] = match[2]
  }
  return env
}

/** Upgrade handshake only; resolves with the upstream fixture's response headers. */
function upgrade(origin, path) {
  return new Promise((resolve, reject) => {
    const req = http.get(`${origin}${path}`, {
      headers: {
        Connection: "Upgrade",
        Upgrade: "websocket",
        "Sec-WebSocket-Key": "b3BlbmJveC11aS10ZXN0IQ==",
        "Sec-WebSocket-Version": "13",
      },
    })
    req.on("upgrade", (res, socket) => {
      socket.destroy()
      if (res.statusCode === 101) resolve(res.headers)
      else reject(new Error(`WebSocket upgrade answered ${res.statusCode}`))
    })
    req.on("response", () => reject(new Error("WebSocket was not upgraded")))
    req.on("error", reject)
    req.setTimeout(3000, () => req.destroy(new Error("WebSocket timeout")))
  })
}

const defaults = releaseEnv()
assert.equal(defaults.BACKEND_HOST, "backend:8080")
assert.equal(
  defaults.TRAJECTORY_HOST,
  "backend:8080",
  "Without TRAJECTORY_HOST the image must keep routing trajectory paths to the backend",
)

const network = docker("network", "create", "--label", "openbox.test=ui-recovery", prefix)
try {
  const backend = (suffix, alias, port = 8080) =>
    run(
      "--name",
      `${prefix}-${suffix}`,
      "--network",
      network,
      "--network-alias",
      alias,
      "-e",
      `QA_INSTANCE=${suffix}`,
      "-e",
      `QA_PORT=${port}`,
      "--entrypoint",
      "python",
      "-v",
      `${root}scripts/fixtures/nginx-backend.py:/tmp/openbox-ui-qa.py:ro`,
      pythonImage,
      "/tmp/openbox-ui-qa.py",
    )
  // A frontend container; `env` is exactly what the operator sets. A bind-mounted
  // template runs on the stock nginx image, so it is given the release ENV first.
  const frontend = (suffix, env) => {
    const settings = { ...(frontendImage ? {} : defaults), ...env }
    const id = run(
      "--name",
      `${prefix}-${suffix}`,
      "--network",
      network,
      "-p",
      "127.0.0.1::80",
      ...Object.entries(settings).flatMap(([key, value]) => ["-e", `${key}=${value}`]),
      ...(frontendImage ? [] : [
        "-v", `${root}nginx.conf:/etc/nginx/templates/default.conf.template:ro`,
        "-v", `${dist}:/usr/share/nginx/html:ro`,
      ]),
      frontendImage || nginxImage,
    )
    const port = JSON.parse(docker("inspect", id))[0].NetworkSettings.Ports["80/tcp"][0].HostPort
    const origin = `http://127.0.0.1:${port}`
    const get = (path, options = {}) =>
      fetch(`${origin}${path}`, { ...options, signal: AbortSignal.timeout(2000) })
    return { id, origin, get }
  }
  const old = backend("old", "backend")
  backend("worker", "trajectory-worker", 8090)
  const proxy = frontend("proxy", {
    BACKEND_HOST: "backend:8080",
    TRAJECTORY_HOST: "trajectory-worker:8090",
  })
  const { origin, get } = proxy
  await until(async () => assert.equal((await (await get("/api/ready")).json()).instance, "old"))
  docker("exec", proxy.id, "nginx", "-t")
  for (const path of ["/", "/index.html", "/app/auth-center", "/app/s/fixture?tab=video"]) {
    const response = await get(path)
    assert.equal(response.status, 200)
    assert.match(response.headers.get("content-type"), /text\/html/)
    assert.match(response.headers.get("cache-control"), /no-store/)
    await response.text()
  }
  const assets = frontendImage
    ? docker("exec", proxy.id, "ls", "/usr/share/nginx/html/assets").split("\n")
    : readdirSync(`${dist}/assets`)
  for (const extension of ["js", "css"]) {
    const file = assets.find((name) => name.endsWith(`.${extension}`))
    assert(file)
    const response = await get(`/assets/${file}`)
    assert.equal(response.status, 200)
    assert.match(response.headers.get("content-type"), extension === "js" ? /javascript/ : /text\/css/)
    assert.match(response.headers.get("cache-control"), /immutable/)
    assert.equal(response.headers.get("x-content-type-options"), "nosniff")
    await response.arrayBuffer()
    const missing = await get(`/assets/old-missing.${extension}`)
    assert.equal(missing.status, 404)
    assert.match(missing.headers.get("content-type"), /text\/plain/)
    assert.match(missing.headers.get("cache-control"), /no-store/)
    assert.equal(await missing.text(), "Asset not found\n")
  }
  const publicFiles = readdirSync(`${root}public`, { recursive: true })
    .filter((file) => statSync(`${root}public/${file}`).isFile())
  for (const file of publicFiles) {
    const response = await get(encodeURI(`/${file}`))
    assert.equal(response.status, 200, `Public asset must be readable: ${file}`)
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), readFileSync(`${root}public/${file}`))
  }
  const requestPath = "/api/echo/a%20b?one=1&two=%2F"
  const posted = await (await get(requestPath, { method: "POST", body: "fixture-only" })).json()
  assert.deepEqual(posted, { instance: "old", path: requestPath, method: "POST", body: "fixture-only" })
  const unavailable = await get("/api/unavailable")
  assert.equal(unavailable.status, 503)
  assert.equal((await unavailable.json()).instance, "old")
  const wsPath = "/ws/fixture?ticket=local-test-only"
  const agentSocket = await upgrade(origin, wsPath)
  assert.equal(agentSocket["x-fixture-path"], wsPath)
  assert.equal(agentSocket["x-fixture-instance"], "old")

  // Admin trajectory reads, socket tickets and the watermark socket reach the
  // trajectory worker with URI, query and body intact; their neighbours do not.
  for (const path of [
    "/api/admin/trajectories/sessions?limit=1&cursor=a_b-",
    "/api/admin/trajectories/sessions/ses%2F1/records/tool%3Acall%20a?through_seq=12&expand=refs",
    "/api/admin/trajectories/sessions/ses_1/blobs/0f3a?through_seq=12",
    "/api/admin/trajectories/sessions/ses_1/payloads/pld_1?through_seq=12&meta=1",
  ]) {
    assert.deepEqual(await (await get(path)).json(), { instance: "worker", path, method: "GET", body: "" })
  }
  const ticketPath = "/api/admin/trajectories/ticket"
  const ticket = await (await get(ticketPath, { method: "POST", body: "fixture-only" })).json()
  assert.deepEqual(ticket, { instance: "worker", path: ticketPath, method: "POST", body: "fixture-only" })
  // The worker is told the same client and scheme as the backend (audit IPs, redirects).
  const seenHttp = (headers) => Object.fromEntries(FORWARDED.map((name) => [name, headers.get(`x-fixture-seen-${name}`)]))
  const seenUpgrade = (headers) => Object.fromEntries(FORWARDED.map((name) => [name, headers[`x-fixture-seen-${name}`]]))
  const backendEcho = await get("/api/echo")
  const forwarded = seenHttp(backendEcho.headers)
  assert.equal((await backendEcho.json()).instance, "old")
  for (const name of FORWARDED) assert.ok(forwarded[name], `The backend must receive ${name}`)
  assert.equal(forwarded["x-forwarded-proto"], "http")
  assert.deepEqual(seenUpgrade(agentSocket), forwarded)
  const workerEcho = await get("/api/admin/trajectories/sessions")
  assert.equal((await workerEcho.json()).instance, "worker")
  assert.deepEqual(seenHttp(workerEcho.headers), forwarded, "The worker must receive the backend's proxy headers")
  for (const path of ["/api/admin/trajectoriesx/sessions", "/api/admin/users?limit=1"]) {
    assert.equal((await (await get(path)).json()).instance, "old", `${path} must stay on the backend`)
  }
  // nginx itself answers the bare prefix (never requested by the SPA) with a
  // redirect to the worker's location instead of proxying it to the backend.
  const bare = await get("/api/admin/trajectories", { redirect: "manual" })
  assert.equal(bare.status, 301)
  assert.match(bare.headers.get("location"), /\/api\/admin\/trajectories\/$/)
  const watermarkPath = "/ws/admin/trajectories?ticket=local-test-only"
  const watermark = await upgrade(origin, watermarkPath)
  assert.equal(watermark["x-fixture-instance"], "worker")
  assert.equal(watermark["x-fixture-path"], watermarkPath)
  assert.deepEqual(seenUpgrade(watermark), forwarded, "The watermark socket must receive the backend's proxy headers")
  const nested = await upgrade(origin, "/ws/admin/trajectories/other?ticket=local-test-only")
  assert.equal(nested["x-fixture-instance"], "old", "Only the exact socket path is the worker's")
  const rendered = docker("exec", proxy.id, "nginx", "-T")
  assert.match(
    rendered,
    /location = \/ws\/admin\/trajectories \{[^}]*proxy_read_timeout 3600s;[^}]*proxy_send_timeout 3600s;/,
  )

  // Started without TRAJECTORY_HOST, the frontend comes up and keeps every
  // trajectory path on the backend.
  const plain = frontend("plain", {})
  await until(async () => assert.equal((await (await plain.get("/api/ready")).json()).instance, "old"))
  docker("exec", plain.id, "nginx", "-t")
  assert.equal((await (await plain.get("/api/admin/trajectories/sessions")).json()).instance, "old")
  assert.equal((await upgrade(plain.origin, watermarkPath))["x-fixture-instance"], "old")

  const currentIp = (id) =>
    Object.values(JSON.parse(docker("inspect", id))[0].NetworkSettings.Networks)[0].IPAddress
  const oldIp = currentIp(old)
  const next = backend("next", "next-backend")
  docker("network", "disconnect", network, next)
  docker("network", "connect", "--alias", "backend", network, next)
  assert.notEqual(currentIp(next), oldIp, "Must actually change the backend IP")
  docker("network", "disconnect", network, old)
  await until(async () => assert.equal((await (await get("/api/ready")).json()).instance, "next"))
  assert.equal((await (await get("/api/admin/trajectories/sessions")).json()).instance, "worker")
  assert.equal(JSON.parse(docker("inspect", proxy.id))[0].Id, proxy.id, "Frontend must not be restarted")
  console.log(
    "PASS: SPA no-store, immutable JS/CSS, public asset contents and permissions, missing asset 404/no-store, API URI/body/status, WebSocket upgrade, admin trajectory HTTP/ticket/WebSocket routed to TRAJECTORY_HOST with neighbours on the backend, frontend without TRAJECTORY_HOST routes them to the backend, backend IP rotation without frontend restart",
  )
} finally {
  for (const id of containers.reverse()) {
    try {
      docker("rm", "-f", id)
    } catch {
      console.error(`Could not remove temporary container ${id}`)
    }
  }
  try {
    docker("network", "rm", network)
  } catch {
    console.error(`Could not remove temporary network ${network}`)
  }
}
