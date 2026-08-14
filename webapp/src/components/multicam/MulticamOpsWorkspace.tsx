import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import {
  Activity,
  ArrowLeft,
  Box,
  Camera,
  CheckCircle2,
  CircleAlert,
  Database,
  Eye,
  Gauge,
  LoaderCircle,
  Network,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  Save,
  Square,
  Workflow,
} from "lucide-react";

import {
  MULTICAM_CAMERAS,
  type CameraFrame,
  type CameraFrames,
  type CaptureStatus,
  type MulticamCameraId,
  type MulticamView,
  type StaticTransform,
  type WorldAction,
  type WorldActionResult,
  type WorldAnchorStatus,
  useMulticamOpsBridge,
} from "../../hooks/useMulticamOpsBridge";

type Language = "ko" | "en";
type DepthPresentation = "visualized" | "raw";

type FramePose = {
  frame: string;
  parentFrame: string | null;
  position: Vec3;
  rotation: Quaternion;
};

type Vec3 = { x: number; y: number; z: number };
type Quaternion = { x: number; y: number; z: number; w: number };
type TfPointerDrag = { id: number; x: number; y: number; mode: "orbit" | "pan" };
type TfViewPreset = { id: string; label: string; azimuth: number; elevation: number; description: string };

const IDENTITY: Quaternion = { x: 0, y: 0, z: 0, w: 1 };
const ORIGIN: Vec3 = { x: 0, y: 0, z: 0 };
const ORBIT_ELEVATION_LIMIT = Math.PI / 2 - 0.01;
const ISOMETRIC_VIEW = { azimuth: -0.78, elevation: 0.48 };
const TF_VIEW_PRESETS: TfViewPreset[] = [
  { id: "isometric", label: "등각", ...ISOMETRIC_VIEW, description: "등각 보기" },
  { id: "plus-x", label: "+X", azimuth: Math.PI / 2, elevation: 0, description: "+X 쪽에서 보기" },
  { id: "minus-x", label: "−X", azimuth: -Math.PI / 2, elevation: 0, description: "−X 쪽에서 보기" },
  { id: "plus-y", label: "+Y", azimuth: Math.PI, elevation: 0, description: "+Y 쪽에서 보기" },
  { id: "minus-y", label: "−Y", azimuth: 0, elevation: 0, description: "−Y 쪽에서 보기" },
  { id: "plus-z", label: "+Z", azimuth: 0, elevation: ORBIT_ELEVATION_LIMIT, description: "+Z 쪽에서 보기" },
  { id: "minus-z", label: "−Z", azimuth: 0, elevation: -ORBIT_ELEVATION_LIMIT, description: "−Z 쪽에서 보기" },
];

