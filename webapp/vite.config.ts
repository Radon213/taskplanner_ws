import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function runtimeControlProxy() {
  const target = process.env.TASKPLANNER_RUNTIME_CONTROL_URL?.trim();
  const tokenFile = process.env.TASKPLANNER_RUNTIME_CONTROL_TOKEN_FILE?.trim();
  if (!target || !tokenFile) return undefined;
  try {
    const token = readFileSync(tokenFile, "utf8").trim();
    if (!token) return undefined;
    return {
      "/api/runtime": {
        target,
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/api\/runtime/, "/v1/runtime"),
        headers: { "X-Taskplanner-Runtime-Control-Token": token },
      },
    };
  } catch {
    // Direct front-end development remains available without the host-only
    // control service. The UI will show the runtime transition as unavailable.
    return undefined;
  }
}

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          "motion-vendor": ["framer-motion"],
          "ros-vendor": ["roslib"],
          "icons-vendor": ["lucide-react"],
          "three-vendor": ["three", "three/addons/loaders/GLTFLoader.js"],
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 4173,
    proxy: runtimeControlProxy(),
  },
});
