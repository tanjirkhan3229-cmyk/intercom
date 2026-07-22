import { defineConfig } from "vite";

// Builds the tiny host-page loader (src/loader.ts) as a self-contained IIFE at
// dist/relay.js. This is the ≤5 KB snippet customers embed; it injects the iframe app.
// CSP-safe: no eval, no inline code injection.
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: false, // keep the app build output
    target: "es2018",
    lib: {
      entry: "src/loader.ts",
      name: "relay",
      formats: ["iife"],
      fileName: () => "relay.js",
    },
    rollupOptions: {
      output: {
        // Single self-contained file, no code splitting.
        inlineDynamicImports: true,
      },
    },
  },
});
