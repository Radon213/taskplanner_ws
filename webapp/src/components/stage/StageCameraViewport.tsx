import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Maximize2, Video, VideoOff, X } from "lucide-react";

import {
  TYPED_RFDETR_STAGE_MAX_FRAME_SKEW_SEC,
  type TypedRfdetrToolDetectionFrame,
} from "../../ros/toolObservationMessages";
import { useTypedRfdetrObservation } from "../../hooks/useTypedRfdetrObservation";
import type {
  CameraPreviewContract,
  CameraPreviewContracts,
  CameraPreviewSemantic,
} from "../../ros/cameraPreviewContracts";
import type {
  LiveCameraMediaStore,
  LiveStageCameraId,
} from "../../ros/liveCameraMedia";
import type { TypedRfdetrObservationStore } from "../../ros/typedRfdetrObservationStore";
import type {
  RosbagStageCameraId,
  RosbagStageCameraSlot,
  RosbagUiAuditEventName,
} from "../../ros/rosbagUiAuditMessages";
import type { CompressedImageFrame } from "../../types";

export type StageCameraId = LiveStageCameraId;

export type StageCameraFrames = Partial<
  Record<StageCameraId, CompressedImageFrame | null>
>;

export type StageCameraViewportProps = {
  cameraId: StageCameraId;
  /** Existing 9090 fallback while the isolated media bridge is unavailable. */
  frame?: CompressedImageFrame | null;
  /** Blob-backed media store; camera raster updates never rerender App. */
  mediaStore?: LiveCameraMediaStore;
  /** Browser-drawn boxes from typed remote RF-DETR facts; never a detector raster. */
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  /** External latest-only geometry store; preferred over React-owned facts. */
  typedRfdetrObservationStore?: TypedRfdetrObservationStore;
  sourceContract?: CameraPreviewContract;
  liveLabel: string;
  emptyLabel: string;
  className?: string;
  style?: CSSProperties;
  /** Browser-local rosbag presentation state. Normal live use remains uncontrolled. */
  replayOnly?: boolean;
  replayInspectionOpen?: boolean;
  replaySlot?: RosbagStageCameraSlot;
  onReplayPresentationChange?: (
    event: Extract<
      RosbagUiAuditEventName,
      "stage_camera_inspection_changed"
    >,
    slot: RosbagStageCameraSlot,
    camera: RosbagStageCameraId,
    inspecting: boolean,
  ) => void;
};

const DEFAULT_CAMERA_IDS: readonly [StageCameraId, StageCameraId] = [
  "cam2",
  "flir",
];

