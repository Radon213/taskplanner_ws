import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 4173,
  },
});
