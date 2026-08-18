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
    manifest: true,
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes("/node_modules/")) return undefined;
          if (/\/(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          if (/\/(roslib|socket\.io|socket\.io-client|engine\.io|engine\.io-client|ws)\//.test(id)) {
            return "ros-vendor";
          }
          if (id.includes("/lucide-react/")) return "icons-vendor";
          if (id.includes("/three/")) return "three-vendor";
          return undefined;
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
