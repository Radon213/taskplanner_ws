import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Maximize2, Video, VideoOff, X } from "lucide-react";

import {
  TYPED_RFDETR_STAGE_MAX_FRAME_SKEW_SEC,
  type TypedRfdetrToolDetectionFrame,
} from "../../hooks/useRosBridge";
import type { CompressedImageFrame } from "../../types";

export type StageCameraId = "cam1" | "cam2" | "cam3" | "cam4" | "flir";

export type StageCameraFrames = Partial<Record<StageCameraId, CompressedImageFrame | null>>;

export type StageCameraViewportProps = {
  cameraId: StageCameraId;
  frame: CompressedImageFrame | null | undefined;
  overlay?: CompressedImageFrame | null;
  /** Browser-drawn boxes from typed remote RF-DETR facts; never a detector raster. */
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  liveLabel: string;
  emptyLabel: string;
  className?: string;
  style?: CSSProperties;
};

const DEFAULT_CAMERA_IDS: readonly [StageCameraId, StageCameraId] = [
  "cam2",
  "flir",
];

function hasAlignedTypedRfdetrDetection(
  cameraId: StageCameraId,
  frame: CompressedImageFrame | null | undefined,
  detection: TypedRfdetrToolDetectionFrame | null | undefined,
): detection is TypedRfdetrToolDetectionFrame {
  if (!frame || !detection || detection.cameraId !== cameraId) return false;
  const frameStampSec = frame.sourceStampSec;
  if (!frame.frameId || typeof frameStampSec !== "number" || !Number.isFinite(frameStampSec)) {
    return false;
  }
  if (frame.frameId !== detection.sourceFrameId) return false;
  return Math.abs(frameStampSec - detection.sourceStampSec)
    <= TYPED_RFDETR_STAGE_MAX_FRAME_SKEW_SEC;
}

function TypedRfdetrDetectionOverlay({
  detection,
}: {
  detection: TypedRfdetrToolDetectionFrame;
}) {
  const width = detection.imageWidth;
  const height = detection.imageHeight;
  return (
    <svg
      className="stage-camera-typed-rfdetr-overlay"
      data-slot="stage-typed-rfdetr-overlay"
      data-rfdetr-camera={detection.cameraId}
      data-rfdetr-observation={detection.observationId}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
      focusable="false"
    >
      <g className="stage-camera-typed-rfdetr-source">
        <rect x="12" y="12" width="104" height="23" rx="5" />
        <text x="20" y="28">TYPED RF-DETR</text>
      </g>
      {detection.instances.map((instance) => {
        const [left, top, right, bottom] = instance.bboxXyxyNorm;
        const x = left * width;
        const y = top * height;
        const boxWidth = (right - left) * width;
        const boxHeight = (bottom - top) * height;
        const label = `${instance.className} ${Math.round(instance.confidence * 100)}%`;
        return (
          <g
            className="stage-camera-typed-rfdetr-box"
            data-rfdetr-class={instance.className}
            key={`${detection.observationId}-${instance.instanceId}`}
          >
            <rect
              x={x}
              y={y}
              width={boxWidth}
              height={boxHeight}
              vectorEffect="non-scaling-stroke"
            />
            <text x={x + 4} y={Math.max(15, y - 5)}>{label}</text>
          </g>
        );
      })}
    </svg>
  );
}

function CameraCanvas({
  cameraId,
  cameraLabel,
  frame,
  overlay,
  typedRfdetrDetection,
  liveLabel,
  emptyLabel,
}: {
  cameraId: StageCameraId;
  cameraLabel: string;
  frame: CompressedImageFrame | null | undefined;
  overlay?: CompressedImageFrame | null;
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  liveLabel: string;
  emptyLabel: string;
}) {
  const alignedTypedRfdetrDetection = hasAlignedTypedRfdetrDetection(
    cameraId,
    frame,
    typedRfdetrDetection,
  );
  return (
    <div className="stage-camera-canvas">
      {frame ? (
        <>
          <img
            className="stage-camera-frame"
            src={frame.src}
            alt={`${cameraLabel} ${liveLabel}`}
          />
          {overlay ? (
            <img
              className="stage-camera-overlay"
              src={overlay.src}
              alt=""
              aria-hidden="true"
            />
          ) : null}
          {alignedTypedRfdetrDetection ? (
            <TypedRfdetrDetectionOverlay detection={typedRfdetrDetection} />
          ) : null}
        </>
      ) : (
        <div className="stage-camera-empty">
          <VideoOff aria-hidden="true" size={18} strokeWidth={1.8} />
          <span>{emptyLabel}</span>
        </div>
      )}
    </div>
  );
}

type StageCameraInspectDialogProps = {
  cameraId: StageCameraId;
  frame: CompressedImageFrame;
  overlay?: CompressedImageFrame | null;
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  liveLabel: string;
  emptyLabel: string;
  onClose: () => void;
};

