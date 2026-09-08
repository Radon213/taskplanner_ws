import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, X } from "lucide-react";

import {
  isFreshSurgeryRecordReceipt,
  isTerminalSurgeryRecordReceipt,
  surgeryRecordReceiptOutcomeKey,
  type SurgeryRecordReceipt,
} from "../../ros/surgeryRecordMessages";
import {
  projectSurgeryRecordReceipt,
  type SurgeryRecordReceiptPresentation,
} from "../../presentation/surgeryRecordPresentation";
import type { Language } from "../../utils/display";
import type {
  RosbagSurgeryRecordTab,
  RosbagUiAuditEventName,
  RosbagUiPresentation,
} from "../../ros/rosbagUiAuditMessages";

import "./surgery-record-response-modal.css";

type ReceiptTab = RosbagSurgeryRecordTab;

const RECEIPT_TABS: ReceiptTab[] = ["record", "source", "response"];
const RECEIPT_AUTO_CLOSE_SECONDS = 30;

function displayServerTime(value: string, language: Language): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value || "—";
  return new Intl.DateTimeFormat(language === "ko" ? "ko-KR" : "en-US", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(parsed));
}

function defaultTab(model: SurgeryRecordReceiptPresentation): ReceiptTab {
  if (model.document.kind === "structured" || model.document.kind === "plain") return "record";
  if (model.source.text) return "response";
  return "source";
}

function outcomeCopy(outcome: SurgeryRecordReceiptPresentation["outcome"], language: Language) {
  if (outcome === "success") {
    return language === "ko"
      ? {
        title: "수술기록지가 생성되었습니다!",
        description: "수술 종료 후 생성한 수술기록지를 서버가 정상 접수했습니다.",
        status: "전송 완료",
      }
      : {
        title: "Surgery record generated",
        description: "The server accepted the surgery record generated after the procedure ended.",
        status: "Accepted",
      };
  }
  if (outcome === "warning") {
    return language === "ko"
      ? {
        title: "수술기록지가 생성되었지만 확인이 필요합니다",
        description: "기록지 내용과 서버 원문을 함께 확인하세요.",
        status: "확인 필요",
      }
      : {
        title: "Review the surgery record",
        description: "Review the record body together with the exact server response.",
        status: "Review needed",
      };
  }
  return language === "ko"
    ? {
      title: "수술기록지 전송 결과를 확인하세요",
      description: "수술기록지 서버가 성공 접수를 확인하지 못했습니다. 실제 응답을 확인하세요.",
      status: "전송 결과 확인",
    }
    : {
      title: "Review surgery record submission",
      description: "The record server did not confirm acceptance. Review the exact response.",
      status: "Review submission",
    };
}

function warningCopy(model: SurgeryRecordReceiptPresentation, language: Language): string {
  if (!model.warnings.length) return "";
  if (model.warnings.includes("response_truncated")) {
    return language === "ko"
      ? "서버 응답은 표시 길이 제한에 맞춰 일부만 포함되었습니다."
      : "The server response was limited for display.";
  }
  if (model.warnings.includes("summary_headers_unrecognized")) {
    return language === "ko"
      ? "서버가 표준 5개 섹션과 다른 기록지 형식을 반환해 원문 형식으로 표시합니다."
      : "The server returned a non-standard record layout, so it is shown as source text.";
  }
  if (
    model.warnings.includes("surgery_code_mismatch")
    || model.warnings.includes("received_at_mismatch")
  ) {
    return language === "ko"
      ? "영수증과 서버 응답의 메타데이터가 일치하지 않습니다. 원문을 확인하세요."
      : "Receipt metadata differs from the server response. Review the source text.";
  }
  return language === "ko"
    ? "수술기록지 응답을 함께 확인하세요."
    : "Review the surgery record response.";
}

