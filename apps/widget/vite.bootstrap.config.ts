import { defineConfig } from "vite";

// Builds the stable bootstrap (src/bootstrap.ts) as a self-contained IIFE at dist/boot.js.
// This is the short-TTL script customers embed; it reads the rollout pointer and loads the
// immutable v{semver}/relay.js chosen for the workspace's cohort (RFC-001 §9). CSP-safe.
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: false, // keep the app + loader build output
    target: "es2018",
    lib: {
      entry: "src/bootstrap.ts",
      name: "relayBoot",
      formats: ["iife"],
      fileName: () => "boot.js",
    },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
});