function useClock(intervalMs = 1_000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function formatAge(at: number | null | undefined, now: number): string {
  if (!at) return "수신 전";
  const seconds = Math.max(0, (now - at) / 1_000);
  return seconds < 1 ? `${Math.round(seconds * 1_000)} ms 전` : `${seconds.toFixed(1)} s 전`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 s";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return minutes > 0 ? `${minutes} m ${remainder.toString().padStart(2, "0")} s` : `${remainder} s`;
}

function statusTone(live: boolean, stale = false): "ok" | "warn" | "idle" {
  if (live && !stale) return "ok";
  return stale ? "warn" : "idle";
}

function cameraDisplayName(id: string): string {
  return MULTICAM_CAMERAS.find((camera) => camera.id === id)?.label || id;
}

function isRecent(frame: CameraFrame | null, now: number): boolean {
  return Boolean(frame && now - frame.receivedAt < 3_000);
}

function normalizeQuaternion(value: Quaternion): Quaternion {
  const length = Math.hypot(value.x, value.y, value.z, value.w) || 1;
  return { x: value.x / length, y: value.y / length, z: value.z / length, w: value.w / length };
}

function multiplyQuaternion(left: Quaternion, right: Quaternion): Quaternion {
  return normalizeQuaternion({
    x: left.w * right.x + left.x * right.w + left.y * right.z - left.z * right.y,
    y: left.w * right.y - left.x * right.z + left.y * right.w + left.z * right.x,
    z: left.w * right.z + left.x * right.y - left.y * right.x + left.z * right.w,
    w: left.w * right.w - left.x * right.x - left.y * right.y - left.z * right.z,
  });
}

function rotateVector(rotation: Quaternion, vector: Vec3): Vec3 {
  const q = normalizeQuaternion(rotation);
  const uv = {
    x: q.y * vector.z - q.z * vector.y,
    y: q.z * vector.x - q.x * vector.z,
    z: q.x * vector.y - q.y * vector.x,
  };
  const uuv = {
    x: q.y * uv.z - q.z * uv.y,
    y: q.z * uv.x - q.x * uv.z,
    z: q.x * uv.y - q.y * uv.x,
  };
  return {
    x: vector.x + 2 * (q.w * uv.x + uuv.x),
    y: vector.y + 2 * (q.w * uv.y + uuv.y),
    z: vector.z + 2 * (q.w * uv.z + uuv.z),
  };
}

function add(left: Vec3, right: Vec3): Vec3 {
  return { x: left.x + right.x, y: left.y + right.y, z: left.z + right.z };
}

function subtract(left: Vec3, right: Vec3): Vec3 {
  return { x: left.x - right.x, y: left.y - right.y, z: left.z - right.z };
}

function scaleVector(vector: Vec3, scalar: number): Vec3 {
  return { x: vector.x * scalar, y: vector.y * scalar, z: vector.z * scalar };
}

function dot(left: Vec3, right: Vec3): number {
  return left.x * right.x + left.y * right.y + left.z * right.z;
}

function cross(left: Vec3, right: Vec3): Vec3 {
  return {
    x: left.y * right.z - left.z * right.y,
    y: left.z * right.x - left.x * right.z,
    z: left.x * right.y - left.y * right.x,
  };
}

function normalizeVector(vector: Vec3): Vec3 {
  const length = Math.hypot(vector.x, vector.y, vector.z) || 1;
  return scaleVector(vector, 1 / length);
}

function cameraBasis(azimuth: number, elevation: number): { forward: Vec3; right: Vec3; up: Vec3 } {
  const eyeDirection = normalizeVector({
    x: Math.cos(elevation) * Math.sin(azimuth),
    y: -Math.cos(elevation) * Math.cos(azimuth),
    z: Math.sin(elevation),
  });
  const forward = scaleVector(eyeDirection, -1);
  const upSeed = Math.abs(eyeDirection.z) > 0.98 ? { x: 0, y: 1, z: 0 } : { x: 0, y: 0, z: 1 };
  const right = normalizeVector(cross(forward, upSeed));
  return { forward, right, up: normalizeVector(cross(right, forward)) };
}

function inverseQuaternion(value: Quaternion): Quaternion {
  const normalized = normalizeQuaternion(value);
  return { x: -normalized.x, y: -normalized.y, z: -normalized.z, w: normalized.w };
}

function buildFramePoses(transforms: StaticTransform[]): FramePose[] {
  if (!transforms.length) return [];
  const byChild = new Map(transforms.map((transform) => [transform.childFrame, transform]));
  const parents = new Set(transforms.map((transform) => transform.parentFrame));
  const roots = [...parents].filter((frame) => !byChild.has(frame));
  const poses = new Map<string, FramePose>();
  for (const root of roots) {
    poses.set(root, { frame: root, parentFrame: null, position: ORIGIN, rotation: IDENTITY });
  }
  if (!poses.size) {
    poses.set(transforms[0].parentFrame, {
      frame: transforms[0].parentFrame,
      parentFrame: null,
      position: ORIGIN,
      rotation: IDENTITY,
    });
  }
  for (let pass = 0; pass < transforms.length + 1; pass += 1) {
    let changed = false;
    for (const transform of transforms) {
      if (poses.has(transform.childFrame)) continue;
      const parent = poses.get(transform.parentFrame);
      if (!parent) continue;
      const rotation = multiplyQuaternion(parent.rotation, transform.rotation);
      const position = add(parent.position, rotateVector(parent.rotation, transform.translation));
      poses.set(transform.childFrame, {
        frame: transform.childFrame,
        parentFrame: transform.parentFrame,
        position,
        rotation,
      });
      changed = true;
    }
    if (!changed) break;
  }
  let orphanIndex = 0;
  for (const transform of transforms) {
    if (!poses.has(transform.childFrame)) {
      orphanIndex += 1;
      poses.set(transform.childFrame, {
        frame: transform.childFrame,
        parentFrame: transform.parentFrame,
        position: { x: orphanIndex * 0.28, y: -0.35, z: 0 },
        rotation: transform.rotation,
      });
    }
  }
  return [...poses.values()];
}

function posesRelativeTo(poses: FramePose[], referenceFrame: string): FramePose[] {
  const reference = poses.find((pose) => pose.frame === referenceFrame);
  if (!reference) return poses;
  const inverseReferenceRotation = inverseQuaternion(reference.rotation);
  return poses.map((pose) => ({
    ...pose,
    position: rotateVector(inverseReferenceRotation, subtract(pose.position, reference.position)),
    rotation: multiplyQuaternion(inverseReferenceRotation, pose.rotation),
  }));
}

function labelForFrame(frame: string): string {
  if (frame.length <= 28) return frame;
  return `${frame.slice(0, 18)}…${frame.slice(-7)}`;
}

function isOperationalFrame(frame: string): boolean {
  return /^(world|tag\d+|cam_[1-4]_color_optical_frame|humanoid|bed_|mayo|surgeon)/i.test(frame);
}

function TfScene({ transforms }: { transforms: StaticTransform[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pointerRef = useRef<TfPointerDrag | null>(null);
  const viewScaleRef = useRef(1);
  const [azimuth, setAzimuth] = useState(ISOMETRIC_VIEW.azimuth);
  const [elevation, setElevation] = useState(ISOMETRIC_VIEW.elevation);
  const [pan, setPan] = useState<Vec3>(ORIGIN);
  const [zoom, setZoom] = useState(1);
  const [showAllFrames, setShowAllFrames] = useState(true);
  const allPoses = useMemo(() => buildFramePoses(transforms), [transforms]);
  const referenceFrame = useMemo(() => {
    if (allPoses.some((pose) => pose.frame === "humanoid")) return "humanoid";
    return allPoses.find((pose) => pose.parentFrame === null)?.frame || allPoses[0]?.frame || "";
  }, [allPoses]);
  const humanoidRelativePoses = useMemo(
    () => posesRelativeTo(allPoses, referenceFrame),
    [allPoses, referenceFrame],
  );
  const poses = useMemo(() => {
    if (showAllFrames) return humanoidRelativePoses;
    const selected = humanoidRelativePoses.filter((pose) => isOperationalFrame(pose.frame));
    if (!selected.length) return humanoidRelativePoses;
    const requiredParents = new Set(selected.map((pose) => pose.parentFrame).filter(Boolean));
    return humanoidRelativePoses.filter((pose) => selected.includes(pose) || requiredParents.has(pose.frame));
  }, [humanoidRelativePoses, showAllFrames]);
  const activeViewPreset = useMemo(
    () => TF_VIEW_PRESETS.find((preset) => Math.abs(preset.azimuth - azimuth) < 0.01 && Math.abs(preset.elevation - elevation) < 0.01)?.id || null,
    [azimuth, elevation],
  );

  const applyViewPreset = (preset: TfViewPreset) => {
    setAzimuth(preset.azimuth);
    setElevation(preset.elevation);
  };

  const resetView = () => {
    setAzimuth(ISOMETRIC_VIEW.azimuth);
    setElevation(ISOMETRIC_VIEW.elevation);
    setPan(ORIGIN);
    setZoom(1);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setZoom((current) => Math.max(0.5, Math.min(2.2, current - event.deltaY * 0.001)));
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const draw = () => {
      const bounds = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(bounds.width * dpr));
      canvas.height = Math.max(1, Math.round(bounds.height * dpr));
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.clearRect(0, 0, bounds.width, bounds.height);
      const styles = getComputedStyle(canvas);
      const color = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
      const palette = {
        grid: color("--line", "rgba(220, 231, 235, 0.16)"),
        link: color("--line-strong", "rgba(220, 231, 235, 0.3)"),
        text: color("--soft", "#c6d0d4"),
        muted: color("--muted", "#9eabb1"),
        active: color("--robot", "#70ddd1"),
        world: color("--clinical", "#75a7ff"),
        x: color("--tf-axis-x", "#ff8a74"),
        y: color("--tf-axis-y", "#92d979"),
        z: color("--tf-axis-z", "#75a7ff"),
      };
      context.fillStyle = color("--ops-canvas", "rgba(8, 12, 14, 0.34)");
      context.fillRect(0, 0, bounds.width, bounds.height);
      if (!poses.length) {
        context.fillStyle = palette.muted;
        context.font = "13px sans-serif";
        context.textAlign = "center";
        context.fillText("/tf_static 수신을 기다리는 중입니다.", bounds.width / 2, bounds.height / 2);
        return;
      }
      const maxExtent = Math.max(0.75, ...poses.flatMap((pose) => [
        Math.abs(pose.position.x),
        Math.abs(pose.position.y),
        Math.abs(pose.position.z),
      ]));
      const scale = Math.min(bounds.width, bounds.height) / (maxExtent * 3.1) * zoom;
      viewScaleRef.current = scale;
      const basis = cameraBasis(azimuth, elevation);
      const cameraDistance = maxExtent * 3.8;
      const project = (point: Vec3) => {
        const relative = subtract(point, pan);
        const depth = dot(relative, basis.forward);
        const perspective = cameraDistance / Math.max(cameraDistance * 0.38, cameraDistance + depth);
        return {
          x: bounds.width / 2 + dot(relative, basis.right) * scale * perspective,
          y: bounds.height / 2 - dot(relative, basis.up) * scale * perspective,
          depth,
        };
      };
      const drawLine = (start: Vec3, end: Vec3, stroke: string, width = 1) => {
        const a = project(start);
        const b = project(end);
        context.beginPath();
        context.moveTo(a.x, a.y);
        context.lineTo(b.x, b.y);
        context.strokeStyle = stroke;
        context.lineWidth = width;
        context.stroke();
      };
      for (let index = -3; index <= 3; index += 1) {
        drawLine({ x: index * maxExtent / 3, y: -maxExtent, z: 0 }, { x: index * maxExtent / 3, y: maxExtent, z: 0 }, palette.grid);
        drawLine({ x: -maxExtent, y: index * maxExtent / 3, z: 0 }, { x: maxExtent, y: index * maxExtent / 3, z: 0 }, palette.grid);
      }
      const byFrame = new Map(poses.map((pose) => [pose.frame, pose]));
      for (const pose of poses) {
        if (!pose.parentFrame) continue;
        const parent = byFrame.get(pose.parentFrame);
        if (parent) drawLine(parent.position, pose.position, palette.link, 1.25);
      }
      const axisLength = Math.max(0.12, Math.min(0.38, maxExtent * 0.14));
      const drawLocalAxes = (pose: FramePose) => {
        const emphasis = pose.frame === referenceFrame;
        const opacity = emphasis ? 1 : isOperationalFrame(pose.frame) ? 0.86 : 0.66;
        const width = emphasis ? 2.3 : 1.3;
        const axes = [
          { direction: { x: axisLength, y: 0, z: 0 }, color: palette.x },
          { direction: { x: 0, y: axisLength, z: 0 }, color: palette.y },
          { direction: { x: 0, y: 0, z: axisLength }, color: palette.z },
        ];
        context.save();
        context.globalAlpha = opacity;
        for (const axis of axes) {
          drawLine(pose.position, add(pose.position, rotateVector(pose.rotation, axis.direction)), axis.color, width);
        }
        context.restore();
      };
      for (const pose of poses) drawLocalAxes(pose);
      const compactLabels = bounds.width < 480;
      context.font = "11px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace";
      context.textAlign = "left";
      const occupiedLabels: Array<{ x: number; y: number; width: number }> = [];
      for (const pose of [...poses].sort((left, right) => left.position.z - right.position.z)) {
        const point = project(pose.position);
        const operational = isOperationalFrame(pose.frame);
        context.beginPath();
        context.arc(point.x, point.y, operational ? 4.5 : 3, 0, Math.PI * 2);
        context.fillStyle = pose.frame === "world" || /^tag\d+$/i.test(pose.frame) ? palette.world : operational ? palette.active : palette.muted;
        context.fill();
        const labelIsAnchor = pose.frame === "world" || /^tag\d+$/i.test(pose.frame);
        if ((operational || showAllFrames) && (!compactLabels || labelIsAnchor)) {
          const label = labelForFrame(pose.frame);
          const x = point.x + 7;
          const y = point.y - 6;
          const width = context.measureText(label).width;
          const collides = occupiedLabels.some((placed) => Math.abs(placed.y - y) < 13 && x < placed.x + placed.width + 8 && x + width + 8 > placed.x);
          if (!collides || labelIsAnchor) {
            context.fillStyle = palette.text;
            context.fillText(label, x, y);
            occupiedLabels.push({ x, y, width });
          }
        }
      }
    };
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    draw();
    return () => observer.disconnect();
  }, [azimuth, elevation, pan, poses, referenceFrame, showAllFrames, zoom]);

  const onPointerDown = (event: PointerEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerRef.current = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      mode: event.button === 1 || event.button === 2 || event.shiftKey ? "pan" : "orbit",
    };
  };
  const onPointerMove = (event: PointerEvent<HTMLCanvasElement>) => {
    const pointer = pointerRef.current;
    if (!pointer || pointer.id !== event.pointerId) return;
    event.preventDefault();
    const dx = event.clientX - pointer.x;
    const dy = event.clientY - pointer.y;
    pointerRef.current = { ...pointer, x: event.clientX, y: event.clientY };
    if (pointer.mode === "pan") {
      const basis = cameraBasis(azimuth, elevation);
      const inverseScale = 1 / Math.max(0.001, viewScaleRef.current);
      setPan((current) => add(
        current,
        add(scaleVector(basis.right, -dx * inverseScale), scaleVector(basis.up, dy * inverseScale)),
      ));
      return;
    }
    setAzimuth((current) => current - dx * 0.012);
    setElevation((current) => Math.max(-ORBIT_ELEVATION_LIMIT, Math.min(ORBIT_ELEVATION_LIMIT, current - dy * 0.012)));
  };

  return (
    <section className="ops-card ops-tf-card" aria-labelledby="ops-tf-heading">
      <header className="ops-card-heading">
        <div>
          <p>TF_STATIC</p>
          <h2 id="ops-tf-heading">공간 좌표계</h2>
          <span>{transforms.length ? `${transforms.length}개 고정 변환 · ${referenceFrame || "기준"} 기준 · 모든 프레임 XYZ 축` : "고정 변환 대기"}</span>
        </div>
        <div className="ops-heading-actions">
          <label className="ops-checkline"><input checked={showAllFrames} onChange={(event) => setShowAllFrames(event.target.checked)} type="checkbox" />전체 프레임</label>
          <button className="ops-icon-button" onClick={resetView} type="button" title="휴머노이드 기준 전체 맞춤" aria-label="휴머노이드 기준 좌표계 전체 맞춤"><RotateCcw size={16} /></button>
        </div>
      </header>
      <div className="ops-tf-toolbar" role="toolbar" aria-label="좌표계 보기 방향">
        {TF_VIEW_PRESETS.map((preset) => <button aria-pressed={activeViewPreset === preset.id} className="ops-button ops-tf-view-button" key={preset.id} onClick={() => applyViewPreset(preset)} title={preset.description} type="button">{preset.label}</button>)}
        <button className="ops-button ops-tf-fit-button" onClick={resetView} title="휴머노이드 원점으로 전체 맞춤" type="button">전체 맞춤</button>
      </div>
      <canvas
        ref={canvasRef}
        className="ops-tf-canvas"
        aria-label="휴머노이드 기준 tf_static 고정 좌표계 3차원 보기. 모든 프레임의 X Y Z 축이 표시됩니다. 좌클릭 드래그로 회전하고 Shift 또는 가운데나 오른쪽 드래그로 이동하며 휠로 확대합니다."
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={() => { pointerRef.current = null; }}
        onPointerCancel={() => { pointerRef.current = null; }}
        onContextMenu={(event) => event.preventDefault()}
        onDoubleClick={resetView}
        role="img"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setAzimuth((current) => current - 0.12);
          if (event.key === "ArrowRight") setAzimuth((current) => current + 0.12);
          if (event.key === "ArrowUp") setElevation((current) => Math.max(-ORBIT_ELEVATION_LIMIT, current - 0.12));
          if (event.key === "ArrowDown") setElevation((current) => Math.min(ORBIT_ELEVATION_LIMIT, current + 0.12));
          if (event.key === "Home") { event.preventDefault(); resetView(); }
        }}
      />
      <div className="ops-tf-legend" aria-label="좌표 축 범례"><span className="axis-x">X</span><span className="axis-y">Y</span><span className="axis-z">Z</span><span>모든 프레임의 로컬 축 · humanoid 원점 · 좌클릭 회전 · Shift/가운데/오른쪽 드래그 이동 · 휠 확대 · 더블클릭 전체 맞춤</span></div>
      <div className="ops-tf-tree" aria-label="고정 좌표계 목록">
        {transforms.length ? transforms.map((transform) => (
          <div key={transform.childFrame} className={isOperationalFrame(transform.childFrame) ? "operational" : ""}>
            <code>{transform.parentFrame}</code><span>→</span><code>{transform.childFrame}</code>
            <small>{transform.translation.x.toFixed(3)}, {transform.translation.y.toFixed(3)}, {transform.translation.z.toFixed(3)} m</small>
          </div>
        )) : <p>아직 받은 고정 변환이 없습니다. `world_anchor_node` 및 multicam launch 상태를 확인하세요.</p>}
      </div>
    </section>
  );
}