function tabLabel(tab: ReceiptTab, language: Language): string {
  if (language === "ko") {
    return tab === "record" ? "기록지" : tab === "source" ? "전송 원문" : "서버 원문";
  }
  return tab === "record" ? "Record" : tab === "source" ? "Sent text" : "Server source";
}

function sourceTitle(model: SurgeryRecordReceiptPresentation, language: Language): string {
  if (model.source.kind === "error") return language === "ko" ? "서버 오류 원문" : "Server error source";
  return language === "ko" ? "서버 응답 원문" : "Server response source";
}

function shouldDismissForNewProcedureRun(
  receipt: SurgeryRecordReceipt,
  activeProcedureRunId: string,
): boolean {
  return Boolean(
    activeProcedureRunId
    && activeProcedureRunId !== receipt.procedureRunId,
  );
}

function renderRecordDocument(
  model: SurgeryRecordReceiptPresentation,
  warning: string,
  language: Language,
) {
  if (model.document.kind === "structured") {
    return (
      <>
        {warning ? <p className="surgery-record-receipt-structured-status">{warning}</p> : null}
        <div
          className="surgery-record-receipt-structured"
          tabIndex={0}
        >
          {model.document.sections.map((section) => (
            <section
              className="surgery-record-receipt-section"
              data-section-kind={section.key}
              key={section.key}
            >
              <header className="surgery-record-receipt-section-heading">
                <span aria-hidden="true">{section.number}</span>
                <h3>{section.label}</h3>
              </header>
              <div className="surgery-record-receipt-section-content">
                {section.key === "procedure" && model.document.procedureSteps.length ? (
                  <ol>
                    {model.document.procedureSteps.map((step) => (
                      <li key={`${step.number}-${step.text}`}><span>{step.text}</span></li>
                    ))}
                  </ol>
                ) : section.key === "instruments" && model.document.instruments.length ? (
                  <ul className="surgery-record-receipt-instrument-list">
                    {model.document.instruments.map((instrument) => (
                      <li key={instrument}>{instrument}</li>
                    ))}
                  </ul>
                ) : (
                  <p>{section.text || "—"}</p>
                )}
              </div>
            </section>
          ))}
        </div>
      </>
    );
  }

  if (model.document.kind === "plain") {
    return (
      <>
        {warning ? <p className="surgery-record-receipt-structured-status">{warning}</p> : null}
        <pre
          aria-label={language === "ko" ? "서버가 생성한 수술기록지" : "Surgery record returned by server"}
          className="surgery-record-receipt-plain-summary"
          tabIndex={0}
        >
          {model.document.summary}
        </pre>
      </>
    );
  }

  return (
    <p className="surgery-record-receipt-empty">
      {model.outcome === "error"
        ? language === "ko"
          ? "서버가 기록지 본문 대신 오류를 반환했습니다. 서버 원문에서 상세 내용을 확인하세요."
          : "The server returned an error instead of a record body. Review the server source."
        : language === "ko"
          ? "서버가 구조화 가능한 기록지 본문을 반환하지 않았습니다. 서버 원문에서 상세 내용을 확인하세요."
          : "The server did not return a structured record body. Review the server source."}
    </p>
  );
}

