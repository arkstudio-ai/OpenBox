import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

function controlPlaneFrameGuard() {
  const install = (server: { middlewares: { use: (handler: (req: { url?: string }, res: { setHeader: (name: string, value: string) => void }, next: () => void) => void) => void } }) => {
    server.middlewares.use((req, res, next) => {
      const path = req.url || '/'
      if (!path.startsWith('/api/') && !path.startsWith('/ws/')) {
        res.setHeader('Content-Security-Policy', "frame-ancestors 'none'")
        res.setHeader('X-Frame-Options', 'DENY')
      }
      next()
    })
  }
  return {
    name: 'openbox-control-plane-frame-guard',
    configureServer: install,
    configurePreviewServer: install,
  }
}

export default defineConfig({
  plugins: [controlPlaneFrameGuard(), react(), tailwindcss()],
  build: {
    target: "esnext",
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-xterm": ["@xterm/xterm", "@xterm/addon-fit", "@xterm/addon-web-links"],
          "vendor-markdown": ["react-markdown", "remark-gfm", "rehype-highlight"],
          "vendor-diff": ["react-diff-viewer-continued"],
          "vendor-motion": ["framer-motion"],
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    // 3000 matches the redirect URI registered in Logto
    // (http://localhost:3000/callback) — changing it means re-registering there.
    host: '0.0.0.0',
    port: 3000,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      '/api': 'http://localhost:8080',
      '/ws': {
        target: 'ws://localhost:8080',
        ws: true,
      },
    },
  },
})
