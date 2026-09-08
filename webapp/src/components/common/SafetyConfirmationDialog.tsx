import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { ShieldAlert } from "lucide-react";

export type SafetyConfirmationDialogProps = {
  open: boolean;
  title: string;
  description: string;
  note?: string;
  closeLabel: string;
  confirmLabel: string;
  onClose: () => void;
  onConfirm: () => void;
};

export function SafetyConfirmationDialog({
  open,
  title,
  description,
  note,
  closeLabel,
  confirmLabel,
  onClose,
  onConfirm,
}: SafetyConfirmationDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const appRoot = document.getElementById("root");
    const rootWasInert = appRoot?.inert ?? false;
    const previousBodyOverflow = document.body.style.overflow;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    if (appRoot) appRoot.inert = true;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onCloseRef.current();
    };
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      document.removeEventListener("keydown", handleEscape, true);
      if (appRoot) appRoot.inert = rootWasInert;
      document.body.style.overflow = previousBodyOverflow;
      const previousFocus = previousFocusRef.current;
      window.requestAnimationFrame(() => {
        if (
          previousFocus?.isConnected &&
          !previousFocus.matches(":disabled") &&
          !previousFocus.closest("[inert]")
        ) {
          previousFocus.focus();
          return;
        }
        document.querySelector<HTMLElement>("main, [role='main']")?.focus();
      });
    };
  }, [open]);

  if (typeof document === "undefined") return null;

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
    );
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return createPortal(
    open ? (
        <div
          className="safety-dialog-backdrop"
          data-slot="safety-confirmation-backdrop"
          onMouseDown={(event) => {
            if (event.currentTarget === event.target) onClose();
          }}
        >
          <div
            aria-describedby={descriptionId}
            aria-labelledby={titleId}
            aria-modal="true"
            className="safety-dialog"
            data-slot="safety-confirmation-dialog"
            onKeyDown={handleKeyDown}
            ref={dialogRef}
            role="alertdialog"
          >
            <span className="safety-dialog-icon" aria-hidden="true">
              <ShieldAlert size={22} strokeWidth={2} />
            </span>
            <h2 id={titleId}>{title}</h2>
            <p id={descriptionId}>{description}</p>
            {note ? <p className="safety-dialog-note">{note}</p> : null}
            <div className="safety-dialog-actions">
              <button
                className="safety-dialog-close"
                onClick={onClose}
                ref={closeButtonRef}
                type="button"
              >
                {closeLabel}
              </button>
              <button
                className="safety-dialog-confirm"
                onClick={() => {
                  onClose();
                  onConfirm();
                }}
                type="button"
              >
                {confirmLabel}
              </button>
            </div>
          </div>
        </div>
      ) : null,
    document.body,
  );
}
