import { defineConfig } from "vite";

// The browser talks only to the Vite origin; /api is proxied to the backend
// (service name `api` inside Docker Compose), so no CORS is involved in development.
const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: apiTarget, changeOrigin: true } },
    // bind-mounted source on macOS: polling makes file watching reliable in containers
    watch: { usePolling: true, interval: 300 },
  },
});