function CameraGrid({
  view,
  depthPresentation,
  frames,
  captureStatus,
  now,
}: {
  view: MulticamView;
  depthPresentation: DepthPresentation;
  frames: CameraFrames;
  captureStatus: CaptureStatus | null;
  now: number;
}) {
  const cameras = view === "depth" ? MULTICAM_CAMERAS.filter((camera) => camera.depthTopic) : MULTICAM_CAMERAS;
  return (
    <div className="ops-camera-grid" aria-label={view === "color" ? "동기화 컬러 카메라" : "동기화 깊이 카메라"}>
      {cameras.map((camera) => {
        const frame = frames[camera.id];
        const streamLive = isRecent(frame, now);
        const nodeOnline = Boolean(captureStatus?.online_cameras.includes(camera.id));
        const online = nodeOnline && streamLive;
        return (
          <article className={`ops-camera-card ${online ? "is-live" : "is-stale"}`} key={`${view}-${camera.id}`} data-camera-id={camera.id}>
            <header>
              <div><strong>{camera.label}</strong><span>{view === "color" ? "SYNCED COLOR" : "SYNCED DEPTH"}</span></div>
              <span className={`ops-status-dot ${statusTone(online, Boolean(frame) && !streamLive)}`} title={online ? "capture_status와 브라우저 프리뷰 모두 최신" : "드라이버 또는 동기화 스트림을 확인하세요."}>{online ? "LIVE" : streamLive ? "CHECK" : "WAIT"}</span>
            </header>
            <div className="ops-camera-media">
              {frame ? view === "depth" ? <DepthPreview cameraLabel={camera.label} frame={frame} presentation={depthPresentation} /> : <img src={frame.src} alt={`${camera.label} 동기화 컬러 프리뷰`} /> : <div className="ops-camera-empty"><Camera size={28} /><span>프레임 대기</span></div>}
            </div>
            <footer>
              <span>{frame ? `${frame.previewHz.toFixed(1)} Hz preview` : "수신 전"}</span>
              <span>{formatAge(frame?.receivedAt, now)}</span>
              <code title={view === "color" ? camera.colorTopic : camera.depthTopic || ""}>{frame?.frameId || "frame_id 대기"}</code>
            </footer>
          </article>
        );
      })}
    </div>
  );
}