function hasAlignedTypedRfdetrDetection(
  cameraId: StageCameraId,
  frame: CompressedImageFrame | null | undefined,
  detection: TypedRfdetrToolDetectionFrame | null | undefined,
  sourceSemantic: CameraPreviewSemantic = "camera_source",
): detection is TypedRfdetrToolDetectionFrame {
  // The remote final compositor has already rendered the typed detections.
  // Never paint the browser vector layer over those pixels a second time.
  if (sourceSemantic === "operator_overlay") return false;
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

function cameraConnected(
  mediaStore: LiveCameraMediaStore | undefined,
  cameraId: StageCameraId,
  fallbackFrame?: CompressedImageFrame | null,
): boolean {
  return Boolean(mediaStore?.get(cameraId) ?? fallbackFrame);
}

/**
 * The image element is deliberately uncontrolled by React. The store invokes
 * this component once per source frame and changes only this DOM node's Blob
 * URL, leaving the operating-room board and all control panels untouched.
 */
function CameraCanvas({
  cameraId,
  cameraLabel,
  frame,
  mediaStore,
  typedRfdetrDetection,
  sourceContract,
  liveLabel,
  emptyLabel,
}: {
  cameraId: StageCameraId;
  cameraLabel: string;
  frame?: CompressedImageFrame | null;
  mediaStore?: LiveCameraMediaStore;
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  sourceContract?: CameraPreviewContract;
  liveLabel: string;
  emptyLabel: string;
}) {
  // A Live viewport is owned solely by the dedicated 9095 media store. The
  // legacy 9090 fallback can still change during a control-bridge transition,
  // but must never restart an in-flight Live JPEG decode.
  const fallbackFrame = mediaStore ? undefined : frame;
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const primaryMediaImageRef = useRef<HTMLImageElement | null>(null);
  const secondaryMediaImageRef = useRef<HTMLImageElement | null>(null);
  const activeMediaSlotRef = useRef<0 | 1>(0);
  const pendingMediaFrameRef = useRef<CompressedImageFrame | null>(null);
  const decodeInFlightRef = useRef(false);
  const decodeGenerationRef = useRef(0);
  // A browser that already has a stored JPEG still needs to decode it before
  // this canvas stops showing its empty state. The legacy fallback has a
  // React-owned image and keeps its existing immediate behavior.
  const availabilityRef = useRef(
    mediaStore ? false : cameraConnected(mediaStore, cameraId, fallbackFrame),
  );
  const [hasFrame, setHasFrame] = useState(availabilityRef.current);
  const frameAtRender = mediaStore?.get(cameraId) ?? fallbackFrame ?? null;
  const alignedTypedRfdetrDetection = hasAlignedTypedRfdetrDetection(
    cameraId,
    frameAtRender,
    typedRfdetrDetection,
    sourceContract?.semantic,
  );

  useEffect(() => {
    if (!mediaStore) {
      const available = Boolean(fallbackFrame);
      availabilityRef.current = available;
      setHasFrame(available);
      return undefined;
    }

    let disposed = false;
    const mediaImages = () => [
      primaryMediaImageRef.current,
      secondaryMediaImageRef.current,
    ] as const;
    const updateCanvasMetadata = (next: CompressedImageFrame | null) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      canvas.dataset.cameraConnected = next ? "true" : "false";
      canvas.dataset.cameraFrameId = next?.frameId ?? "";
      canvas.dataset.cameraSourceStamp = next?.sourceStampSec?.toString() ?? "";
      canvas.dataset.cameraReceivedAt = next?.receivedAt.toString() ?? "";
    };
    const updateAvailability = (available: boolean) => {
      if (availabilityRef.current === available) return;
      availabilityRef.current = available;
      setHasFrame(available);
    };
    const clearMediaCanvas = () => {
      // Invalidate an in-flight Blob decode before making its slot reusable.
      decodeGenerationRef.current += 1;
      decodeInFlightRef.current = false;
      pendingMediaFrameRef.current = null;
      for (const image of mediaImages()) {
        if (!image) continue;
        image.onload = null;
        image.onerror = null;
        image.dataset.active = "false";
        image.setAttribute("aria-hidden", "true");
        image.removeAttribute("src");
      }
      updateCanvasMetadata(null);
      updateAvailability(false);
    };

    const decodeNext = () => {
      if (disposed || decodeInFlightRef.current) return;
      const candidate = pendingMediaFrameRef.current;
      if (!candidate) return;
      const [primary, secondary] = mediaImages();
      const target = activeMediaSlotRef.current === 0 ? secondary : primary;
      if (!target) return;

      // Exactly one decode is active per viewport. Arrivals while it decodes
      // replace `pendingMediaFrameRef`, so a slow paint can never build a FIFO.
      pendingMediaFrameRef.current = null;
      decodeInFlightRef.current = true;
      const generation = decodeGenerationRef.current + 1;
      decodeGenerationRef.current = generation;
      let settled = false;
      const settle = (decoded: boolean) => {
        if (settled) return;
        settled = true;
        // A clear/newer decode has reused this DOM slot. Its late promise must
        // not clear the new in-flight marker or show an obsolete JPEG.
        if (disposed || generation !== decodeGenerationRef.current) return;
        target.onload = null;
        target.onerror = null;
        decodeInFlightRef.current = false;
        if (decoded) {
          const [currentPrimary, currentSecondary] = mediaImages();
          const previous = activeMediaSlotRef.current === 0
            ? currentPrimary
            : currentSecondary;
          target.dataset.active = "true";
          target.setAttribute("aria-hidden", "false");
          if (previous && previous !== target) {
            previous.dataset.active = "false";
            previous.setAttribute("aria-hidden", "true");
          }
          activeMediaSlotRef.current = target === currentPrimary ? 0 : 1;
          updateCanvasMetadata(candidate);
          updateAvailability(true);
        }
        decodeNext();
      };

      target.dataset.active = "false";
      target.setAttribute("aria-hidden", "true");
      target.onload = () => settle(true);
      target.onerror = () => settle(false);
      target.src = candidate.src;
      // `decode()` confirms that the browser has usable pixels. `onload`
      // remains the compatibility fallback for a browser without it.
      if (typeof target.decode === "function") {
        void target.decode().then(
          () => settle(true),
          () => settle(false),
        );
      }
    };

    const applyFrame = (next: CompressedImageFrame | null) => {
      if (!next) {
        clearMediaCanvas();
        return;
      }
      pendingMediaFrameRef.current = next;
      decodeNext();
    };

    applyFrame(mediaStore.get(cameraId));
    const unsubscribe = mediaStore.subscribe(cameraId, applyFrame);
    return () => {
      disposed = true;
      decodeGenerationRef.current += 1;
      // The next effect instance may immediately consume the same store. Do
      // not leave an invalidated decode holding its per-canvas single-flight
      // gate, or every later frame would remain pending forever.
      decodeInFlightRef.current = false;
      pendingMediaFrameRef.current = null;
      for (const image of mediaImages()) {
        if (!image) continue;
        image.onload = null;
        image.onerror = null;
      }
      unsubscribe();
    };
  }, [cameraId, fallbackFrame, mediaStore]);

  return (
    <div
      className="stage-camera-canvas"
      data-camera-connected={hasFrame ? "true" : "false"}
      data-camera-frame-id={frameAtRender?.frameId ?? ""}
      data-camera-received-at={frameAtRender?.receivedAt ?? ""}
      data-camera-source-stamp={frameAtRender?.sourceStampSec ?? ""}
      ref={canvasRef}
    >
      {mediaStore ? (
        <>
          <img
            alt={`${cameraLabel} ${liveLabel}`}
            aria-hidden="true"
            className="stage-camera-frame stage-camera-frame-buffer"
            data-active="false"
            decoding="async"
            draggable={false}
            ref={primaryMediaImageRef}
          />
          <img
            alt={`${cameraLabel} ${liveLabel}`}
            aria-hidden="true"
            className="stage-camera-frame stage-camera-frame-buffer"
            data-active="false"
            decoding="async"
            draggable={false}
            ref={secondaryMediaImageRef}
          />
        </>
      ) : (
        <img
          alt={`${cameraLabel} ${liveLabel}`}
          className="stage-camera-frame"
          decoding="async"
          draggable={false}
          hidden={!hasFrame}
          ref={imageRef}
          src={fallbackFrame?.src}
        />
      )}
      <div className="stage-camera-empty" hidden={hasFrame}>
        <VideoOff aria-hidden="true" size={18} strokeWidth={1.8} />
        <span>{emptyLabel}</span>
      </div>
      {alignedTypedRfdetrDetection ? (
        <TypedRfdetrDetectionOverlay detection={typedRfdetrDetection} />
      ) : null}
    </div>
  );
}