function StageCameraInspectDialog({
  cameraId,
  frame,
  overlay,
  typedRfdetrDetection,
  liveLabel,
  emptyLabel,
  onClose,
}: StageCameraInspectDialogProps) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const cameraLabel = cameraId.toUpperCase();

  useEffect(() => {
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(focusFrame);
  }, []);

  const dialog = (
    <div className="stage-camera-inspect-backdrop" onMouseDown={onClose}>
      <section
        aria-describedby="stage-camera-inspect-detail"
        aria-labelledby="stage-camera-inspect-title"
        aria-modal="true"
        className="stage-camera-inspect-dialog"
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const focusable = [...(dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          } else if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          }
        }}
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <header>
          <div>
            <p>STAGE CAMERA · LIVE VIEW</p>
            <h2 id="stage-camera-inspect-title">{cameraLabel} 확대</h2>
            <span id="stage-camera-inspect-detail">{liveLabel} · {frame.frameId || "frame_id 대기"}</span>
          </div>
          <button aria-label="확대 화면 닫기" className="stage-camera-inspect-close" onClick={onClose} ref={closeButtonRef} type="button">
            <X aria-hidden="true" size={20} />
            <span>닫기</span>
          </button>
        </header>
        <div className="stage-camera-inspect-media">
          <CameraCanvas
            cameraId={cameraId}
            cameraLabel={cameraLabel}
            frame={frame}
            overlay={overlay}
            typedRfdetrDetection={typedRfdetrDetection}
            liveLabel={liveLabel}
            emptyLabel={emptyLabel}
          />
        </div>
        <footer>
          <code>{frame.topic}</code>
          <span>Esc 또는 닫기 버튼으로 돌아가기</span>
        </footer>
      </section>
    </div>
  );

  // The stage board establishes several stacking contexts for spatial layers.
  // A portal keeps the inspection view above that board rather than letting
  // holders and tool racks bleed through the expanded live image.
  return typeof document === "undefined" ? null : createPortal(dialog, document.body);
}

