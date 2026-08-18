import React from "react";
import ReactDOM from "react-dom/client";
import { LazyMotion, MotionConfig } from "framer-motion";

import App from "./App";
import "./styles.css";
import "./styles/design-tokens.css";
import "./styles/a-plus.css";

const loadMotionFeatures = () =>
  import("./motion-features").then((module) => module.default);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      <LazyMotion features={loadMotionFeatures} strict>
        <App />
      </LazyMotion>
    </MotionConfig>
  </React.StrictMode>,
);
