import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { Language } from "../../utils/display";
import "./monitor-workspace.css";

type FrameState = "loading" | "ready" | "error";

export function SurgiMateMonitorWorkspace({
  language,
  onExit,
}: {
  language: Language;
  onExit: () => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [frameState, setFrameState] = useState<FrameState>("loading");
  const [frameGeneration, setFrameGeneration] = useState(0);
  const title = language === "ko" ? "SurgiMate 수술 관제" : "SurgiMate surgical monitoring";
  const exitLabel = language === "ko" ? "미션 화면" : "Mission";

  // The Mission navigation trigger is unmounted when this full-screen route
  // opens. Focus the page landmark so keyboard and screen-reader users get a
  // stable starting point while the iframe finishes loading.
  useEffect(() => {
    const focusFrame = window.requestAnimationFrame(() => {
      document.getElementById("surgimate-monitor-main")?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, []);

  const handleFrameLoad = () => {
    try {
      const frameDocument = iframeRef.current?.contentDocument;
      const monitorCanvas = frameDocument?.querySelector("main.app-shell .surgical-view");
      setFrameState(monitorCanvas ? "ready" : "error");
    } catch {
      setFrameState("error");
    }
  };

  const retry = () => {
    setFrameState("loading");
    setFrameGeneration((current) => current + 1);
  };

  return (
    <main
      aria-busy={frameState === "loading"}
      aria-label={title}
      className="surgimate-monitor-workspace"
      data-slot="surgimate-monitor-workspace"
      data-state={frameState}
      id="surgimate-monitor-main"
      tabIndex={-1}
    >
      <div className="surgimate-monitor-return-zone">
        <button
          aria-label={language === "ko" ? "Taskplanner 미션 화면으로 돌아가기" : "Return to Taskplanner Mission"}
          className="surgimate-monitor-return"
          onClick={onExit}
          type="button"
        >
          <ArrowLeft aria-hidden="true" size={18} />
          <span>{exitLabel}</span>
        </button>
      </div>

      <iframe
        className="surgimate-monitor-frame"
        data-state={frameState}
        key={frameGeneration}
        onError={() => setFrameState("error")}
        onLoad={handleFrameLoad}
        ref={iframeRef}
        referrerPolicy="same-origin"
        sandbox="allow-scripts allow-same-origin allow-downloads"
        src="/monitor/index.html"
        title={title}
      />

      {frameState !== "ready" ? (
        <section
          aria-live={frameState === "error" ? "assertive" : "polite"}
          className="surgimate-monitor-feedback"
          data-state={frameState}
          role={frameState === "error" ? "alert" : "status"}
        >
          {frameState === "loading" ? (
            <>
              <span className="surgimate-monitor-skeleton wide" />
              <span className="surgimate-monitor-skeleton" />
              <span className="sr-only">
                {language === "ko" ? "수술 관제 화면을 불러오는 중입니다." : "Loading SurgiMate monitoring."}
              </span>
            </>
          ) : (
            <>
              <h1>{language === "ko" ? "수술 관제 화면을 열 수 없습니다." : "SurgiMate monitoring is unavailable."}</h1>
              <p>
                {language === "ko"
                  ? "관제 화면 자산을 확인한 뒤 다시 시도하거나 미션 화면으로 돌아가세요."
                  : "Check the monitoring assets, then retry or return to Mission."}
              </p>
              <div className="surgimate-monitor-feedback-actions">
                <button onClick={onExit} type="button">
                  <ArrowLeft aria-hidden="true" size={18} />
                  {exitLabel}
                </button>
                <button onClick={retry} type="button">
                  <RefreshCw aria-hidden="true" size={18} />
                  {language === "ko" ? "다시 시도" : "Retry"}
                </button>
              </div>
            </>
          )}
        </section>
      ) : null}
    </main>
  );
}