export function StageCameraViewport({
  cameraId,
  frame,
  overlay,
  typedRfdetrDetection,
  liveLabel,
  emptyLabel,
  className = "",
  style,
}: StageCameraViewportProps) {
  const cameraLabel = cameraId.toUpperCase();
  const [expanded, setExpanded] = useState(false);
  const expandTriggerRef = useRef<HTMLButtonElement | null>(null);
  const typedRfdetrAligned = hasAlignedTypedRfdetrDetection(
    cameraId,
    frame,
    typedRfdetrDetection,
  );

  const closeExpanded = useCallback(() => {
    setExpanded(false);
    window.requestAnimationFrame(() => expandTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeExpanded();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeExpanded, expanded]);

  return (
    <>
      <figure
        className={`stage-camera-viewport stage-camera-expandable ${className}`.trim()}
        data-slot="stage-camera-viewport"
        data-camera-id={cameraId}
        data-camera-connected={frame ? "true" : "false"}
        data-typed-rfdetr={typedRfdetrAligned ? "aligned" : "none"}
        aria-label={`${cameraLabel} · ${frame ? liveLabel : emptyLabel}`}
        style={style}
      >
        <figcaption>
          <span>
            <Video aria-hidden="true" size={12} strokeWidth={2.2} />
            {cameraLabel}
          </span>
          <i>{frame ? liveLabel : emptyLabel}</i>
        </figcaption>
        {frame ? (
          <button
            aria-label={`${cameraLabel} 라이브 프리뷰 확대`}
            className="stage-camera-expand"
            onClick={(event) => {
              expandTriggerRef.current = event.currentTarget;
              setExpanded(true);
            }}
            ref={expandTriggerRef}
            type="button"
          >
            <CameraCanvas
              cameraId={cameraId}
              cameraLabel={cameraLabel}
              frame={frame}
              overlay={overlay}
              typedRfdetrDetection={typedRfdetrDetection}
              liveLabel={liveLabel}
              emptyLabel={emptyLabel}
            />
            <span aria-hidden="true" className="stage-camera-expand-hint"><Maximize2 size={16} /><span>확대</span></span>
          </button>
        ) : (
          <CameraCanvas
            cameraId={cameraId}
            cameraLabel={cameraLabel}
            frame={frame}
            overlay={overlay}
            typedRfdetrDetection={typedRfdetrDetection}
            liveLabel={liveLabel}
            emptyLabel={emptyLabel}
          />
        )}
      </figure>
      {expanded && frame ? (
        <StageCameraInspectDialog
          cameraId={cameraId}
          emptyLabel={emptyLabel}
          frame={frame}
          liveLabel={liveLabel}
          typedRfdetrDetection={typedRfdetrDetection}
          onClose={closeExpanded}
          overlay={overlay}
        />
      ) : null}
    </>
  );
}

export function StageCameraToggleViewport({
  frames,
  overlays,
  typedRfdetrDetections,
  cameraIds = DEFAULT_CAMERA_IDS,
  initialCamera,
  language = "en",
  liveLabel,
  liveLabels,
  emptyLabel,
  emptyLabels,
  className = "",
  style,
}: {
  frames: StageCameraFrames;
  overlays?: StageCameraFrames;
  typedRfdetrDetections?: Partial<Record<StageCameraId, TypedRfdetrToolDetectionFrame | null>>;
  cameraIds?: readonly [StageCameraId, StageCameraId];
  initialCamera?: StageCameraId;
  language?: "ko" | "en";
  liveLabel: string;
  liveLabels?: Partial<Record<StageCameraId, string>>;
  emptyLabel: string;
  emptyLabels?: Partial<Record<StageCameraId, string>>;
  className?: string;
  style?: CSSProperties;
}) {
  const fallbackCamera =
    initialCamera && cameraIds.includes(initialCamera)
      ? initialCamera
      : cameraIds[0];
  const [activeCamera, setActiveCamera] =
    useState<StageCameraId>(fallbackCamera);
  const [expanded, setExpanded] = useState(false);
  const expandTriggerRef = useRef<HTMLButtonElement | null>(null);
  const resolvedCamera = cameraIds.includes(activeCamera)
    ? activeCamera
    : fallbackCamera;
  const frame = frames[resolvedCamera];
  const overlay = overlays?.[resolvedCamera];
  const typedRfdetrDetection = typedRfdetrDetections?.[resolvedCamera];
  const cameraLabel = resolvedCamera.toUpperCase();
  const resolvedLiveLabel = liveLabels?.[resolvedCamera] ?? liveLabel;
  const resolvedEmptyLabel = emptyLabels?.[resolvedCamera] ?? emptyLabel;
  const toggleLabel = cameraIds
    .map((cameraId) => cameraId.toUpperCase())
    .join(" / ");
  const nextCamera = cameraIds.find((cameraId) => cameraId !== resolvedCamera) ?? cameraIds[0];
  const nextCameraLabel = nextCamera.toUpperCase();
  const typedRfdetrAligned = hasAlignedTypedRfdetrDetection(
    resolvedCamera,
    frame,
    typedRfdetrDetection,
  );

  const closeExpanded = useCallback(() => {
    setExpanded(false);
    window.requestAnimationFrame(() => expandTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeExpanded();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeExpanded, expanded]);

  return (
    <>
      <figure
        className={`stage-camera-viewport switchable-stage-camera ${className}`.trim()}
        data-slot="stage-camera-toggle-viewport"
        data-camera-id={resolvedCamera}
        data-camera-connected={frame ? "true" : "false"}
        data-typed-rfdetr={typedRfdetrAligned ? "aligned" : "none"}
        aria-label={`${cameraLabel} · ${frame ? resolvedLiveLabel : resolvedEmptyLabel}`}
        style={style}
      >
        <figcaption>
          <span className="stage-camera-source-label">
            <Video aria-hidden="true" size={12} strokeWidth={2.2} />
            {cameraLabel}
          </span>
          <div
            className="stage-camera-toggle"
            role="group"
            aria-label={toggleLabel}
          >
            {cameraIds.map((cameraId) => (
              <button
                key={cameraId}
                type="button"
                className={resolvedCamera === cameraId ? "active" : ""}
                aria-pressed={resolvedCamera === cameraId}
                onClick={() => setActiveCamera(cameraId)}
              >
                {cameraId.toUpperCase()}
              </button>
            ))}
          </div>
          <button
            className="stage-camera-toggle-compact"
            type="button"
            aria-label={language === "ko"
              ? `${cameraLabel}에서 ${nextCameraLabel}로 전환`
              : `Switch from ${cameraLabel} to ${nextCameraLabel}`}
            onClick={() => setActiveCamera(nextCamera)}
          >
            {nextCameraLabel}
          </button>
          <i>{frame ? resolvedLiveLabel : resolvedEmptyLabel}</i>
        </figcaption>
        {frame ? (
          <button
            aria-label={`${cameraLabel} 라이브 프리뷰 확대`}
            className="stage-camera-expand"
            onClick={(event) => {
              expandTriggerRef.current = event.currentTarget;
              setExpanded(true);
            }}
            ref={expandTriggerRef}
            type="button"
          >
            <CameraCanvas
              cameraId={resolvedCamera}
              cameraLabel={cameraLabel}
              frame={frame}
              overlay={overlay}
              typedRfdetrDetection={typedRfdetrDetection}
              liveLabel={resolvedLiveLabel}
              emptyLabel={resolvedEmptyLabel}
            />
            <span aria-hidden="true" className="stage-camera-expand-hint"><Maximize2 size={16} /><span>확대</span></span>
          </button>
        ) : (
          <CameraCanvas
            cameraId={resolvedCamera}
            cameraLabel={cameraLabel}
            frame={frame}
            overlay={overlay}
            typedRfdetrDetection={typedRfdetrDetection}
            liveLabel={resolvedLiveLabel}
            emptyLabel={resolvedEmptyLabel}
          />
        )}
      </figure>
      {expanded && frame ? (
        <StageCameraInspectDialog
          cameraId={resolvedCamera}
          emptyLabel={resolvedEmptyLabel}
          frame={frame}
          liveLabel={resolvedLiveLabel}
          typedRfdetrDetection={typedRfdetrDetection}
          onClose={closeExpanded}
          overlay={overlay}
        />
      ) : null}
    </>
  );
}
