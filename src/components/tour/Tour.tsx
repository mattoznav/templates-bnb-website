import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { useGLTF, useProgress } from "@react-three/drei";
import * as THREE from "three";
import { withBase } from "../../lib/paths";
import { canShowTour, markReady, tour } from "../../lib/tour";

// Large screens get lightmaps at twice the resolution; phones and small laptops the lighter file
const MODEL_HD = withBase("/models/alder-house-hd.glb");
const MODEL_SD = withBase("/models/alder-house.glb");
const pickModel = () =>
  window.innerWidth * Math.min(window.devicePixelRatio, 2) >= 2000 && !window.matchMedia("(pointer: coarse)").matches
    ? MODEL_HD
    : MODEL_SD;
const DRACO = withBase("/draco/");
const SKY = "#dfe6e4";

type Point = [number, number, number];
type StopView = { camera: Point; target: Point; side: "left" | "right" };
type Props = { stops: StopView[] };

/** House coordinates (z up, y towards the back) to three.js (y up, z towards the viewer) */
const toScene = ([x, y, z]: Point) => new THREE.Vector3(x, z, -y);

export default function Tour({ stops }: Props) {
  const [model, setModel] = useState<string | null>(null);

  useEffect(() => {
    const ok = canShowTour();
    setModel(ok ? pickModel() : null);
    document.documentElement.dataset.tour = ok ? "loading" : "off";
  }, []);

  if (!model) return null;
  return (
    <>
      <Canvas
        flat
        dpr={[1, 1.75]}
        // A near plane at half a metre keeps depth precise, so surfaces close together do not flicker
        camera={{ fov: 38, near: 0.5, far: 300, position: [-2, 3.2, 16] }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        aria-hidden="true"
      >
        <color attach="background" args={[SKY]} />
        <fog attach="fog" args={[SKY, 30, 95]} />
        <Suspense fallback={null}>
          <House stops={stops} model={model} />
        </Suspense>
      </Canvas>
      <Loader />
    </>
  );
}

function House({ stops, model }: Props & { model: string }) {
  const { scene } = useGLTF(model, DRACO);
  const gl = useThree((state) => state.gl);
  const camera = useThree((state) => state.camera);
  const position = useRef(tour.target);
  const roof = useRef<THREE.Object3D | null>(null);
  const roofMaterial = useRef<THREE.MeshBasicMaterial | null>(null);
  const eye = useMemo(() => new THREE.Vector3(), []);
  const drift = useMemo(() => new THREE.Vector2(), []);
  const trees = useRef<{ mesh: THREE.Object3D; centre: THREE.Vector3; radius: number }[]>([]);
  const look = useMemo(() => new THREE.Vector3(), []);

  // Every texture already holds its light: show it as it is, without lights.
  const size = useThree((state) => state.size);
  const anchors = useMemo(
    () =>
      stops.map((stop) => ({
        camera: toScene(stop.camera),
        target: toScene(stop.target),
        // Push the scene away from the text: a card on the left puts the house on the right
        shift: stop.side === "left" ? 1 : -1,
      })),
    [stops],
  );

  useMemo(() => {
    scene.updateMatrixWorld(true);
    trees.current = [];
    scene.traverse((node) => {
      if (!(node instanceof THREE.Mesh)) return;
      const source = node.material as THREE.MeshStandardMaterial;
      const map = source.map;
      if (map) map.anisotropy = gl.capabilities.getMaxAnisotropy();
      const kind = partOf(node);
      let material: THREE.MeshBasicMaterial;
      if (kind === "glass") {
        // Window panes: a faint tint, so rooms stay visible from outside
        material = new THREE.MeshBasicMaterial({ color: "#dfeaec", transparent: true, opacity: 0.18, depthWrite: false });
      } else if (kind === "tree" || kind === "plant") {
        // Cut-out leaves; plants are not baked, so they are toned down to sit in the room's light
        material = new THREE.MeshBasicMaterial({
          map,
          alphaTest: 0.5,
          side: THREE.DoubleSide,
          color: kind === "plant" ? "#b9b4a8" : "#ffffff",
        });
      } else {
        // Everything else already holds its light in the texture
        material = new THREE.MeshBasicMaterial({ map });
      }
      if (kind === "roof") {
        material.transparent = true;
        roof.current = node;
        roofMaterial.current = material;
      }
      if (kind === "tree") {
        node.geometry.computeBoundingSphere();
        const sphere = node.geometry.boundingSphere!.clone().applyMatrix4(node.matrixWorld);
        trees.current.push({ mesh: node, centre: sphere.center, radius: sphere.radius });
      }
      node.material = material;
    });
  }, [scene, gl]);

  useEffect(() => {
    markReady();
    document.documentElement.dataset.tour = "ready";
  }, []);

  useFrame((state, delta) => {
    // Follow the scroll with a little lag, so wheel steps feel smooth
    position.current = THREE.MathUtils.damp(position.current, tour.target, 3.2, Math.min(delta, 0.1));
    const last = anchors.length - 1;
    const p = THREE.MathUtils.clamp(position.current, 0, last);
    const i = Math.min(Math.floor(p), last - 1);
    // Ease each leg so the camera settles at every stop
    const f = smootherstep(p - i);
    eye.lerpVectors(anchors[i].camera, anchors[i + 1].camera, f);
    look.lerpVectors(anchors[i].target, anchors[i + 1].target, f);
    // A small, eased drift with the pointer keeps the scene alive between scrolls
    drift.x = THREE.MathUtils.damp(drift.x, state.pointer.x * 0.15, 2, delta);
    drift.y = THREE.MathUtils.damp(drift.y, state.pointer.y * 0.08, 2, delta);
    eye.x += drift.x;
    eye.y += drift.y;
    camera.position.copy(eye);
    camera.lookAt(look);

    // Trees step aside when they would stand between the camera and the room
    for (const tree of trees.current) {
      tree.mesh.visible = segmentDistance(tree.centre, eye, look) > tree.radius + 0.5;
    }

    // Frame the subject beside the card on wide screens, above it on narrow ones
    const cam = camera as THREE.PerspectiveCamera;
    const wide = size.width > 760;
    const shift = THREE.MathUtils.lerp(anchors[i].shift, anchors[i + 1].shift, f);
    const aspect = size.width / size.height;
    cam.fov = aspect < 1 ? Math.min(68, 38 / Math.pow(aspect, 0.75)) : 38;
    cam.setViewOffset(
      size.width,
      size.height,
      wide ? -shift * size.width * 0.17 : 0,
      wide ? 0 : size.height * 0.2,
      size.width,
      size.height,
    );

    // The roof lifts off after the first stop and settles back for the last one
    if (roof.current && roofMaterial.current) {
      const open = THREE.MathUtils.clamp(Math.min(p, last - p) * 1.4, 0, 1);
      roof.current.position.y = open * 4;
      roofMaterial.current.opacity = 1 - open;
      roof.current.visible = open < 0.99;
    }
  });

  const pick = (event: ThreeEvent<MouseEvent>) => {
    const room = roomOf(event.object);
    if (!room) return;
    event.stopPropagation();
    tour.onRoomClick?.(room);
  };
  const hover = (event: ThreeEvent<PointerEvent>) => {
    document.body.style.cursor = roomOf(event.object) ? "pointer" : "";
  };

  return (
    <>
      <primitive object={scene} onClick={pick} onPointerMove={hover} onPointerOut={() => (document.body.style.cursor = "")} />
      {/* Lawn beyond the baked garden, fading into the fog */}
      <mesh rotation-x={-Math.PI / 2} position={[8, -0.2, -5]}>
        <circleGeometry args={[220, 48]} />
        <meshBasicMaterial color="#8aa36a" />
      </mesh>
    </>
  );
}

function Loader() {
  const { progress, active } = useProgress();
  useEffect(() => {
    document.documentElement.style.setProperty("--load", `${Math.round(progress)}%`);
  }, [progress]);
  return (
    <div className="tour-loader" data-done={!active && progress === 100} role="status">
      <span>Opening the house</span>
      <span className="tour-loader__bar" />
    </div>
  );
}

/** What a mesh is, from its own name or its parent's (meshes with several materials are split into children) */
function partOf(node: THREE.Object3D): string {
  for (let n: THREE.Object3D | null = node; n; n = n.parent) {
    const match = /^(room|plant|tree|glass|roof|walls|grounds)/.exec(n.name);
    if (match) return match[1];
  }
  return "";
}

function roomOf(node: THREE.Object3D): string | null {
  for (let n: THREE.Object3D | null = node; n; n = n.parent) {
    if (n.name.startsWith("room_")) return n.name.slice("room_".length);
  }
  return null;
}

const ab = new THREE.Vector3();
const ap = new THREE.Vector3();
/** Distance from point p to the segment a-b */
function segmentDistance(p: THREE.Vector3, a: THREE.Vector3, b: THREE.Vector3) {
  ab.subVectors(b, a);
  ap.subVectors(p, a);
  const t = THREE.MathUtils.clamp(ap.dot(ab) / ab.lengthSq(), 0, 1);
  return ap.sub(ab.multiplyScalar(t)).length();
}

function smootherstep(x: number) {
  const t = THREE.MathUtils.clamp(x, 0, 1);
  return t * t * t * (t * (t * 6 - 15) + 10);
}
