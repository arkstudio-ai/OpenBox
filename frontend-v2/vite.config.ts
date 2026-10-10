import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import path from "node:path"
import { readFile } from "node:fs/promises"
import { readFileSync } from "node:fs"
import type { Plugin } from "vite"

// One id per build, stamped into the bundle (`__APP_BUILD__`) and into
// index.html (`<meta name="app-build">`). A tab compares the two to learn
// that a deployment happened behind it — see shared/lib/build-version.ts.
const buildId =
  process.env.VITE_BUILD_ID ||
  new Date()
    .toISOString()
    .replace(/[-:TZ]/g, "")
    .slice(0, 14)

const BACKEND_PROXY_TARGET = process.env.VITE_BACKEND_PROXY_TARGET || "http://localhost:8080"
const trajectoryProxyTarget = process.env.VITE_TRAJECTORY_PROXY_TARGET || BACKEND_PROXY_TARGET
// A LAN microphone needs a secure origin. Reuse a local certificate without putting keys in the repo.
const httpsCert = process.env.DEV_HTTPS_CERT
const httpsKey = process.env.DEV_HTTPS_KEY
if (Boolean(httpsCert) !== Boolean(httpsKey)) {
  throw new Error("Set both DEV_HTTPS_CERT and DEV_HTTPS_KEY to enable LAN HTTPS")
}

function publicLegalPages(): Plugin {
  return {
    name: "public-legal-pages",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const pathname = new URL(req.url ?? "/", "http://localhost").pathname
        if (
          !/^\/legal(?:\/en)?(?:\/(?:terms|privacy(?:\/(?:collection|third-parties|permissions))?|ai|disclaimer|contact))?\/?$/.test(
            pathname,
          )
        )
          return next()
        const file = path.join(__dirname, "public", pathname, "index.html")
        void readFile(file)
          .then((content) => {
            res.setHeader("Content-Type", "text/html; charset=utf-8")
            res.setHeader("Cache-Control", "no-cache")
            res.end(content)
          })
          .catch(next)
      })
    },
  }
}

function appBuildMeta() {
  return {
    name: "app-build-meta",
    transformIndexHtml(html: string) {
      return html.replace("<head>", `<head>\n    <meta name="app-build" content="${buildId}" />`)
    },
  }
}

export default defineConfig({
  plugins: [publicLegalPages(), react(), tailwindcss(), appBuildMeta()],
  define: { __APP_BUILD__: JSON.stringify(buildId) },
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  build: {
    target: "es2022",
    // No manual chunk groups: xterm and the markdown stack are only ever
    // reached through dynamic import(), so Rolldown splits them naturally.
    // (Grouping them by regex pulled shared React into the vendor chunk and
    // dragged it onto the first screen — measured, not hypothetical.)
  },
  server: {
    // Port 3000 matches the redirect URI registered in Logto
    // (http://localhost:3000/callback) — changing it means re-registering there.
    host: "0.0.0.0",
    https: httpsCert && httpsKey ? { cert: readFileSync(httpsCert), key: readFileSync(httpsKey) } : undefined,
    // Fixed at 3000 (Logto redirect URI), but overridable via PORT so a
    // second checkout can run its dev server alongside the main one.
    port: Number(process.env.PORT) || 3000,
    strictPort: true,
    // The first matching prefix wins, so the admin trajectory entries come
    // first. They reach the trajectory worker when VITE_TRAJECTORY_PROXY_TARGET
    // names it (e.g. http://localhost:8090) and the backend otherwise.
    proxy: {
      "/api/admin/trajectories/": trajectoryProxyTarget,
      "/ws/admin/trajectories": { target: trajectoryProxyTarget.replace(/^http/, "ws"), ws: true },
      "/api": BACKEND_PROXY_TARGET,
      "/ws": { target: BACKEND_PROXY_TARGET.replace(/^http/, "ws"), ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