function DepthPreview({ cameraLabel, frame, presentation }: { cameraLabel: string; frame: CameraFrame; presentation: DepthPresentation }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (presentation !== "visualized") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    const image = new Image();
    image.onload = () => {
      if (cancelled || !image.naturalWidth || !image.naturalHeight) return;
      const scale = Math.min(1, 480 / image.naturalWidth);
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      const histogram = new Uint32Array(256);
      for (let index = 0; index < pixels.data.length; index += 4) histogram[pixels.data[index]] += 1;
      const validPixels = canvas.width * canvas.height - histogram[0];
      if (!validPixels) return;
      const percentile = (fraction: number) => {
        const target = validPixels * fraction;
        let count = 0;
        for (let value = 1; value < histogram.length; value += 1) {
          count += histogram[value];
          if (count >= target) return value;
        }
        return 255;
      };
      const low = percentile(0.03);
      const high = Math.max(low + 1, percentile(0.98));
      for (let index = 0; index < pixels.data.length; index += 4) {
        const highByte = pixels.data[index];
        if (!highByte) continue;
        const intensity = Math.round(Math.max(0, Math.min(1, (highByte - low) / (high - low))) * 255);
        pixels.data[index] = intensity;
        pixels.data[index + 1] = intensity;
        pixels.data[index + 2] = intensity;
      }
      context.putImageData(pixels, 0, 0);
    };
    image.src = frame.src;
    return () => { cancelled = true; };
  }, [frame.src, presentation]);

  if (presentation === "raw") return <img src={frame.src} alt={`${cameraLabel} 동기화 깊이 원본 PNG 프리뷰`} />;
  return <canvas ref={canvasRef} aria-label={`${cameraLabel} 동기화 깊이 가시화 프리뷰`} role="img" />;
}

