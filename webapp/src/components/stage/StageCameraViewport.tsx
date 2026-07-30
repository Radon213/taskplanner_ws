import { type CSSProperties, useState } from "react";
import { Video, VideoOff } from "lucide-react";

import type { CompressedImageFrame } from "../../types";

export type StageCameraId = "cam1" | "cam2" | "cam3" | "cam4" | "flir";

export type StageCameraFrames = Partial<Record<StageCameraId, CompressedImageFrame | null>>;

export type StageCameraViewportProps = {
  cameraId: StageCameraId;
  frame: CompressedImageFrame | null | undefined;
  overlay?: CompressedImageFrame | null;
  liveLabel: string;
  emptyLabel: string;
  className?: string;
  style?: CSSProperties;
};

const DEFAULT_CAMERA_IDS: readonly [StageCameraId, StageCameraId] = [
  "cam2",
  "flir",
];

function CameraCanvas({
  cameraLabel,
  frame,
  overlay,
  liveLabel,
  emptyLabel,
}: {
  cameraLabel: string;
  frame: CompressedImageFrame | null | undefined;
  overlay?: CompressedImageFrame | null;
  liveLabel: string;
  emptyLabel: string;
}) {
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

export function StageCameraViewport({
  cameraId,
  frame,
  overlay,
  liveLabel,
  emptyLabel,
  className = "",
  style,
}: StageCameraViewportProps) {
  const cameraLabel = cameraId.toUpperCase();

  return (
    <figure
      className={`stage-camera-viewport ${className}`.trim()}
      data-slot="stage-camera-viewport"
      data-camera-id={cameraId}
      data-camera-connected={frame ? "true" : "false"}
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
      <CameraCanvas
        cameraLabel={cameraLabel}
        frame={frame}
        overlay={overlay}
        liveLabel={liveLabel}
        emptyLabel={emptyLabel}
      />
    </figure>
  );
}

export function StageCameraToggleViewport({
  frames,
  overlays,
  cameraIds = DEFAULT_CAMERA_IDS,
  initialCamera,
  liveLabel,
  liveLabels,
  emptyLabel,
  emptyLabels,
  className = "",
  style,
}: {
  frames: StageCameraFrames;
  overlays?: StageCameraFrames;
  cameraIds?: readonly [StageCameraId, StageCameraId];
  initialCamera?: StageCameraId;
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
  const resolvedCamera = cameraIds.includes(activeCamera)
    ? activeCamera
    : fallbackCamera;
  const frame = frames[resolvedCamera];
  const overlay = overlays?.[resolvedCamera];
  const cameraLabel = resolvedCamera.toUpperCase();
  const resolvedLiveLabel = liveLabels?.[resolvedCamera] ?? liveLabel;
  const resolvedEmptyLabel = emptyLabels?.[resolvedCamera] ?? emptyLabel;
  const toggleLabel = cameraIds
    .map((cameraId) => cameraId.toUpperCase())
    .join(" / ");

  return (
    <figure
      className={`stage-camera-viewport switchable-stage-camera ${className}`.trim()}
      data-slot="stage-camera-toggle-viewport"
      data-camera-id={resolvedCamera}
      data-camera-connected={frame ? "true" : "false"}
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
        <i>{frame ? resolvedLiveLabel : resolvedEmptyLabel}</i>
      </figcaption>
      <CameraCanvas
        cameraLabel={cameraLabel}
        frame={frame}
        overlay={overlay}
        liveLabel={resolvedLiveLabel}
        emptyLabel={resolvedEmptyLabel}
      />
    </figure>
  );
}