type StageCameraInspectDialogProps = {
  cameraId: StageCameraId;
  frame?: CompressedImageFrame | null;
  mediaStore?: LiveCameraMediaStore;
  typedRfdetrDetection?: TypedRfdetrToolDetectionFrame | null;
  sourceContract?: CameraPreviewContract;
  liveLabel: string;
  emptyLabel: string;
  onClose: () => void;
};

function StageCameraInspectDialog({
  cameraId,
  frame,
  mediaStore,
  typedRfdetrDetection,
  sourceContract,
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
            <span id="stage-camera-inspect-detail">{liveLabel} · source timestamp 유지</span>
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
            emptyLabel={emptyLabel}
            frame={frame}
            liveLabel={liveLabel}
            mediaStore={mediaStore}
            sourceContract={sourceContract}
            typedRfdetrDetection={typedRfdetrDetection}
          />
        </div>
        <footer>
          <code>{sourceContract?.topic || mediaStore?.get(cameraId)?.topic || "camera topic 대기"}</code>
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

function useCameraAvailability(
  mediaStore: LiveCameraMediaStore | undefined,
  cameraId: StageCameraId,
  fallbackFrame?: CompressedImageFrame | null,
): boolean {
  // Do not let legacy camera state alter the Live observer path. It is useful
  // only for Debug/LLM where no dedicated store is present.
  const activeFallbackFrame = mediaStore ? undefined : fallbackFrame;
  const [available, setAvailable] = useState(() => cameraConnected(
    mediaStore,
    cameraId,
    activeFallbackFrame,
  ));

  useEffect(() => {
    const update = (frame: CompressedImageFrame | null) => {
      const next = Boolean(frame);
      setAvailable((current) => current === next ? current : next);
    };
    update(mediaStore?.get(cameraId) ?? activeFallbackFrame ?? null);
    return mediaStore?.subscribe(cameraId, update);
  }, [activeFallbackFrame, cameraId, mediaStore]);

  return available;
}

export function StageCameraViewport({
  cameraId,
  frame,
  mediaStore,
  typedRfdetrDetection,
  typedRfdetrObservationStore,
  sourceContract,
  liveLabel,
  emptyLabel,
  className = "",
  style,
  replayOnly = false,
  replayInspectionOpen = false,
  replaySlot = "cam3",
  onReplayPresentationChange,
}: StageCameraViewportProps) {
  const cameraLabel = cameraId.toUpperCase();
  const [expanded, setExpanded] = useState(false);
  const presentationExpanded = replayOnly ? replayInspectionOpen : expanded;
  const expandTriggerRef = useRef<HTMLButtonElement | null>(null);
  const observedTypedRfdetrDetection = useTypedRfdetrObservation(
    typedRfdetrObservationStore,
    sourceContract?.semantic !== "operator_overlay" && (cameraId === "cam3" || cameraId === "cam4")
      ? cameraId
      : null,
    typedRfdetrDetection,
  );
  const activeTypedRfdetrDetection = cameraId === "cam3" || cameraId === "cam4"
    ? observedTypedRfdetrDetection
    : typedRfdetrDetection;
  const hasFrame = useCameraAvailability(mediaStore, cameraId, frame);
  const frameAtRender = mediaStore?.get(cameraId) ?? frame ?? null;
  const typedRfdetrAligned = hasAlignedTypedRfdetrDetection(
    cameraId,
    frameAtRender,
    activeTypedRfdetrDetection,
    sourceContract?.semantic,
  );

  const closeExpanded = useCallback(() => {
    if (replayOnly) return;
    setExpanded(false);
    onReplayPresentationChange?.(
      "stage_camera_inspection_changed",
      replaySlot,
      cameraId,
      false,
    );
    window.requestAnimationFrame(() => expandTriggerRef.current?.focus());
  }, [cameraId, onReplayPresentationChange, replayOnly, replaySlot]);

  useEffect(() => {
    if (!presentationExpanded || replayOnly) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeExpanded();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeExpanded, presentationExpanded, replayOnly]);

  return (
    <>
      <figure
        className={`stage-camera-viewport stage-camera-expandable ${className}`.trim()}
        data-slot="stage-camera-viewport"
        data-camera-id={cameraId}
        data-camera-connected={hasFrame ? "true" : "false"}
        data-camera-preview-semantic={sourceContract?.semantic ?? "camera_source"}
        data-camera-topic={sourceContract?.topic}
        data-typed-rfdetr={typedRfdetrAligned ? "aligned" : "none"}
        aria-label={`${cameraLabel} · ${hasFrame ? liveLabel : emptyLabel}${sourceContract ? ` · ${sourceContract.topic}` : ""}`}
        title={sourceContract?.topic}
        style={style}
      >
        <figcaption>
          <span>
            <Video aria-hidden="true" size={12} strokeWidth={2.2} />
            {cameraLabel}
          </span>
          <i>{hasFrame ? liveLabel : emptyLabel}</i>
        </figcaption>
        <button
          aria-label={`${cameraLabel} 라이브 프리뷰 확대`}
          className="stage-camera-expand"
          disabled={!hasFrame}
          onClick={(event) => {
            if (replayOnly) return;
            if (!(mediaStore?.get(cameraId) ?? frame)) return;
            expandTriggerRef.current = event.currentTarget;
            setExpanded(true);
            onReplayPresentationChange?.(
              "stage_camera_inspection_changed",
              replaySlot,
              cameraId,
              true,
            );
          }}
          ref={expandTriggerRef}
          type="button"
        >
          <CameraCanvas
            cameraId={cameraId}
            cameraLabel={cameraLabel}
            emptyLabel={emptyLabel}
            frame={frame}
            liveLabel={liveLabel}
            mediaStore={mediaStore}
            sourceContract={sourceContract}
            typedRfdetrDetection={activeTypedRfdetrDetection}
          />
          <span aria-hidden="true" className="stage-camera-expand-hint"><Maximize2 size={16} /><span>확대</span></span>
        </button>
      </figure>
      {presentationExpanded ? (
        <StageCameraInspectDialog
          cameraId={cameraId}
          emptyLabel={emptyLabel}
          frame={frame}
          liveLabel={liveLabel}
          mediaStore={mediaStore}
          typedRfdetrDetection={activeTypedRfdetrDetection}
          sourceContract={sourceContract}
          onClose={closeExpanded}
        />
      ) : null}
    </>
  );
}

export function StageCameraToggleViewport({
  frames,
  mediaStore,
  typedRfdetrDetections,
  typedRfdetrObservationStore,
  sourceContracts,
  cameraIds = DEFAULT_CAMERA_IDS,
  initialCamera,
  language = "en",
  liveLabel,
  liveLabels,
  emptyLabel,
  emptyLabels,
  className = "",
  style,
  replayOnly = false,
  replaySelectedCamera,
  replayInspectionOpen = false,
  replaySlot = "surgical_bed",
  onReplayPresentationChange,
}: {
  frames?: Partial<Record<StageCameraId, CompressedImageFrame | null>>;
  mediaStore?: LiveCameraMediaStore;
  typedRfdetrDetections?: Partial<Record<StageCameraId, TypedRfdetrToolDetectionFrame | null>>;
  typedRfdetrObservationStore?: TypedRfdetrObservationStore;
  sourceContracts?: CameraPreviewContracts;
  cameraIds?: readonly [StageCameraId, StageCameraId];
  initialCamera?: StageCameraId;
  language?: "ko" | "en";
  liveLabel: string;
  liveLabels?: Partial<Record<StageCameraId, string>>;
  emptyLabel: string;
  emptyLabels?: Partial<Record<StageCameraId, string>>;
  className?: string;
  style?: CSSProperties;
  replayOnly?: boolean;
  replaySelectedCamera?: StageCameraId;
  replayInspectionOpen?: boolean;
  replaySlot?: Exclude<RosbagStageCameraSlot, "cam3">;
  onReplayPresentationChange?: (
    event: Extract<
      RosbagUiAuditEventName,
      "stage_camera_selected" | "stage_camera_inspection_changed"
    >,
    slot: Exclude<RosbagStageCameraSlot, "cam3">,
    camera: RosbagStageCameraId,
    inspecting: boolean,
  ) => void;
}) {
  const fallbackCamera =
    initialCamera && cameraIds.includes(initialCamera)
      ? initialCamera
      : cameraIds[0];
  const [activeCamera, setActiveCamera] =
    useState<StageCameraId>(fallbackCamera);
  const [expanded, setExpanded] = useState(false);
  const expandTriggerRef = useRef<HTMLButtonElement | null>(null);
  const replayCamera = replaySelectedCamera && cameraIds.includes(replaySelectedCamera)
    ? replaySelectedCamera
    : fallbackCamera;
  const resolvedCamera = replayOnly
    ? replayCamera
    : cameraIds.includes(activeCamera)
    ? activeCamera
    : fallbackCamera;
  const presentationExpanded = replayOnly ? replayInspectionOpen : expanded;
  const sourceContract = sourceContracts?.[resolvedCamera];
  const fallbackTypedRfdetrDetection = typedRfdetrDetections?.[resolvedCamera];
  const observedTypedRfdetrDetection = useTypedRfdetrObservation(
    typedRfdetrObservationStore,
    sourceContract?.semantic !== "operator_overlay" && (resolvedCamera === "cam3" || resolvedCamera === "cam4")
      ? resolvedCamera
      : null,
    fallbackTypedRfdetrDetection,
  );
  const typedRfdetrDetection = resolvedCamera === "cam3" || resolvedCamera === "cam4"
    ? observedTypedRfdetrDetection
    : fallbackTypedRfdetrDetection;
  const frame = frames?.[resolvedCamera];
  const cameraLabel = resolvedCamera.toUpperCase();
  const resolvedLiveLabel = liveLabels?.[resolvedCamera] ?? liveLabel;
  const resolvedEmptyLabel = emptyLabels?.[resolvedCamera] ?? emptyLabel;
  const toggleLabel = cameraIds
    .map((cameraId) => cameraId.toUpperCase())
    .join(" / ");
  const nextCamera = cameraIds.find((cameraId) => cameraId !== resolvedCamera) ?? cameraIds[0];
  const nextCameraLabel = nextCamera.toUpperCase();
  const hasFrame = useCameraAvailability(mediaStore, resolvedCamera, frame);
  const frameAtRender = mediaStore?.get(resolvedCamera) ?? frame ?? null;
  const typedRfdetrAligned = hasAlignedTypedRfdetrDetection(
    resolvedCamera,
    frameAtRender,
    typedRfdetrDetection,
    sourceContract?.semantic,
  );

  const closeExpanded = useCallback(() => {
    if (replayOnly) return;
    setExpanded(false);
    onReplayPresentationChange?.(
      "stage_camera_inspection_changed",
      replaySlot,
      resolvedCamera,
      false,
    );
    window.requestAnimationFrame(() => expandTriggerRef.current?.focus());
  }, [onReplayPresentationChange, replayOnly, replaySlot, resolvedCamera]);

  useEffect(() => {
    if (!presentationExpanded || replayOnly) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeExpanded();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeExpanded, presentationExpanded, replayOnly]);

  const selectCamera = useCallback((camera: StageCameraId) => {
    if (replayOnly || !cameraIds.includes(camera)) return;
    setActiveCamera(camera);
    onReplayPresentationChange?.(
      "stage_camera_selected",
      replaySlot,
      camera,
      expanded,
    );
  }, [cameraIds, expanded, onReplayPresentationChange, replayOnly, replaySlot]);

  return (
    <>
      <figure
        className={`stage-camera-viewport switchable-stage-camera ${className}`.trim()}
        data-slot="stage-camera-toggle-viewport"
        data-camera-id={resolvedCamera}
        data-camera-connected={hasFrame ? "true" : "false"}
        data-camera-preview-semantic={sourceContract?.semantic ?? "camera_source"}
        data-camera-topic={sourceContract?.topic}
        data-typed-rfdetr={typedRfdetrAligned ? "aligned" : "none"}
        aria-label={`${cameraLabel} · ${hasFrame ? resolvedLiveLabel : resolvedEmptyLabel}${sourceContract ? ` · ${sourceContract.topic}` : ""}`}
        title={sourceContract?.topic}
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
                onClick={() => selectCamera(cameraId)}
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
            onClick={() => selectCamera(nextCamera)}
          >
            {nextCameraLabel}
          </button>
          <i>{hasFrame ? resolvedLiveLabel : resolvedEmptyLabel}</i>
        </figcaption>
        <button
          aria-label={`${cameraLabel} 라이브 프리뷰 확대`}
          className="stage-camera-expand"
          disabled={!hasFrame}
          onClick={(event) => {
            if (replayOnly) return;
            if (!(mediaStore?.get(resolvedCamera) ?? frame)) return;
            expandTriggerRef.current = event.currentTarget;
            setExpanded(true);
            onReplayPresentationChange?.(
              "stage_camera_inspection_changed",
              replaySlot,
              resolvedCamera,
              true,
            );
          }}
          ref={expandTriggerRef}
          type="button"
        >
          <CameraCanvas
            cameraId={resolvedCamera}
            cameraLabel={cameraLabel}
            emptyLabel={resolvedEmptyLabel}
            frame={frame}
            liveLabel={resolvedLiveLabel}
            mediaStore={mediaStore}
            sourceContract={sourceContract}
            typedRfdetrDetection={typedRfdetrDetection}
          />
          <span aria-hidden="true" className="stage-camera-expand-hint"><Maximize2 size={16} /><span>확대</span></span>
        </button>
      </figure>
      {presentationExpanded ? (
        <StageCameraInspectDialog
          cameraId={resolvedCamera}
          emptyLabel={resolvedEmptyLabel}
          frame={frame}
          liveLabel={resolvedLiveLabel}
          mediaStore={mediaStore}
          typedRfdetrDetection={typedRfdetrDetection}
          sourceContract={sourceContract}
          onClose={closeExpanded}
        />
      ) : null}
    </>
  );
}
