import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Vite config for Yorik's React frontend.
//
// Production build lands in dist/ and is served by FastAPI as static
// files under /r/* (see backend/main.py). The base path is set to /r/
// so all asset URLs in the built index.html are prefixed correctly.
//
// During development (`npm run dev`), Vite serves on :5173 with HMR
// and proxies /api/* to the FastAPI dev server on :8000 so the
// React app talks to the real backend without CORS friction.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  base: "/r/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Inline small assets, keep larger as separate files for caching.
    assetsInlineLimit: 4096,
    // Source maps enabled so production stack traces point at real
    // function names + line numbers in the browser devtools. ~3x bundle
    // size on disk but only loaded when devtools is open. Worth it for
    // a self-hosted app where the maintainer IS the debugger.
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
