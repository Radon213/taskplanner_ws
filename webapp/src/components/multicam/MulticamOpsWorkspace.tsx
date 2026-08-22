import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent,
} from "react";
import { LayoutGroup } from "framer-motion";
import * as m from "framer-motion/m";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
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
  CAPTURE_STATUS_MAX_AGE_MS,
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
import { silk } from "../../motion-system";

type Language = "ko" | "en";
type DepthPresentation = "visualized" | "raw";

type FramePose = {
  frame: string;
  parentFrame: string | null;
  position: Vec3;
  rotation: Quaternion;
  source: TfTransformSource;
};

type Vec3 = { x: number; y: number; z: number };
type Quaternion = { x: number; y: number; z: number; w: number };
type TfPointerDrag = { id: number; x: number; y: number; mode: "orbit" | "pan" };
type TfViewPreset = { id: string; label: string; azimuth: number; elevation: number; description: string };
type TfModelStatus = "loading" | "ready" | "error";

/**
 * Static and dynamic messages share the ROS TFMessage wire shape. The source
 * remains explicit all the way into the scene so a moving `/tf` frame is never
 * presented as a calibration/static transform.
 */
export type TfTransformSource = "static" | "dynamic";
export type TfSceneTransform = StaticTransform & {
  source?: TfTransformSource;
  receivedAt?: number;
  sourceStamp?: string;
};

const IDENTITY: Quaternion = { x: 0, y: 0, z: 0, w: 1 };
const ORIGIN: Vec3 = { x: 0, y: 0, z: 0 };
const ORBIT_ELEVATION_LIMIT = Math.PI / 2 - 0.01;
const ISOMETRIC_VIEW = { azimuth: -0.78, elevation: 0.48 };
const TF_MODEL_URL = "/models/humanoid-tray-tag1.glb";
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
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") setNow(Date.now());
    };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, intervalMs);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
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

