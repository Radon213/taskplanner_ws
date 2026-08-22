import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const webappRoot = fileURLToPath(new URL(".", import.meta.url));

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
  optimizeDeps: {
    include: ["roslib-monitor"],
  },
  build: {
    manifest: true,
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      input: {
        main: resolve(webappRoot, "index.html"),
        monitor: resolve(webappRoot, "monitor/index.html"),
      },
      output: {
        codeSplitting: {
          groups: [
            {
              name: "react-vendor",
              test: /\/node_modules\/(react|react-dom|scheduler)\//,
              priority: 40,
            },
            {
              name: "ros-vendor",
              test: /\/node_modules\/(roslib|socket\.io|socket\.io-client|engine\.io|engine\.io-client|ws)\//,
              priority: 30,
            },
            {
              name: "icons-vendor",
              test: /\/node_modules\/lucide-react\//,
              priority: 20,
            },
            {
              // Runtime selection, bridge admission, and bounded payload parsing
              // form one cacheable safety boundary shared by Mission/Debug.
              name: "runtime-core",
              test: /\/src\/(?:runtimeModes|hooks\/use(?:RosBridge|RuntimeControl)|utils\/(?:display|runtimeAuthorityCopy))\.tsx?$/,
              priority: 15,
            },
            {
              name: "three-vendor",
              test: /\/node_modules\/three\//,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 4173,
    // The desktop entry point, LAN proxy, and runtime-control CORS contract all
    // target 4173. Never drift silently to 4174 after a config/HMR restart.
    strictPort: true,
    proxy: runtimeControlProxy(),
  },
});
