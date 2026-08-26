import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import { RotateCcw } from "lucide-react";
import * as THREE from "three";

import type { StaticTransform } from "../../hooks/useMulticamOpsBridge";
import "./MulticamOpsWorkspace.css";

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
      void import("three/addons/loaders/GLTFLoader.js")
        .then(({ GLTFLoader }) => {
          if (disposed) return;
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
        })
        .catch((error) => {
          if (disposed) return;
          console.error("TF model loader initialization failed", error);
          setModelStatus("error");
        });
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
    <section className="ops-card ops-tf-card" aria-labelledby="ops-tf-heading" data-slot="multicam-tf-scene">
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