function buildFramePoses(transforms: readonly TfSceneTransform[]): FramePose[] {
  if (!transforms.length) return [];
  const byChild = new Map(transforms.map((transform) => [transform.childFrame, transform]));
  const parents = new Set(transforms.map((transform) => transform.parentFrame));
  const roots = [...parents].filter((frame) => !byChild.has(frame));
  const poses = new Map<string, FramePose>();
  for (const root of roots) {
    const rootSource = transforms.find((transform) => transform.parentFrame === root)?.source ?? "static";
    poses.set(root, { frame: root, parentFrame: null, position: ORIGIN, rotation: IDENTITY, source: rootSource });
  }
  if (!poses.size) {
    poses.set(transforms[0].parentFrame, {
      frame: transforms[0].parentFrame,
      parentFrame: null,
      position: ORIGIN,
      rotation: IDENTITY,
      source: transforms[0].source ?? "static",
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
        source: transform.source ?? "static",
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
        source: transform.source ?? "static",
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

export const TfScene = memo(function TfScene({
  transforms,
  showTransformTree = true,
}: {
  transforms: readonly TfSceneTransform[];
  /** Debug's TF tab renders static and dynamic lists separately below the scene. */
  showTransformTree?: boolean;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const labelLayerRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<TfPointerDrag | null>(null);
  const viewScaleRef = useRef(1);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const framesRootRef = useRef<THREE.Group | null>(null);
  const modelAnchorRef = useRef<THREE.Group | null>(null);
  const modelRef = useRef<THREE.Object3D | null>(null);
  const modelMeshCountRef = useRef(0);
  const renderSceneRef = useRef<() => void>(() => undefined);
  const sceneExtentRef = useRef(1);
  const [azimuth, setAzimuth] = useState(ISOMETRIC_VIEW.azimuth);
  const [elevation, setElevation] = useState(ISOMETRIC_VIEW.elevation);
  const [pan, setPan] = useState<Vec3>(ORIGIN);
  const [zoom, setZoom] = useState(1);
  const [showAllFrames, setShowAllFrames] = useState(true);
  const [showModel, setShowModel] = useState(true);
  const [modelStatus, setModelStatus] = useState<TfModelStatus>("loading");
  const staticTransformCount = transforms.filter((transform) => (transform.source ?? "static") === "static").length;
  const dynamicTransformCount = transforms.length - staticTransformCount;
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
  const tagPose = useMemo(
    () => humanoidRelativePoses.find((pose) => pose.frame.toLowerCase() === "tag1") || null,
    [humanoidRelativePoses],
  );
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
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setZoom((current) => Math.max(0.5, Math.min(2.2, current - event.deltaY * 0.001)));
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    let disposed = false;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch (error) {
      console.error("TF WebGL renderer initialization failed", error);
      setModelStatus("error");
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.setClearColor(0x000000, 0);
    renderer.domElement.setAttribute("aria-hidden", "true");
    viewport.prepend(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 100);
    camera.up.set(0, 0, 1);
    const framesRoot = new THREE.Group();
    const modelAnchor = new THREE.Group();
    scene.add(framesRoot, modelAnchor);
    scene.add(new THREE.HemisphereLight(0xe8f6ff, 0x263038, 2.1));
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.5);
    keyLight.position.set(4, -3, 7);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0x8fc8ff, 1.3);
    fillLight.position.set(-4, 2, 2);
    scene.add(fillLight);

    rendererRef.current = renderer;
    sceneRef.current = scene;
    cameraRef.current = camera;
    framesRootRef.current = framesRoot;
    modelAnchorRef.current = modelAnchor;

    const resize = () => {
      const bounds = viewport.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      renderer.setSize(bounds.width, bounds.height, false);
      camera.aspect = bounds.width / bounds.height;
      camera.updateProjectionMatrix();
      renderSceneRef.current();
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(viewport);
    resize();

    const loadModel = () => {
      const loader = new GLTFLoader();
      loader.load(
        TF_MODEL_URL,
        (gltf) => {
          if (disposed) {
            gltf.scene.traverse((object) => {
              if (!(object instanceof THREE.Mesh)) return;
              object.geometry.dispose();
              const materials = Array.isArray(object.material) ? object.material : [object.material];
              for (const material of materials) material.dispose();
            });
            return;
          }
          modelRef.current = gltf.scene;
          gltf.scene.name = "humanoid-tray-tag1";
          let meshCount = 0;
          gltf.scene.traverse((object) => {
            if (!(object instanceof THREE.Mesh)) return;
            meshCount += 1;
            object.frustumCulled = true;
            const materials = Array.isArray(object.material) ? object.material : [object.material];
            for (const material of materials) {
              if ("roughness" in material && typeof material.roughness === "number") material.roughness = Math.max(0.42, material.roughness);
            }
          });
          modelMeshCountRef.current = meshCount;
          setModelStatus("ready");
        },
        undefined,
        (error) => {
          if (disposed) return;
          console.error("TF model load failed", error);
          setModelStatus("error");
        },
      );
    };
    let modelObserver: IntersectionObserver | null = null;
    if ("IntersectionObserver" in window) {
      modelObserver = new IntersectionObserver((entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        modelObserver?.disconnect();
        modelObserver = null;
        loadModel();
      }, { rootMargin: "0px" });
      modelObserver.observe(viewport);
    } else {
      loadModel();
    }

    return () => {
      disposed = true;
      resizeObserver.disconnect();
      modelObserver?.disconnect();
      if (modelRef.current) {
        modelRef.current.traverse((object) => {
          if (!(object instanceof THREE.Mesh)) return;
          object.geometry.dispose();
          const materials = Array.isArray(object.material) ? object.material : [object.material];
          for (const material of materials) material.dispose();
        });
      }
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
      rendererRef.current = null;
      sceneRef.current = null;
      cameraRef.current = null;
      framesRootRef.current = null;
      modelAnchorRef.current = null;
      modelRef.current = null;
    };
  }, []);

  useEffect(() => {
    const framesRoot = framesRootRef.current;
    const modelAnchor = modelAnchorRef.current;
    if (!framesRoot || !modelAnchor) return;
    for (const child of [...framesRoot.children]) {
      framesRoot.remove(child);
      child.traverse((object) => {
        if (!(object instanceof THREE.Line || object instanceof THREE.LineSegments || object instanceof THREE.Mesh)) return;
        object.geometry.dispose();
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        for (const material of materials) material.dispose();
      });
    }
    modelAnchor.clear();
    modelAnchor.position.set(0, 0, 0);
    modelAnchor.quaternion.identity();
    const viewport = viewportRef.current;
    if (viewport) {
      viewport.dataset.modelState = showModel ? modelStatus : "hidden";
      delete viewport.dataset.modelBounds;
      delete viewport.dataset.modelMeshCount;
    }

    let maxExtent = Math.max(0.75, ...poses.flatMap((pose) => [
      Math.abs(pose.position.x),
      Math.abs(pose.position.y),
      Math.abs(pose.position.z),
    ]));
    const gridSize = maxExtent * 2.2;
    const grid = new THREE.GridHelper(gridSize, 12, 0x49616b, 0x293a42);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = 0;
    framesRoot.add(grid);

    const byFrame = new Map(poses.map((pose) => [pose.frame, pose]));
    const linkPositions: number[] = [];
    for (const pose of poses) {
      if (!pose.parentFrame) continue;
      const parent = byFrame.get(pose.parentFrame);
      if (!parent) continue;
      linkPositions.push(
        parent.position.x, parent.position.y, parent.position.z,
        pose.position.x, pose.position.y, pose.position.z,
      );
    }
    if (linkPositions.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(linkPositions, 3));
      framesRoot.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x77909b, transparent: true, opacity: 0.62 })));
    }

    const axisLength = Math.max(0.12, Math.min(0.38, maxExtent * 0.14));
    for (const pose of poses) {
      const isReferenceFrame = pose.frame === referenceFrame;
      const axes = new THREE.AxesHelper(axisLength);
      axes.position.set(pose.position.x, pose.position.y, pose.position.z);
      axes.quaternion.set(pose.rotation.x, pose.rotation.y, pose.rotation.z, pose.rotation.w);
      const material = axes.material as THREE.LineBasicMaterial;
      material.transparent = true;
      material.opacity = isReferenceFrame ? 1 : isOperationalFrame(pose.frame) ? 0.9 : 0.68;
      if (isReferenceFrame) {
        material.depthTest = false;
        material.depthWrite = false;
        axes.renderOrder = 1_000;
      }
      framesRoot.add(axes);

      const dotGeometry = new THREE.SphereGeometry(isReferenceFrame ? axisLength * 0.075 : axisLength * 0.05, 10, 8);
      const dotColor = isReferenceFrame ? 0x9cf7ed : pose.frame === "world" || /^tag\d+$/i.test(pose.frame) ? 0x75a7ff : isOperationalFrame(pose.frame) ? 0x70ddd1 : 0x9eabb1;
      const dotMaterial = new THREE.MeshBasicMaterial({
        color: dotColor,
        depthTest: !isReferenceFrame,
        depthWrite: !isReferenceFrame,
      });
      const dot = new THREE.Mesh(dotGeometry, dotMaterial);
      dot.position.copy(axes.position);
      if (isReferenceFrame) dot.renderOrder = 1_002;
      framesRoot.add(dot);

      if (isReferenceFrame) {
        const halo = new THREE.Mesh(
          new THREE.SphereGeometry(axisLength * 0.16, 16, 12),
          new THREE.MeshBasicMaterial({
            color: 0x70ddd1,
            depthTest: false,
            depthWrite: false,
            opacity: 0.18,
            transparent: true,
          }),
        );
        halo.position.copy(axes.position);
        halo.renderOrder = 1_001;
        framesRoot.add(halo);
      }
    }

    if (showModel && modelStatus === "ready" && modelRef.current && tagPose) {
      modelAnchor.position.set(tagPose.position.x, tagPose.position.y, tagPose.position.z);
      modelAnchor.quaternion.set(tagPose.rotation.x, tagPose.rotation.y, tagPose.rotation.z, tagPose.rotation.w);
      modelAnchor.add(modelRef.current);
      const box = new THREE.Box3().setFromObject(modelAnchor);
      if (!box.isEmpty()) {
        maxExtent = Math.max(
          maxExtent,
          Math.abs(box.min.x), Math.abs(box.min.y), Math.abs(box.min.z),
          Math.abs(box.max.x), Math.abs(box.max.y), Math.abs(box.max.z),
        );
        if (viewport) {
          viewport.dataset.modelBounds = [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z]
            .map((value) => value.toFixed(3))
            .join(",");
          viewport.dataset.modelMeshCount = String(modelMeshCountRef.current);
        }
      }
    }
    sceneExtentRef.current = Math.max(0.75, maxExtent);
    renderSceneRef.current();
  }, [modelStatus, poses, referenceFrame, showModel, tagPose]);

  useEffect(() => {
    renderSceneRef.current = () => {
      const renderer = rendererRef.current;
      const scene = sceneRef.current;
      const camera = cameraRef.current;
      const viewport = viewportRef.current;
      if (!renderer || !scene || !camera || !viewport) return;
      const bounds = viewport.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      const maxExtent = sceneExtentRef.current;
      const basis = cameraBasis(azimuth, elevation);
      const eyeDirection = scaleVector(basis.forward, -1);
      const cameraDistance = maxExtent * 3.45 / zoom;
      camera.position.set(
        pan.x + eyeDirection.x * cameraDistance,
        pan.y + eyeDirection.y * cameraDistance,
        pan.z + eyeDirection.z * cameraDistance,
      );
      camera.up.set(basis.up.x, basis.up.y, basis.up.z);
      camera.near = Math.max(0.005, cameraDistance / 1000);
      camera.far = Math.max(100, cameraDistance * 20);
      camera.lookAt(pan.x, pan.y, pan.z);
      camera.updateProjectionMatrix();
      viewScaleRef.current = bounds.height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * cameraDistance);
      renderer.render(scene, camera);

      const labelLayer = labelLayerRef.current;
      if (!labelLayer) return;
      for (const element of labelLayer.querySelectorAll<HTMLElement>("[data-tf-frame]")) {
        const pose = byFrameForLabels.get(element.dataset.tfFrame || "");
        if (!pose) {
          element.hidden = true;
          continue;
        }
        const projected = new THREE.Vector3(pose.position.x, pose.position.y, pose.position.z).project(camera);
        const visible = projected.z >= -1 && projected.z <= 1;
        element.hidden = !visible;
        if (!visible) continue;
        const x = (projected.x * 0.5 + 0.5) * bounds.width;
        const y = (-projected.y * 0.5 + 0.5) * bounds.height;
        element.style.transform = `translate3d(${Math.round(x + 7)}px, ${Math.round(y - 7)}px, 0)`;
      }
    };
    renderSceneRef.current();
  }, [azimuth, elevation, pan, poses, zoom]);

  const byFrameForLabels = useMemo(() => new Map(poses.map((pose) => [pose.frame, pose])), [poses]);

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
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
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
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
          <p>{dynamicTransformCount ? "TF · STATIC + DYNAMIC" : "TF_STATIC"}</p>
          <h2 id="ops-tf-heading">공간 좌표계</h2>
          <span>{transforms.length
            ? `${staticTransformCount}개 고정 · ${dynamicTransformCount}개 동적 변환 · ${referenceFrame || "기준"} 기준 · 모든 프레임 XYZ 축 · tag1 모델 정합`
            : "고정·동적 변환 대기"}</span>
        </div>
        <div className="ops-heading-actions">
          <span aria-live="polite" className={`ops-model-chip ${modelStatus === "error" ? "error" : modelStatus === "ready" && tagPose ? "ready" : "loading"}`} role="status">
            MODEL {modelStatus === "error" ? "ERROR" : modelStatus !== "ready" ? "LOADING" : tagPose ? "TAG1" : "TF WAIT"}
          </span>
          <label className="ops-checkline"><input checked={showModel} onChange={(event) => setShowModel(event.target.checked)} type="checkbox" />3D 모델</label>
          <label className="ops-checkline"><input checked={showAllFrames} onChange={(event) => setShowAllFrames(event.target.checked)} type="checkbox" />전체 프레임</label>
          <button className="ops-icon-button" onClick={resetView} type="button" title="휴머노이드 기준 전체 맞춤" aria-label="휴머노이드 기준 좌표계 전체 맞춤"><RotateCcw size={16} /></button>
        </div>
      </header>
      <div className="ops-tf-toolbar" role="toolbar" aria-label="좌표계 보기 방향">
        {TF_VIEW_PRESETS.map((preset) => <button aria-label={preset.description} aria-pressed={activeViewPreset === preset.id} className="ops-button ops-tf-view-button" key={preset.id} onClick={() => applyViewPreset(preset)} title={preset.description} type="button">{preset.label}</button>)}
        <button aria-label="휴머노이드 원점으로 전체 맞춤" className="ops-button ops-tf-fit-button" onClick={resetView} title="휴머노이드 원점으로 전체 맞춤" type="button">전체 맞춤</button>
      </div>
      <div
        ref={viewportRef}
        className="ops-tf-canvas"
        aria-label="휴머노이드 또는 수신된 기준 프레임 기준 좌표계와 tag1 기준 컬러 모델 3차원 보기. 정적 tf_static과 동적 tf 프레임은 별도 색상으로 표시되며, 모든 프레임의 X Y Z 축이 표시됩니다. 좌클릭 드래그로 회전하고 Shift 또는 가운데나 오른쪽 드래그로 이동하며 휠로 확대합니다."
        aria-describedby="ops-tf-controls-help"
        aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Home"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={() => { pointerRef.current = null; }}
        onPointerCancel={() => { pointerRef.current = null; }}
        onContextMenu={(event) => event.preventDefault()}
        onDoubleClick={resetView}
        role="img"
        tabIndex={0}
        onKeyDown={(event) => {
          if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(event.key)) event.preventDefault();
          if (event.key === "ArrowLeft") setAzimuth((current) => current - 0.12);
          if (event.key === "ArrowRight") setAzimuth((current) => current + 0.12);
          if (event.key === "ArrowUp") setElevation((current) => Math.max(-ORBIT_ELEVATION_LIMIT, current - 0.12));
          if (event.key === "ArrowDown") setElevation((current) => Math.min(ORBIT_ELEVATION_LIMIT, current + 0.12));
          if (event.key === "Home") resetView();
        }}
      >
        {!poses.length && <div className="ops-tf-empty">/tf_static 또는 /tf 수신을 기다리는 중입니다.</div>}
        <div ref={labelLayerRef} className="ops-tf-label-layer" aria-hidden="true">
          {poses.map((pose) => (
            <span
              className={[
                pose.frame === referenceFrame ? "reference" : "",
                pose.frame === "world" || /^tag\d+$/i.test(pose.frame) ? "anchor" : isOperationalFrame(pose.frame) ? "operational" : "",
                pose.source === "dynamic" ? "dynamic" : "static",
              ].filter(Boolean).join(" ")}
              data-tf-frame={pose.frame}
              key={pose.frame}
            >{labelForFrame(pose.frame)}</span>
          ))}
        </div>
      </div>
      <div className="ops-tf-legend" id="ops-tf-controls-help" aria-label="좌표 축 범례"><span className="axis-x">X</span><span className="axis-y">Y</span><span className="axis-z">Z</span><span className="static">STATIC /tf_static</span><span className="dynamic">DYNAMIC /tf</span><span>모든 프레임의 로컬 축 · 상대 변환만 표시하며 world·robot 정합은 별도 보정 증거가 필요 · 컬러 모델은 tag1 원점 정합 · 좌클릭 회전 · Shift/가운데/오른쪽 드래그 이동 · 휠 확대 · 방향키 회전 · Home/더블클릭 전체 맞춤</span></div>
      {showTransformTree ? <div className="ops-tf-tree" aria-label="좌표계 목록">
        {transforms.length ? transforms.map((transform) => (
          <div key={transform.childFrame} className={isOperationalFrame(transform.childFrame) ? "operational" : ""}>
            <code>{transform.parentFrame}</code><span>→</span><code>{transform.childFrame}</code>
            <small><b className={(transform.source ?? "static") === "dynamic" ? "dynamic" : "static"}>{(transform.source ?? "static") === "dynamic" ? "DYNAMIC /tf" : "STATIC /tf_static"}</b> · {transform.translation.x.toFixed(3)}, {transform.translation.y.toFixed(3)}, {transform.translation.z.toFixed(3)} m</small>
          </div>
        )) : <p>아직 받은 변환이 없습니다. `/tf_static` 또는 `/tf` 발행 상태를 확인하세요.</p>}
      </div> : null}
    </section>
  );
});

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
    <div
      aria-label={view === "color" ? "동기화 컬러 카메라" : "동기화 깊이 카메라"}
      aria-labelledby={`multicam-tab-${view}`}
      className="ops-camera-grid"
      id="multicam-camera-panel"
      role="tabpanel"
    >
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

