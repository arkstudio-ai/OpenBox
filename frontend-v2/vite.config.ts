import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import path from "node:path"

function controlPlaneFrameGuard() {
  const install = (server: {
    middlewares: {
      use: (
        handler: (
          req: { url?: string },
          res: { setHeader: (name: string, value: string) => void },
          next: () => void,
        ) => void,
      ) => void
    }
  }) => {
    server.middlewares.use((req, res, next) => {
      const path = req.url || "/"
      if (!path.startsWith("/api/") && !path.startsWith("/ws/")) {
        res.setHeader("Content-Security-Policy", "frame-ancestors 'none'")
        res.setHeader("X-Frame-Options", "DENY")
      }
      next()
    })
  }
  return {
    name: "openbox-control-plane-frame-guard",
    configureServer: install,
    configurePreviewServer: install,
  }
}

export default defineConfig({
  plugins: [controlPlaneFrameGuard(), react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
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
    // Fixed at 3000 (Logto redirect URI), but overridable via PORT so a
    // second checkout can run its dev server alongside the main one.
    port: Number(process.env.PORT) || 3000,
    strictPort: true,
    proxy: {
      "/api": "http://localhost:8080",
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
