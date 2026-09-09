// npm run build && npm run test:nginx
// No real backend, account or cloud is used. Every created Docker resource is
// tracked by its returned ID and removed in finally, including failed runs.
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { existsSync, readdirSync } from "node:fs"
import http from "node:http"
import { fileURLToPath } from "node:url"
import { randomUUID } from "node:crypto"

const root = fileURLToPath(new URL("../", import.meta.url))
const dist = `${root}dist`
assert(existsSync(`${dist}/index.html`), "Run npm run build before the nginx test")
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
const network = docker("network", "create", "--label", "openbox.test=ui-recovery", prefix)
try {
  const backend = (suffix, alias) =>
    run(
      "--name",
      `${prefix}-${suffix}`,
      "--network",
      network,
      "--network-alias",
      alias,
      "-e",
      `QA_INSTANCE=${suffix}`,
      "--entrypoint",
      "python",
      "-v",
      `${root}scripts/fixtures/nginx-backend.py:/tmp/openbox-ui-qa.py:ro`,
      pythonImage,
      "/tmp/openbox-ui-qa.py",
    )
  const old = backend("old", "backend")
  const proxy = run(
    "--name",
    `${prefix}-proxy`,
    "--network",
    network,
    "-p",
    "127.0.0.1::80",
    "-e",
    "BACKEND_HOST=backend:8080",
    "-v",
    `${root}nginx.conf:/etc/nginx/templates/default.conf.template:ro`,
    "-v",
    `${dist}:/usr/share/nginx/html:ro`,
    nginxImage,
  )
  const port = JSON.parse(docker("inspect", proxy))[0].NetworkSettings.Ports["80/tcp"][0].HostPort
  const origin = `http://127.0.0.1:${port}`
  const get = (path, options = {}) =>
    fetch(`${origin}${path}`, { ...options, signal: AbortSignal.timeout(2000) })
  await until(async () => assert.equal((await (await get("/api/ready")).json()).instance, "old"))
  docker("exec", proxy, "nginx", "-t")
  for (const path of ["/", "/index.html", "/app/auth-center", "/app/s/fixture?tab=video"]) {
    const response = await get(path)
    assert.equal(response.status, 200)
    assert.match(response.headers.get("content-type"), /text\/html/)
    assert.match(response.headers.get("cache-control"), /no-store/)
    await response.text()
  }
  for (const extension of ["js", "css"]) {
    const file = readdirSync(`${dist}/assets`).find((name) => name.endsWith(`.${extension}`))
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
  const requestPath = "/api/echo/a%20b?one=1&two=%2F"
  const posted = await (await get(requestPath, { method: "POST", body: "fixture-only" })).json()
  assert.deepEqual(posted, { instance: "old", path: requestPath, method: "POST", body: "fixture-only" })
  const unavailable = await get("/api/unavailable")
  assert.equal(unavailable.status, 503)
  assert.equal((await unavailable.json()).instance, "old")
  const wsPath = "/ws/fixture?ticket=local-test-only"
  await new Promise((resolve, reject) => {
    const req = http.get(`${origin}${wsPath}`, {
      headers: {
        Connection: "Upgrade",
        Upgrade: "websocket",
        "Sec-WebSocket-Key": "b3BlbmJveC11aS10ZXN0IQ==",
        "Sec-WebSocket-Version": "13",
      },
    })
    req.on("upgrade", (res, socket) => {
      try {
        assert.equal(res.statusCode, 101)
        assert.equal(res.headers["x-fixture-path"], wsPath)
        resolve()
      } catch (error) {
        reject(error)
      }
      socket.destroy()
    })
    req.on("response", () => reject(new Error("WebSocket was not upgraded")))
    req.on("error", reject)
    req.setTimeout(3000, () => req.destroy(new Error("WebSocket timeout")))
  })
  const currentIp = (id) =>
    Object.values(JSON.parse(docker("inspect", id))[0].NetworkSettings.Networks)[0].IPAddress
  const oldIp = currentIp(old)
  const next = backend("next", "next-backend")
  docker("network", "disconnect", network, next)
  docker("network", "connect", "--alias", "backend", network, next)
  assert.notEqual(currentIp(next), oldIp, "Must actually change the backend IP")
  docker("network", "disconnect", network, old)
  await until(async () => assert.equal((await (await get("/api/ready")).json()).instance, "next"))
  assert.equal(JSON.parse(docker("inspect", proxy))[0].Id, proxy, "Frontend must not be restarted")
  console.log(
    "PASS: SPA no-store, immutable JS/CSS, missing asset 404/no-store, API URI/body/status, WebSocket upgrade, backend IP rotation without frontend restart",
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
