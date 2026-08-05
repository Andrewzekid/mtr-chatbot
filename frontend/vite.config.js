import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import wasm from "vite-plugin-wasm";
import topLevelAwait from "vite-plugin-top-level-await";

export default defineConfig({
  plugins: [wasm(), topLevelAwait(), tailwindcss(), react()],
  server: {
    host: true,
    port: 5175,
  },
  optimizeDeps: {
    exclude: ['@rerun-io/web-viewer'],
  },
});
