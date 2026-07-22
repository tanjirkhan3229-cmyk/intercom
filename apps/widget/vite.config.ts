import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Builds the iframe app (the messenger SPA). The loader that injects this iframe is built
// separately (vite.loader.config.ts). Versioned immutable bundles + CDN rollout: P0.6.
export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
    sourcemap: true,
  },
  server: {
    port: 5173,
  },
});