function CaptureStatusPanel({ language, status, colorFrames, now }: { language: Language; status: CaptureStatus | null; colorFrames: CameraFrames; now: number }) {
  const fresh = Boolean(status && now - status.receivedAt <= CAPTURE_STATUS_MAX_AGE_MS);
  const lastStatusPrefix = language === "ko" ? "마지막 상태" : "Last status";
  const freshWaitLabel = language === "ko" ? "새 상태 대기" : "waiting for fresh status";
  const statusDetail = (detail: string): string => {
    if (!status) return language === "ko" ? "수신 대기" : "Waiting for status";
    return fresh ? detail : `${lastStatusPrefix} · ${formatAge(status.receivedAt, now)} · ${freshWaitLabel}`;
  };
  const statusToneFor = (tone: "ok" | "warn" | "idle"): "ok" | "warn" | "idle" => {
    if (!status) return "idle";
    return fresh ? tone : "warn";
  };
  const columnLabels = language === "ko"
    ? { camera: "카메라", config: "설정 ID", driver: "드라이버/USB 수신", preview: "프리뷰", alignment: "정합 상태" }
    : { camera: "Camera", config: "Config ID", driver: "Driver / USB", preview: "Preview", alignment: "Alignment" };
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
        <Metric icon={Camera} label="온라인 카메라" value={`${status?.online_cameras.length || 0}/5`} detail={statusDetail(status?.offline_cameras.length ? `오프라인: ${status.offline_cameras.join(", ")}` : "동기화 입력 정상")} tone={statusToneFor(status?.all_cameras_online ? "ok" : "warn")} />
        <Metric icon={Gauge} label="최근 동기화" value={status ? status.synced_frames.toLocaleString() : "—"} detail={statusDetail(status ? `최대 skew ${status.max_sync_skew_ms.toFixed(1)} ms` : "수신 대기")} tone={statusToneFor(status && status.max_sync_skew_ms < 50 ? "ok" : "warn")} />
        <Metric icon={Activity} label="캡처 세션" value={status?.recording ? "REC" : "IDLE"} detail={statusDetail(status?.recording ? `${status.session_name || "이름 없음"} · ${formatDuration(status.elapsed_sec)}` : "현재 녹화 중이 아님")} tone={statusToneFor(status?.recording ? "ok" : "idle")} />
        <Metric icon={Workflow} label="보정 준비" value={status?.ready_for_calibration ? "READY" : "HOLD"} detail={statusDetail(status?.hint || "상태 대기")} tone={statusToneFor(status?.ready_for_calibration ? "ok" : "idle")} />
      </div>
      <div className="ops-inventory-note"><CircleAlert size={16} /><span><strong>USB 판정 기준:</strong> launch inventory의 카메라 ID/serial과 드라이버가 실제로 내보낸 `capture_status`·동기화 프레임을 함께 확인합니다. 케이블 링크 속도·포트 재열거 같은 물리 USB 상세는 원격 `cam_watch`/`preflight.sh`의 별도 검사 항목입니다.</span></div>
      <div className="ops-inventory-table-wrap">
        <table className="ops-table">
          <thead><tr><th>{columnLabels.camera}</th><th>{columnLabels.config}</th><th>{columnLabels.driver}</th><th>{columnLabels.preview}</th><th>{columnLabels.alignment}</th></tr></thead>
          <tbody>{MULTICAM_CAMERAS.map((camera) => {
            const frame = colorFrames[camera.id];
            const coverage = status?.cameras.find((item) => item.camera_name === camera.id);
            const driverOnline = Boolean(status?.online_cameras.includes(camera.id));
            const frameFresh = Boolean(frame && now - frame.receivedAt < 3_000);
            const driverLabel = !status
              ? "not reported"
              : fresh
                ? driverOnline ? "driver online" : "not reported"
                : `${lastStatusPrefix} · ${driverOnline ? "driver online" : "not reported"}`;
            const previewLabel = frame
              ? `${frame.previewHz.toFixed(1)} Hz · ${formatAge(frame.receivedAt, now)}`
              : "수신 전";
            const coverageLabel = coverage
              ? `${coverage.detect_rate_hz.toFixed(1)} Hz tag · ${Math.round(coverage.area_coverage * 100)}%`
              : "coverage 대기";
            return <tr key={camera.id}>
              <th data-label={columnLabels.camera} scope="row">{camera.label}</th>
              <td data-label={columnLabels.config}><code>{camera.serial}</code></td>
              <td data-label={columnLabels.driver}><span className={`ops-inline-status ${fresh && driverOnline ? "ok" : "warn"}`}>{driverLabel}</span></td>
              <td data-label={columnLabels.preview}><span className={`ops-inline-status ${frameFresh ? "ok" : "warn"}`}>{previewLabel}</span></td>
              <td data-label={columnLabels.alignment}><span className={`ops-inline-status ${fresh && coverage ? "ok" : "warn"}`}>{fresh ? coverageLabel : coverage ? `${lastStatusPrefix} · ${coverageLabel}` : coverageLabel}</span></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {status?.capture_dir ? <p className="ops-path"><span>저장 위치</span><code aria-label="캡처 저장 위치" tabIndex={0}>{status.capture_dir}</code></p> : null}
    </section>
  );
}

function Metric({ icon: Icon, label, value, detail, tone }: { icon: typeof Camera; label: string; value: string; detail: string; tone: "ok" | "warn" | "idle" }) {
  return <article className={`ops-metric tone-${tone}`}><Icon size={17} aria-hidden="true" /><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></article>;
}

function WorldAnchorPanel({
  status,
  now,
  pending,
  result,
}: {
  status: WorldAnchorStatus | null;
  now: number;
  pending: WorldAction | null;
  result: WorldActionResult | null;
}) {
  const stale = Boolean(status && now - status.receivedAt > 3_000);
  return (
    <section className="ops-card ops-world-card" aria-labelledby="ops-world-heading">
      <header className="ops-card-heading">
        <div>
          <p>WORLD CONSOLE</p>
          <h2 id="ops-world-heading">World Anchor</h2>
          <span>{status ? `${formatAge(status.receivedAt, now)} · ${status.reference_frame || "reference 대기"} → ${status.world_frame || "world 대기"}` : "world_anchor_node status 대기"}</span>
        </div>
        <span className={`ops-status-dot ${statusTone(Boolean(status?.collecting), stale)}`}>{stale ? "STALE" : status?.collecting ? "COLLECTING" : "IDLE"}</span>
      </header>
      <p className={`ops-world-message ${stale ? "is-stale" : ""}`}>{status ? stale ? `마지막 상태 · ${status.message || "상태 메시지 없음"} · 새 상태 대기` : status.message || "상태 메시지 없음" : "world_anchor_node의 상태 메시지를 기다리는 중입니다."}</p>
      <div className={`ops-world-tags ${stale ? "is-stale" : ""}`}>
        {Object.entries(status?.tags || {}).map(([id, tag]) => <article key={id}><span>TAG {id} · {tag.role || "role 미지정"}</span><strong>{tag.total ?? 0}<small> samples</small></strong><p>{Object.entries(tag.per_camera || {}).map(([camera, value]) => `${camera}: ${value.count ?? 0}${value.fresh ? "" : " (stale)"}`).join(" · ") || "카메라 샘플 대기"}</p></article>)}
        {!status?.tags || !Object.keys(status.tags).length ? <p className="ops-empty-inline">태그 샘플 상태 대기</p> : null}
      </div>
      <div className="ops-world-actions">
        <button className="ops-button secondary" disabled type="button"><Play size={16} />샘플 수집 시작</button>
        <button className="ops-button secondary" disabled type="button"><Square size={16} />수집 중지</button>
        <label className="ops-confirmation"><input disabled type="checkbox" /><span>Observer 화면에서는 World Anchor 변경을 승인하거나 실행할 수 없습니다.</span></label>
        <button className="ops-button danger" disabled type="button">{pending === "solve" ? <LoaderCircle className="ops-spin" size={16} /> : <Save size={16} />}Solve · 저장 · TF 발행</button>
        <button className="ops-button secondary" disabled type="button">{pending === "publish" ? <LoaderCircle className="ops-spin" size={16} /> : <RefreshCw size={16} />}저장된 Anchor 다시 발행</button>
      </div>
      <p className="ops-control-boundary"><CircleAlert size={15} />전용 observer는 read-only입니다. 이 화면은 World Anchor Trigger 서비스를 호출하지 않으며 실행 중인 런타임을 변경하지 않습니다.</p>
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
          {visible.map((topic) => (
            <div key={topic.name} role="listitem">
              <button className={topic.name === selectedTopic ? "selected" : ""} onClick={() => onSelect(topic.name)} type="button"><code>{topic.name}</code><span>{topic.type}</span></button>
            </div>
          ))}
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

export function MulticamOpsWorkspace({
  language,
  onExit,
  embedded = false,
}: {
  language: Language;
  onExit?: () => void;
  embedded?: boolean;
}) {
  const mainId = embedded ? "debug-multicam-main" : "multicam-main";
  // A standalone workspace replaces its navigation trigger, so restore
  // keyboard/screen-reader focus to its landmark. Embedded Debug keeps focus
  // on the outer roving tab; moving it into this inner region would break
  // arrow-key tab navigation.
  useEffect(() => {
    if (embedded) return;
    const focusFrame = window.requestAnimationFrame(() => {
      document.getElementById(mainId)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [embedded, mainId]);
  const [view, setView] = useState<MulticamView>("color");
  const [depthPresentation, setDepthPresentation] = useState<DepthPresentation>("visualized");
  // The Debug TF tab owns its own `/tf_static` + `/tf` subscriptions through
  // the Debug bridge. Do not keep a duplicate static-transform subscription in
  // the embedded multicam observer.
  const bridge = useMulticamOpsBridge(view, { observeStaticTf: !embedded });
  const now = useClock();
  const handleViewKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, current: MulticamView) => {
    const options: MulticamView[] = ["color", "depth"];
    const currentIndex = options.indexOf(current);
    const nextIndex = event.key === "ArrowRight"
      ? (currentIndex + 1) % options.length
      : event.key === "ArrowLeft"
        ? (currentIndex - 1 + options.length) % options.length
        : event.key === "Home"
          ? 0
          : event.key === "End"
            ? options.length - 1
            : null;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = options[nextIndex];
    setView(next);
    window.requestAnimationFrame(() => document.getElementById(`multicam-tab-${next}`)?.focus());
  };
  const activeFrames = view === "color" ? bridge.colorFrames : bridge.depthFrames;
  const freshFrameCount = Object.values(activeFrames).filter(
    (frame) => frame && now - frame.receivedAt <= 3_000,
  ).length;

  const observerConnection = (
    <div className="ops-connection">
      <div className="ops-observer-signals" aria-label="멀티캠 observer 상태">
        <div className={`ops-connection-state ${bridge.socketConnected ? "ok" : "warn"}`}><Radio size={16} /><span>Transport {bridge.socketConnected ? "연결" : "대기"}</span></div>
        <div className={`ops-connection-state ${bridge.captureTopicDiscovered ? "ok" : "warn"}`}><Activity size={16} /><span>Graph topic {bridge.captureTopicDiscovered ? "발견" : "미발견"}</span></div>
        <div className={`ops-connection-state ${bridge.captureStatusFresh ? "ok" : "warn"}`}><Gauge size={16} /><span>CaptureStatus {bridge.captureStatusFresh ? "fresh" : bridge.captureStatus ? "stale" : "대기"}</span></div>
      </div>
      <div className="ops-observer-endpoint"><code title={bridge.url}>{bridge.url}</code><button className="ops-icon-button" onClick={bridge.retry} type="button" title="멀티캠 observer 재연결" aria-label="멀티캠 observer 재연결"><RefreshCw size={16} /></button></div>
      <small aria-live="polite">{bridge.connectionMessage} · {view} frame fresh {freshFrameCount}/{view === "color" ? 5 : 4}</small>
    </div>
  );

  const readinessBoundary = !bridge.connected ? <p aria-live="polite" className="ops-disconnected-banner" data-slot="multicam-readiness-boundary" role="status"><CircleAlert aria-hidden="true" size={16} />전용 멀티캠 observer가 ready 상태가 아닙니다. 실행 중인 모드는 유지되며, fresh CaptureStatus 확인 전에는 관측 내용을 신뢰하지 않습니다.</p> : null;

  const consoleBody = (
    <div className={`ops-layout${embedded ? " ops-embedded-layout" : ""}`}>
      <section className="ops-card ops-preview-card" aria-labelledby="ops-preview-heading">
        <header className="ops-card-heading">
          <div><p>SYNCED PREVIEW</p><h2 id="ops-preview-heading">주요 동기화 뷰</h2><span>{view === "color" ? "5개 /synced color stream · 토픽 수신 프레임을 원본 그대로 표시" : depthPresentation === "visualized" ? "D455 4대의 /synced compressedDepth stream · 원본 거리값을 화면 대비로만 가시화" : "D455 4대의 /synced compressedDepth stream · 토픽 원본 PNG 표시 · FLIR은 depth 센서가 없습니다."}</span></div>
          <div className="ops-preview-actions">
            <LayoutGroup id={`multicam-view-tabs-${embedded ? "debug" : "workspace"}`}>
              <div className="ops-segmented" role="tablist" aria-label="영상 유형">
                {(["color", "depth"] as const).map((option) => {
                  const active = view === option;
                  const Icon = option === "color" ? Eye : Box;
                  return (
                    <button
                      aria-controls="multicam-camera-panel"
                      aria-selected={active}
                      className={active ? "active" : ""}
                      id={`multicam-tab-${option}`}
                      key={option}
                      onClick={() => setView(option)}
                      onKeyDown={(event) => handleViewKeyDown(event, option)}
                      role="tab"
                      tabIndex={active ? 0 : -1}
                      type="button"
                    >
                      {active ? (
                        <m.span
                          aria-hidden="true"
                          className="ops-segment-focus"
                          layoutId={`multicam-active-view-${embedded ? "debug" : "workspace"}`}
                          transition={silk.layout.transition}
                        />
                      ) : null}
                      <span className="ops-segment-label"><Icon size={15} />{option === "color" ? "Color" : "Depth"}</span>
                    </button>
                  );
                })}
              </div>
            </LayoutGroup>
            {view === "depth" ? (
              <LayoutGroup id={`multicam-depth-presentation-${embedded ? "debug" : "workspace"}`}>
                <div className="ops-segmented ops-depth-presentation" aria-label="Depth 표시 방식" role="group">
                  {(["visualized", "raw"] as const).map((option) => {
                    const active = depthPresentation === option;
                    return (
                      <button
                        aria-pressed={active}
                        className={active ? "active" : ""}
                        key={option}
                        onClick={() => setDepthPresentation(option)}
                        type="button"
                      >
                        {active ? (
                          <m.span
                            aria-hidden="true"
                            className="ops-segment-focus"
                            layoutId={`multicam-active-depth-presentation-${embedded ? "debug" : "workspace"}`}
                            transition={silk.layout.transition}
                          />
                        ) : null}
                        <span className="ops-segment-label">{option === "visualized" ? "가시화" : "원본 PNG"}</span>
                      </button>
                    );
                  })}
                </div>
              </LayoutGroup>
            ) : null}
          </div>
        </header>
        <CameraGrid captureStatus={bridge.captureStatus} depthPresentation={depthPresentation} frames={activeFrames} now={now} view={view} />
      </section>
      {!embedded ? <TfScene transforms={bridge.tfTransforms} /> : null}
      <WorldAnchorPanel now={now} pending={bridge.worldActionPending} result={bridge.worldActionResult} status={bridge.worldStatus} />
      <CaptureStatusPanel colorFrames={bridge.colorFrames} language={language} now={now} status={bridge.captureStatus} />
      <TopicInspector now={now} onRefresh={bridge.refreshTopics} onSelect={bridge.setSelectedTopic} sample={bridge.selectedTopicSample} selectedTopic={bridge.selectedTopic} selectedType={bridge.selectedTopicType} topicError={bridge.topicError} topics={bridge.topics} />
    </div>
  );

  if (embedded) {
    return (
      <section className="ops-embedded-workspace" data-slot="debug-multicam-ops" id={mainId} tabIndex={-1} aria-labelledby="debug-multicam-heading">
        <header className="ops-card ops-embedded-header">
          <div className="ops-card-heading">
            <div><p>DEBUG · MULTICAM OBSERVER</p><h2 id="debug-multicam-heading">멀티캠 관제</h2><span>동기화 영상, Capture 상태, World Anchor를 read-only observer로 확인합니다. TF·3D 모델은 개별 기능의 TF 탭에서 확인합니다.</span></div>
          </div>
          {observerConnection}
        </header>
        {readinessBoundary}
        {consoleBody}
      </section>
    );
  }

  return (
    <div className="app-shell ops-app-shell" data-slot="multicam-ops-workspace">
      <a className="skip-link" href={`#${mainId}`}>
        {language === "ko" ? "멀티캠 본문으로 이동" : "Skip to multicamera content"}
      </a>
      <header className="ops-header">
        <div className="ops-brand"><button className="ops-back-button" onClick={() => onExit?.()} type="button"><ArrowLeft size={18} />{language === "ko" ? "미션 화면" : "Mission"}</button><div><p>ARPA MULTICAM · ROS 2 OPERATIONS</p><h1>멀티캠 관제 콘솔</h1><span>동기화 영상, 고정 TF, Capture 상태 및 World Anchor를 하나의 ROSBridge 세션에서 확인합니다.</span></div></div>
        {observerConnection}
      </header>

      {readinessBoundary}
      <main id={mainId} tabIndex={-1}>{consoleBody}</main>
    </div>
  );
}

export default MulticamOpsWorkspace;