export function SurgeryRecordResponseModal({
  activeProcedureRunId,
  receipt,
  language,
  replayOnly = false,
  replayPresentation,
  onReplayPresentationChange,
}: {
  activeProcedureRunId: string;
  receipt: SurgeryRecordReceipt | null;
  language: Language;
  replayOnly?: boolean;
  replayPresentation?: RosbagUiPresentation;
  onReplayPresentationChange?: (
    event: Extract<
      RosbagUiAuditEventName,
      "surgery_record_opened" | "surgery_record_tab_selected" | "surgery_record_closed"
    >,
    visible: boolean,
    tab: ReceiptTab,
  ) => void;
}) {
  const [visibleReceipt, setVisibleReceipt] = useState<SurgeryRecordReceipt | null>(null);
  const [activeTab, setActiveTab] = useState<ReceiptTab>("record");
  const [remainingAutoCloseSeconds, setRemainingAutoCloseSeconds] = useState(
    RECEIPT_AUTO_CLOSE_SECONDS,
  );
  const openedKeyRef = useRef("");
  const dialogRef = useRef<HTMLElement>(null);
  const tabRefs = useRef<Record<ReceiptTab, HTMLButtonElement | null>>({
    record: null,
    source: null,
    response: null,
  });
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const recordTabId = useId();
  const sourceTabId = useId();
  const responseTabId = useId();
  const recordPanelId = useId();
  const sourcePanelId = useId();
  const responsePanelId = useId();

  const presentation = useMemo(
    () => (visibleReceipt ? projectSurgeryRecordReceipt(visibleReceipt) : null),
    [visibleReceipt],
  );
  const replayVisible = replayOnly
    ? replayPresentation?.surgeryRecordVisible === true
    : true;
  const selectedTab = replayOnly
    ? replayPresentation?.surgeryRecordTab ?? "record"
    : activeTab;
  const close = useCallback(() => {
    if (replayOnly) return;
    setVisibleReceipt(null);
    onReplayPresentationChange?.("surgery_record_closed", false, activeTab);
  }, [activeTab, onReplayPresentationChange, replayOnly]);

  useEffect(() => {
    if (!visibleReceipt || !shouldDismissForNewProcedureRun(visibleReceipt, activeProcedureRunId)) {
      return;
    }
    close();
  }, [activeProcedureRunId, close, visibleReceipt]);

  useEffect(() => {
    if (!isTerminalSurgeryRecordReceipt(receipt) || !isFreshSurgeryRecordReceipt(receipt)) return;
    if (shouldDismissForNewProcedureRun(receipt, activeProcedureRunId)) return;
    const key = surgeryRecordReceiptOutcomeKey(receipt);
    if (openedKeyRef.current === key) return;
    openedKeyRef.current = key;
    const nextPresentation = projectSurgeryRecordReceipt(receipt);
    const nextTab = defaultTab(nextPresentation);
    setActiveTab(nextTab);
    setRemainingAutoCloseSeconds(RECEIPT_AUTO_CLOSE_SECONDS);
    setVisibleReceipt(receipt);
    if (!replayOnly) {
      onReplayPresentationChange?.("surgery_record_opened", true, nextTab);
    }
  }, [activeProcedureRunId, onReplayPresentationChange, receipt, replayOnly]);

  useEffect(() => {
    if (!visibleReceipt || replayOnly) return;
    const deadline = Date.now() + RECEIPT_AUTO_CLOSE_SECONDS * 1_000;
    const updateRemainingAutoCloseSeconds = () => {
      const nextRemainingSeconds = Math.max(
        0,
        Math.ceil((deadline - Date.now()) / 1_000),
      );
      setRemainingAutoCloseSeconds(nextRemainingSeconds);
      if (nextRemainingSeconds === 0) close();
    };
    updateRemainingAutoCloseSeconds();
    const timerId = window.setInterval(updateRemainingAutoCloseSeconds, 250);
    return () => window.clearInterval(timerId);
  }, [close, replayOnly, visibleReceipt]);

  useEffect(() => {
    if (!visibleReceipt || !replayVisible) return;
    const appRoot = document.getElementById("root");
    const rootWasInert = appRoot?.inert ?? false;
    const previousBodyOverflow = document.body.style.overflow;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    if (appRoot) appRoot.inert = true;
    document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => dialogRef.current?.focus());
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close();
    };
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleEscape, true);
      if (appRoot) appRoot.inert = rootWasInert;
      document.body.style.overflow = previousBodyOverflow;
      const previousFocus = previousFocusRef.current;
      window.requestAnimationFrame(() => {
        if (previousFocus?.isConnected && !previousFocus.matches(":disabled")) {
          previousFocus.focus();
          return;
        }
        document.querySelector<HTMLElement>("main, [role='main']")?.focus();
      });
    };
  }, [close, replayVisible, visibleReceipt]);

  const selectTab = useCallback((tab: ReceiptTab, focus = false) => {
    if (replayOnly) return;
    setActiveTab(tab);
    onReplayPresentationChange?.("surgery_record_tab_selected", true, tab);
    if (focus) {
      window.requestAnimationFrame(() => tabRefs.current[tab]?.focus());
    }
  }, [onReplayPresentationChange, replayOnly]);

  if (typeof document === "undefined" || !visibleReceipt || !presentation || !replayVisible) return null;

  const copy = outcomeCopy(presentation.outcome, language);
  const warning = warningCopy(presentation, language);
  const tabIds: Record<ReceiptTab, { tab: string; panel: string }> = {
    record: { tab: recordTabId, panel: recordPanelId },
    source: { tab: sourceTabId, panel: sourcePanelId },
    response: { tab: responseTabId, panel: responsePanelId },
  };
  const sourceText = presentation.source.text || (
    language === "ko" ? "서버 응답 본문이 없습니다." : "No server response body was returned."
  );

  return createPortal(
    <div
      className="surgery-record-receipt-overlay"
      data-slot="surgery-record-receipt-backdrop"
    >
      <section
        aria-describedby={descriptionId}
        aria-labelledby={titleId}
        aria-modal="true"
        className="surgery-record-receipt-dialog"
        data-outcome={presentation.outcome}
        data-slot="surgery-record-receipt-dialog"
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const focusable = Array.from(
            dialogRef.current?.querySelectorAll<HTMLElement>(
              "button:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
            ) ?? [],
          ).filter((element) => !element.hidden && !element.closest("[hidden]"));
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header>
          <span className="surgery-record-receipt-outcome" aria-hidden="true">
            {presentation.outcome === "success" ? <CheckCircle2 size={19} /> : <AlertTriangle size={19} />}
          </span>
          <div className="surgery-record-receipt-heading">
            <small>{language === "ko" ? "수술기록지 생성 서버" : "SURGERY RECORD SERVER"}</small>
            <h2 id={titleId}>{copy.title}</h2>
            <div className="surgery-record-receipt-status-line">
              <span className="surgery-record-receipt-status">{copy.status}</span>
              <span
                className="surgery-record-receipt-auto-close"
                data-slot="surgery-record-receipt-auto-close"
                role="timer"
              >
                {language === "ko"
                  ? `${remainingAutoCloseSeconds}초 후 자동으로 닫힘`
                  : `Closes in ${remainingAutoCloseSeconds}s`}
              </span>
            </div>
          </div>
          <button
            aria-label={language === "ko" ? "수술기록지 닫기" : "Close surgery record"}
            className="surgery-record-receipt-close"
            onClick={close}
            type="button"
          >
            <X aria-hidden="true" size={22} />
          </button>
        </header>

        <div className="surgery-record-receipt-content">
          <p className="surgery-record-receipt-description" id={descriptionId}>{copy.description}</p>
          <dl className="surgery-record-receipt-details">
            <div className="surgery-record-receipt-detail surgery-record-receipt-note-title"><dt>{language === "ko" ? "기록지 제목" : "RECORD TITLE"}</dt><dd>{presentation.metadata.noteTitle || "—"}</dd></div>
            <div className="surgery-record-receipt-detail"><dt>{language === "ko" ? "날짜" : "DATE"}</dt><dd>{presentation.metadata.date || "—"}</dd></div>
            <div className="surgery-record-receipt-detail"><dt>{language === "ko" ? "수술실" : "ROOM"}</dt><dd>{presentation.metadata.roomName || "—"}</dd></div>
            <div className="surgery-record-receipt-detail surgery-record-receipt-surgery-code"><dt>{language === "ko" ? "수술 코드" : "SURGERY CODE"}</dt><dd>{presentation.metadata.surgeryCode || "—"}</dd></div>
            <div className="surgery-record-receipt-detail"><dt>{language === "ko" ? "서버 기록 ID" : "SERVER RECORD ID"}</dt><dd>{presentation.metadata.id || "—"}</dd></div>
            <div className="surgery-record-receipt-detail"><dt>{language === "ko" ? "접수 ID" : "RECEIPT ID"}</dt><dd>{presentation.metadata.receiptId || "—"}</dd></div>
          </dl>

          <div aria-label={language === "ko" ? "수술기록지 보기" : "Surgery record views"} className="surgery-record-receipt-tabs" role="tablist">
            {RECEIPT_TABS.map((tab) => (
              <button
                aria-controls={tabIds[tab].panel}
                aria-selected={selectedTab === tab}
                id={tabIds[tab].tab}
                key={tab}
                onClick={() => selectTab(tab)}
                onKeyDown={(event) => {
                  if (!(["ArrowLeft", "ArrowRight", "Home", "End"] as string[]).includes(event.key)) return;
                  event.preventDefault();
                  const currentIndex = RECEIPT_TABS.indexOf(tab);
                  const nextIndex = event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? RECEIPT_TABS.length - 1
                      : event.key === "ArrowLeft"
                        ? (currentIndex - 1 + RECEIPT_TABS.length) % RECEIPT_TABS.length
                        : (currentIndex + 1) % RECEIPT_TABS.length;
                  selectTab(RECEIPT_TABS[nextIndex], true);
                }}
                ref={(element) => { tabRefs.current[tab] = element; }}
                role="tab"
                tabIndex={selectedTab === tab ? 0 : -1}
                type="button"
              >
                {tabLabel(tab, language)}
              </button>
            ))}
          </div>

          <div className="surgery-record-receipt-panel-stack">
            <section
              aria-labelledby={recordTabId}
              className="surgery-record-receipt-panel surgery-record-receipt-structured-panel"
              hidden={selectedTab !== "record"}
              id={recordPanelId}
              role="tabpanel"
            >
              {renderRecordDocument(presentation, warning, language)}
            </section>
            <section
              aria-labelledby={sourceTabId}
              className="surgery-record-receipt-panel"
              hidden={selectedTab !== "source"}
              id={sourcePanelId}
              role="tabpanel"
            >
              <section className="surgery-record-receipt-body">
                <div className="surgery-record-receipt-body-heading"><h3>{language === "ko" ? "전송 원문" : "SENT TEXT"}</h3><span>{language === "ko" ? "SOURCE TEXT" : "SOURCE TEXT"}</span></div>
                <pre aria-label={language === "ko" ? "서버로 전송한 수술기록지 원문" : "Surgery record text sent to server"} tabIndex={0}>{visibleReceipt.recordText}</pre>
              </section>
            </section>
            <section
              aria-labelledby={responseTabId}
              className="surgery-record-receipt-panel"
              hidden={selectedTab !== "response"}
              id={responsePanelId}
              role="tabpanel"
            >
              <section className="surgery-record-receipt-body">
                <div className="surgery-record-receipt-body-heading"><h3>{sourceTitle(presentation, language)}</h3>{visibleReceipt.responseTruncated ? <span>{language === "ko" ? "표시 길이 제한" : "DISPLAY LIMITED"}</span> : null}</div>
                <pre aria-label={sourceTitle(presentation, language)} tabIndex={0}>{sourceText}</pre>
              </section>
            </section>
          </div>
        </div>

        <footer aria-label={language === "ko" ? "서버 전송 정보" : "Server delivery information"}>
          <div className="surgery-record-receipt-transport">
            <span><small>{language === "ko" ? "수신 시각" : "RECEIVED"}</small><time>{displayServerTime(presentation.metadata.receivedAt, language)}</time></span>
            <span><small>HTTP</small><strong>{presentation.metadata.httpStatus || "—"}</strong></span>
          </div>
          <button onClick={close} type="button">{language === "ko" ? "닫기" : "Close"}</button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
