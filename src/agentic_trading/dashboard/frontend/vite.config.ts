import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` proxies /api to the FastAPI backend (uvicorn on :8600).
// Prod: `npm run build` emits dist/, which api.py serves as static files.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8600", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
});