function CaptureStatusPanel({ status, colorFrames, now }: { status: CaptureStatus | null; colorFrames: CameraFrames; now: number }) {
  const fresh = Boolean(status && now - status.receivedAt < 3_000);
  return (
    <section className="ops-card ops-capture-card" aria-labelledby="ops-capture-heading">
      <header className="ops-card-heading">
        <div>
          <p>CAPTURE STATUS</p>
          <h2 id="ops-capture-heading">카메라 · 동기화 · USB 인벤토리</h2>
          <span>{status ? `${formatAge(status.receivedAt, now)} · /multicam_node/capture_status` : "상태 토픽 대기"}</span>
        </div>
        <span className={`ops-status-dot ${statusTone(Boolean(status?.all_cameras_online), Boolean(status) && !fresh)}`}>{status?.all_cameras_online && fresh ? "5/5 ONLINE" : "CHECK"}</span>
      </header>
      <div className="ops-kpi-grid">
        <Metric icon={Camera} label="온라인 카메라" value={`${status?.online_cameras.length || 0}/5`} detail={status?.offline_cameras.length ? `오프라인: ${status.offline_cameras.join(", ")}` : "동기화 입력 정상"} tone={status?.all_cameras_online ? "ok" : "warn"} />
        <Metric icon={Gauge} label="최근 동기화" value={status ? status.synced_frames.toLocaleString() : "—"} detail={status ? `최대 skew ${status.max_sync_skew_ms.toFixed(1)} ms` : "수신 대기"} tone={status && status.max_sync_skew_ms < 50 ? "ok" : "warn"} />
        <Metric icon={Activity} label="캡처 세션" value={status?.recording ? "REC" : "IDLE"} detail={status?.recording ? `${status.session_name || "이름 없음"} · ${formatDuration(status.elapsed_sec)}` : "현재 녹화 중이 아님"} tone={status?.recording ? "ok" : "idle"} />
        <Metric icon={Workflow} label="보정 준비" value={status?.ready_for_calibration ? "READY" : "HOLD"} detail={status?.hint || "상태 대기"} tone={status?.ready_for_calibration ? "ok" : "idle"} />
      </div>
      <div className="ops-inventory-note"><CircleAlert size={16} /><span><strong>USB 판정 기준:</strong> launch inventory의 카메라 ID/serial과 드라이버가 실제로 내보낸 `capture_status`·동기화 프레임을 함께 확인합니다. 케이블 링크 속도·포트 재열거 같은 물리 USB 상세는 원격 `cam_watch`/`preflight.sh`의 별도 검사 항목입니다.</span></div>
      <div className="ops-inventory-table-wrap">
        <table className="ops-table">
          <thead><tr><th>카메라</th><th>설정 ID</th><th>드라이버/USB 수신</th><th>프리뷰</th><th>정합 상태</th></tr></thead>
          <tbody>{MULTICAM_CAMERAS.map((camera) => {
            const frame = colorFrames[camera.id];
            const coverage = status?.cameras.find((item) => item.camera_name === camera.id);
            const driverOnline = Boolean(status?.online_cameras.includes(camera.id));
            return <tr key={camera.id}>
              <th scope="row">{camera.label}</th>
              <td><code>{camera.serial}</code></td>
              <td><span className={`ops-inline-status ${driverOnline ? "ok" : "warn"}`}>{driverOnline ? "driver online" : "not reported"}</span></td>
              <td>{frame ? `${frame.previewHz.toFixed(1)} Hz · ${formatAge(frame.receivedAt, now)}` : "수신 전"}</td>
              <td>{coverage ? `${coverage.detect_rate_hz.toFixed(1)} Hz tag · ${Math.round(coverage.area_coverage * 100)}%` : "coverage 대기"}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {status?.capture_dir ? <p className="ops-path"><span>저장 위치</span><code>{status.capture_dir}</code></p> : null}
    </section>
  );
}

function Metric({ icon: Icon, label, value, detail, tone }: { icon: typeof Camera; label: string; value: string; detail: string; tone: "ok" | "warn" | "idle" }) {
  return <article className={`ops-metric tone-${tone}`}><Icon size={17} aria-hidden="true" /><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></article>;
}

function WorldAnchorPanel({
  status,
  now,
  connected,
  pending,
  result,
  onAction,
}: {
  status: WorldAnchorStatus | null;
  now: number;
  connected: boolean;
  pending: WorldAction | null;
  result: WorldActionResult | null;
  onAction: (action: WorldAction) => Promise<WorldActionResult>;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const stale = Boolean(status && now - status.receivedAt > 3_000);
  const invoke = (action: WorldAction) => void onAction(action);
  const protectedDisabled = !connected || Boolean(pending) || !confirmed;
  return (
    <section className="ops-card ops-world-card" aria-labelledby="ops-world-heading">
      <header className="ops-card-heading">
        <div>
          <p>WORLD CONSOLE</p>
          <h2 id="ops-world-heading">World Anchor</h2>
          <span>{status ? `${formatAge(status.receivedAt, now)} · ${status.reference_frame || "reference 대기"} → ${status.world_frame || "world 대기"}` : "world_anchor_node status 대기"}</span>
        </div>
        <span className={`ops-status-dot ${statusTone(Boolean(status?.collecting), stale)}`}>{status?.collecting ? "COLLECTING" : stale ? "STALE" : "IDLE"}</span>
      </header>
      <p className="ops-world-message">{status?.message || "world_anchor_node의 상태 메시지를 기다리는 중입니다."}</p>
      <div className="ops-world-tags">
        {Object.entries(status?.tags || {}).map(([id, tag]) => <article key={id}><span>TAG {id} · {tag.role || "role 미지정"}</span><strong>{tag.total ?? 0}<small> samples</small></strong><p>{Object.entries(tag.per_camera || {}).map(([camera, value]) => `${camera}: ${value.count ?? 0}${value.fresh ? "" : " (stale)"}`).join(" · ") || "카메라 샘플 대기"}</p></article>)}
        {!status?.tags || !Object.keys(status.tags).length ? <p className="ops-empty-inline">태그 샘플 상태 대기</p> : null}
      </div>
      <div className="ops-world-actions">
        <button className="ops-button secondary" disabled={!connected || Boolean(pending) || Boolean(status?.collecting)} onClick={() => invoke("begin")} type="button"><Play size={16} />샘플 수집 시작</button>
        <button className="ops-button secondary" disabled={!connected || Boolean(pending) || !status?.collecting} onClick={() => invoke("stop")} type="button"><Square size={16} />수집 중지</button>
        <label className="ops-confirmation"><input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" /><span>태그가 움직이지 않는 기준점이며, 기존 월드 좌표계를 교체해도 안전함을 확인했습니다.</span></label>
        <button className="ops-button danger" disabled={protectedDisabled} onClick={() => invoke("solve")} type="button">{pending === "solve" ? <LoaderCircle className="ops-spin" size={16} /> : <Save size={16} />}Solve · 저장 · TF 발행</button>
        <button className="ops-button secondary" disabled={protectedDisabled} onClick={() => invoke("publish")} type="button">{pending === "publish" ? <LoaderCircle className="ops-spin" size={16} /> : <RefreshCw size={16} />}저장된 Anchor 다시 발행</button>
      </div>
      <p className="ops-control-boundary"><CircleAlert size={15} />`solve`와 `publish`는 `world → camera/tag` static TF를 바꿉니다. 로봇 구동 명령은 보내지 않지만, TF 소비 노드에는 영향을 줄 수 있습니다.</p>
      {result ? <p className={`ops-action-result ${result.success ? "ok" : "error"}`} role="status">{result.success ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}<span><strong>{result.action}</strong> · {result.message}</span></p> : null}
    </section>
  );
}

function TopicInspector({
  topics,
  selectedTopic,
  selectedType,
  sample,
  topicError,
  onSelect,
  onRefresh,
  now,
}: {
  topics: Array<{ name: string; type: string }>;
  selectedTopic: string;
  selectedType: string;
  sample: { receivedAt: number; hz: number; count: number; preview: string } | null;
  topicError: string;
  onSelect: (topic: string) => void;
  onRefresh: () => void;
  now: number;
}) {
  const [filter, setFilter] = useState("");
  const visible = useMemo(() => topics.filter((topic) => topic.name.toLowerCase().includes(filter.toLowerCase()) || topic.type.toLowerCase().includes(filter.toLowerCase())), [filter, topics]);
  return (
    <section className="ops-card ops-topic-card" aria-labelledby="ops-topic-heading">
      <header className="ops-card-heading">
        <div><p>ROS GRAPH</p><h2 id="ops-topic-heading">토픽 상태 · 내용 검사</h2><span>{topics.length ? `${topics.length}개 토픽 발견 · 선택한 토픽만 bounded sample` : "rosapi topic 목록 대기"}</span></div>
        <button className="ops-icon-button" onClick={onRefresh} type="button" title="ROS 토픽 목록 새로고침" aria-label="ROS 토픽 목록 새로고침"><RefreshCw size={16} /></button>
      </header>
      {topicError ? <p className="ops-topic-error"><CircleAlert size={15} />{topicError}</p> : null}
      <div className="ops-topic-controls"><input aria-label="토픽 필터" onChange={(event) => setFilter(event.target.value)} placeholder="토픽 또는 타입 필터" value={filter} /><select aria-label="검사할 ROS 토픽" onChange={(event) => onSelect(event.target.value)} value={selectedTopic}><option value="">토픽 선택</option>{topics.map((topic) => <option key={topic.name} value={topic.name}>{topic.name}</option>)}</select></div>
      <div className="ops-topic-workspace">
        <div className="ops-topic-list" role="list" aria-label="발견된 ROS 토픽">
          {visible.map((topic) => <button key={topic.name} className={topic.name === selectedTopic ? "selected" : ""} onClick={() => onSelect(topic.name)} role="listitem" type="button"><code>{topic.name}</code><span>{topic.type}</span></button>)}
          {!visible.length ? <p>일치하는 토픽이 없습니다.</p> : null}
        </div>
        <div className="ops-topic-preview">
          <div><span>선택</span><strong>{selectedTopic || "토픽 선택"}</strong><small>{selectedType || "type 대기"}</small></div>
          <p className={`ops-inline-status ${sample && now - sample.receivedAt < 3_000 ? "ok" : "warn"}`}>{sample ? `${sample.hz.toFixed(1)} Hz · ${formatAge(sample.receivedAt, now)}` : "메시지 수신 대기"}</p>
          <pre>{sample?.preview || "선택한 토픽의 최신 메시지를 안전한 크기로 표시합니다. 이미지/바이너리 payload는 크기만 표시됩니다."}</pre>
        </div>
      </div>
    </section>
  );
}

function validWebSocketUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "ws:" || url.protocol === "wss:") && !url.username && !url.password;
  } catch {
    return false;
  }
}

export function MulticamOpsWorkspace({ language, onExit }: { language: Language; onExit: () => void }) {
  const [view, setView] = useState<MulticamView>("color");
  const [depthPresentation, setDepthPresentation] = useState<DepthPresentation>("visualized");
  const bridge = useMulticamOpsBridge(view);
  const [draftUrl, setDraftUrl] = useState(bridge.url);
  const [urlError, setUrlError] = useState("");
  const now = useClock();

  useEffect(() => setDraftUrl(bridge.url), [bridge.url]);
  const connect = () => {
    if (!validWebSocketUrl(draftUrl.trim())) {
      setUrlError("ws:// 또는 wss:// URL을 입력하세요.");
      return;
    }
    setUrlError("");
    bridge.setUrl(draftUrl.trim());
  };
  const activeFrames = view === "color" ? bridge.colorFrames : bridge.depthFrames;

  return (
    <div className="app-shell ops-app-shell" data-slot="multicam-ops-workspace">
      <header className="ops-header">
        <div className="ops-brand"><button className="ops-back-button" onClick={onExit} type="button"><ArrowLeft size={18} />{language === "ko" ? "미션 화면" : "Mission"}</button><div><p>ARPA MULTICAM · ROS 2 OPERATIONS</p><h1>멀티캠 관제 콘솔</h1><span>동기화 영상, 고정 TF, Capture 상태 및 World Anchor를 하나의 ROSBridge 세션에서 확인합니다.</span></div></div>
        <div className="ops-connection"><div className={`ops-connection-state ${bridge.connected ? "ok" : "warn"}`}><Radio size={16} /><span>{bridge.connected ? "ROSBridge 연결" : "ROSBridge 대기"}</span></div><form onSubmit={(event) => { event.preventDefault(); connect(); }}><input aria-describedby={urlError ? "ops-bridge-url-error" : undefined} aria-invalid={Boolean(urlError)} aria-label="ROSBridge WebSocket URL" onChange={(event) => setDraftUrl(event.target.value)} value={draftUrl} /><button className="ops-icon-button" type="submit" title="이 ROSBridge에 연결" aria-label="이 ROSBridge에 연결"><Network size={16} /></button><button className="ops-icon-button" onClick={bridge.retry} type="button" title="현재 URL로 재연결" aria-label="현재 URL로 재연결"><RefreshCw size={16} /></button></form>{urlError ? <small className="ops-url-error" id="ops-bridge-url-error" role="alert">{urlError}</small> : <small aria-live="polite">{bridge.connectionMessage}</small>}</div>
      </header>

      {!bridge.connected ? <p className="ops-disconnected-banner"><CircleAlert size={16} />브리지 재연결 전에는 마지막 수신 내용을 신뢰하지 않으며 World Anchor 조작이 잠깁니다.</p> : null}

      <main className="ops-layout">
        <section className="ops-card ops-preview-card" aria-labelledby="ops-preview-heading">
          <header className="ops-card-heading">
            <div><p>SYNCED PREVIEW</p><h2 id="ops-preview-heading">주요 동기화 뷰</h2><span>{view === "color" ? "5개 /synced color stream · 토픽 수신 프레임을 원본 그대로 표시" : depthPresentation === "visualized" ? "D455 4대의 /synced compressedDepth stream · 원본 거리값을 화면 대비로만 가시화" : "D455 4대의 /synced compressedDepth stream · 토픽 원본 PNG 표시 · FLIR은 depth 센서가 없습니다."}</span></div>
            <div className="ops-preview-actions"><div className="ops-segmented" role="tablist" aria-label="영상 유형"><button aria-selected={view === "color"} className={view === "color" ? "active" : ""} onClick={() => setView("color")} role="tab" type="button"><Eye size={15} />Color</button><button aria-selected={view === "depth"} className={view === "depth" ? "active" : ""} onClick={() => setView("depth")} role="tab" type="button"><Box size={15} />Depth</button></div>{view === "depth" ? <div className="ops-segmented ops-depth-presentation" aria-label="Depth 표시 방식" role="group"><button aria-pressed={depthPresentation === "visualized"} className={depthPresentation === "visualized" ? "active" : ""} onClick={() => setDepthPresentation("visualized")} type="button">가시화</button><button aria-pressed={depthPresentation === "raw"} className={depthPresentation === "raw" ? "active" : ""} onClick={() => setDepthPresentation("raw")} type="button">원본 PNG</button></div> : null}</div>
          </header>
          <CameraGrid captureStatus={bridge.captureStatus} depthPresentation={depthPresentation} frames={activeFrames} now={now} view={view} />
        </section>
        <TfScene transforms={bridge.tfTransforms} />
        <WorldAnchorPanel connected={bridge.connected} now={now} onAction={bridge.callWorldAction} pending={bridge.worldActionPending} result={bridge.worldActionResult} status={bridge.worldStatus} />
        <CaptureStatusPanel colorFrames={bridge.colorFrames} now={now} status={bridge.captureStatus} />
        <TopicInspector now={now} onRefresh={bridge.refreshTopics} onSelect={bridge.setSelectedTopic} sample={bridge.selectedTopicSample} selectedTopic={bridge.selectedTopic} selectedType={bridge.selectedTopicType} topicError={bridge.topicError} topics={bridge.topics} />
      </main>
    </div>
  );
}
