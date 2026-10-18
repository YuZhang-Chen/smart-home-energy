import {
  Component,
  Suspense,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  Canvas,
  useFrame,
  useThree,
  type ThreeEvent,
} from "@react-three/fiber";
import { OrbitControls, RoundedBox } from "@react-three/drei";
import {
  Color,
  Group,
  MathUtils,
  MeshStandardMaterial,
  PointLight,
  Vector3,
  PCFShadowMap,
} from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { devices, isAnimating, type DeviceId, type Home } from "./types";

type Vec = [number, number, number];
const wood = "#bb9065",
  cream = "#eee8d9",
  green = "#637a5a",
  dark = "#343e39";
function Block({
  at,
  size,
  color,
  rotation,
  ...props
}: {
  at: Vec;
  size: Vec;
  color: string;
  rotation?: Vec;
  castShadow?: boolean;
  receiveShadow?: boolean;
}) {
  return (
    <mesh position={at} rotation={rotation} castShadow receiveShadow {...props}>
      <boxGeometry args={size} />
      <meshStandardMaterial color={color} roughness={0.85} />
    </mesh>
  );
}
function Soft({
  at,
  size,
  color,
  radius = 0.06,
}: {
  at: Vec;
  size: Vec;
  color: string;
  radius?: number;
}) {
  return (
    <RoundedBox
      position={at}
      args={size}
      radius={radius}
      smoothness={2}
      castShadow
      receiveShadow
    >
      <meshStandardMaterial color={color} roughness={0.95} />
    </RoundedBox>
  );
}
function Cylinder({
  at,
  args,
  color,
  rotation,
}: {
  at: Vec;
  args: [number, number, number, number?];
  color: string;
  rotation?: Vec;
}) {
  return (
    <mesh position={at} rotation={rotation} castShadow receiveShadow>
      <cylinderGeometry args={args} />
      <meshStandardMaterial color={color} roughness={0.7} />
    </mesh>
  );
}
function Plant({ at, scale = 1 }: { at: Vec; scale?: number }) {
  return (
    <group position={at} scale={scale}>
      <Cylinder at={[0, 0.2, 0]} args={[0.23, 0.17, 0.4, 8]} color="#dfcfb4" />
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <group key={i} rotation={[0, i * 1.05, 0]}>
          <Cylinder
            at={[0.06, 0.6, 0]}
            args={[0.012, 0.018, 0.7, 5]}
            color={green}
            rotation={[0, 0, -0.2]}
          />
          <mesh
            position={[0.2, 0.75 + (i % 2) * 0.18, 0]}
            rotation={[0.2, 0, -0.75]}
            scale={[0.15, 0.35, 0.07]}
            castShadow
          >
            <icosahedronGeometry args={[1, 0]} />
            <meshStandardMaterial color={i % 2 ? "#82956b" : green} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

function Pickable({
  id,
  at,
  selected,
  onSelect,
  onHover,
  children,
}: {
  id: DeviceId;
  at: Vec;
  selected: DeviceId;
  onSelect: (id: DeviceId) => void;
  onHover: (id: DeviceId | null) => void;
  children: ReactNode;
}) {
  const [hovered, setHovered] = useState(false);
  const down = useRef<[number, number] | null>(null);
  const controls = useThree((s) => s.controls) as OrbitControlsImpl | null;
  const dragging = useRef(false);
  useEffect(() => {
    const start = () => {
      dragging.current = true;
    };
    const end = () => {
      dragging.current = false;
    };
    controls?.addEventListener("start", start);
    controls?.addEventListener("end", end);
    return () => {
      controls?.removeEventListener("start", start);
      controls?.removeEventListener("end", end);
    };
  }, [controls]);
  useEffect(() => {
    if (hovered) document.body.style.cursor = "pointer";
    return () => {
      document.body.style.cursor = "";
    };
  }, [hovered]);
  const stop = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    setHovered(false);
    onHover(null);
  };
  return (
    <group
      position={at}
      onPointerOver={(e) => {
        e.stopPropagation();
        if (!dragging.current) {
          setHovered(true);
          onHover(id);
        }
      }}
      onPointerOut={stop}
      onPointerDown={(e) => {
        down.current = [e.clientX, e.clientY];
      }}
      onClick={(e) => {
        e.stopPropagation();
        if (
          down.current &&
          Math.hypot(e.clientX - down.current[0], e.clientY - down.current[1]) <
            5
        )
          onSelect(id);
        down.current = null;
      }}
    >
      {children}
      {selected === id && (
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.015 - at[1], 0]}>
          <ringGeometry args={[0.62, 0.66, 48]} />
          <meshBasicMaterial
            color="#a0b78b"
            transparent
            opacity={0.8}
            depthWrite={false}
          />
        </mesh>
      )}
    </group>
  );
}

const labelPositions: Record<DeviceId, Vec> = {
  "ac-1": [-0.65, 3.01, -2.77],
  "washer-1": [2.85, 1.55, -1.95],
  "tv-1": [-0.7, 2.4, -2.69],
  "curtain-1": [-3.9, 3.1, -0.35],
  "bulb-1": [-0.55, 3.3, 0.1],
  "plug-1": [1.35, 0.77, -2.96],
};
function LabelTracker({
  id,
  element,
}: {
  id: DeviceId;
  element: RefObject<HTMLDivElement | null>;
}) {
  const point = useRef(new Vector3());
  useFrame(({ camera, size }) => {
    if (!element.current) return;
    point.current.set(...labelPositions[id]).project(camera);
    element.current.style.transform = `translate(-50%, -100%) translate(${((point.current.x + 1) * size.width) / 2}px, ${((1 - point.current.y) * size.height) / 2}px)`;
    element.current.style.visibility =
      Math.abs(point.current.z) <= 1 ? "visible" : "hidden";
  });
  return null;
}

function Walls() {
  const back = useRef<Group>(null),
    left = useRef<Group>(null);
  useFrame(({ camera }) => {
    if (back.current) back.current.visible = camera.position.z > -2.95;
    if (left.current) left.current.visible = camera.position.x > -3.95;
  });
  return (
    <>
      <group ref={back}>
        <Block at={[0, 1.55, -3.1]} size={[8.2, 3.1, 0.18]} color="#e8e3d5" />
        <Block at={[0, 0.1, -2.98]} size={[8, 0.16, 0.05]} color="#d1c4ad" />
        <Block at={[0, 3.1, -3.1]} size={[8.25, 0.06, 0.24]} color="#f7f1e7" />
      </group>
      <group ref={left}>
        <Block at={[-4.1, 1.55, 0]} size={[0.18, 3.1, 6.2]} color="#eeeadf" />
        <Block at={[-3.98, 0.1, 0]} size={[0.05, 0.16, 6]} color="#d1c4ad" />
        <Block at={[-4.1, 3.1, 0]} size={[0.24, 0.06, 6.25]} color="#f7f1e7" />
      </group>
    </>
  );
}

function Furniture() {
  return (
    <>
      <Soft
        at={[0, -0.22, 0]}
        size={[8.6, 0.4, 6.6]}
        color="#d1bea0"
        radius={0.12}
      />
      <Block at={[0, -0.005, 0]} size={[8, 0.05, 6]} color="#d5b68d" />
      {Array.from({ length: 15 }, (_, i) => (
        <Block
          key={i}
          at={[-3.73 + i * 0.53, 0.023, 0]}
          size={[0.012, 0.003, 6]}
          color="#bda17e"
        />
      ))}
      {[0, 1, 2, 3, 4].map((i) => (
        <Block
          key={i}
          at={[i % 2 ? 0 : -1.8, 0.025, -2.4 + i * 1.2]}
          size={[i % 2 ? 8 : 4.4, 0.003, 0.012]}
          color="#c1a580"
        />
      ))}
      <Walls />
      <Soft
        at={[-0.7, 0.055, 0.45]}
        size={[4.25, 0.045, 3.35]}
        color="#c3c8af"
        radius={0.02}
      />
      <Block at={[-0.7, 0.081, 0.45]} size={[3.9, 0.005, 3]} color="#cbd0ba" />
      {[-1.5, 0.1].map((x) =>
        [-2.15, -2.7].map((z) => (
          <Block
            key={`${x}${z}`}
            at={[x, 0.17, z]}
            size={[0.08, 0.3, 0.08]}
            color={dark}
          />
        )),
      )}
      <Soft
        at={[-0.7, 0.38, -2.45]}
        size={[3.6, 0.42, 0.7]}
        color={wood}
        radius={0.025}
      />
      {[-1.8, -0.7, 0.4].map((x) => (
        <Block
          key={x}
          at={[x, 0.37, -2.089]}
          size={[1.05, 0.29, 0.02]}
          color="#b68b61"
        />
      ))}
      <Cylinder
        at={[-2.1, 0.68, -2.45]}
        args={[0.13, 0.12, 0.18, 10]}
        color="#dfd5bd"
      />
      <Plant at={[0.6, 0.59, -2.45]} scale={0.4} />
      <group position={[-0.8, 0, 1.65]}>
        {[-1.3, 1.3].map((x) =>
          [-0.3, 0.3].map((z) => (
            <Cylinder
              key={`${x}${z}`}
              at={[x, 0.16, z]}
              args={[0.045, 0.035, 0.25, 6]}
              color={wood}
            />
          )),
        )}
        <Soft at={[0, 0.39, 0]} size={[3.05, 0.4, 1.1]} color="#c4bda9" />
        <Soft at={[0, 0.88, 0.42]} size={[3.05, 0.9, 0.27]} color={cream} />
        {[-1.47, 1.47].map((x) => (
          <Soft
            key={x}
            at={[x, 0.66, 0]}
            size={[0.3, 0.65, 1.16]}
            color={cream}
          />
        ))}
        {[-0.69, 0.69].map((x) => (
          <Soft
            key={x}
            at={[x, 0.65, -0.1]}
            size={[1.3, 0.22, 0.85]}
            color="#e6decd"
          />
        ))}
        <group rotation={[0, 0.15, 0.12]}>
          <Soft
            at={[-0.93, 1.01, 0.22]}
            size={[0.5, 0.48, 0.17]}
            color={green}
          />
        </group>
        <group rotation={[0, -0.1, -0.15]}>
          <Soft
            at={[1.0, 0.93, 0.2]}
            size={[0.45, 0.43, 0.18]}
            color="#c49a70"
          />
        </group>
        <Block
          at={[0.75, 0.77, -0.17]}
          size={[0.55, 0.025, 0.67]}
          color="#aab399"
        />
      </group>
      <group position={[-0.65, 0, -0.03]}>
        {[-0.6, 0.6].map((x) =>
          [-0.3, 0.3].map((z) => (
            <Block
              key={`${x}${z}`}
              at={[x, 0.25, z]}
              size={[0.07, 0.5, 0.07]}
              color={wood}
              rotation={[0, 0, x > 0 ? -0.12 : 0.12]}
            />
          )),
        )}
        <Soft
          at={[0, 0.51, 0]}
          size={[1.75, 0.12, 0.95]}
          color="#c6a276"
          radius={0.05}
        />
        <Block
          at={[-0.33, 0.6, -0.12]}
          size={[0.4, 0.05, 0.32]}
          color="#ece5cf"
          rotation={[0, 0.12, 0]}
        />
        <Block
          at={[-0.3, 0.63, -0.1]}
          size={[0.35, 0.025, 0.27]}
          color={green}
          rotation={[0, -0.08, 0]}
        />
        <Cylinder
          at={[0.4, 0.66, 0.05]}
          args={[0.085, 0.07, 0.18, 12]}
          color="#f3edde"
        />
        <Cylinder
          at={[0.4, 0.755, 0.05]}
          args={[0.062, 0.062, 0.006, 12]}
          color="#695546"
        />
      </group>
      <Plant at={[-3.15, 0, 2]} scale={1.3} />
      <Cylinder
        at={[-2.9, 0.28, -2.2]}
        args={[0.28, 0.24, 0.55, 8]}
        color="#e7d6b9"
      />
      <Plant at={[-2.9, 0.55, -2.2]} scale={0.7} />
      <group position={[2.85, 0, -1.9]}>
        <Block at={[0, 0.04, 0]} size={[1.9, 0.04, 1.8]} color="#d9d5ca" />
        <Block at={[0, 1.4, -0.94]} size={[1.9, 2.8, 0.05]} color="#d9dace" />
        <Block at={[0, 1.75, -0.55]} size={[1.7, 0.08, 0.6]} color={wood} />
        <Block
          at={[-0.48, 1.91, -0.55]}
          size={[0.5, 0.23, 0.38]}
          color="#f2ead9"
        />
        <Block
          at={[-0.48, 2.04, -0.55]}
          size={[0.48, 0.04, 0.36]}
          color="#c4bba2"
        />
        <Plant at={[0.45, 1.8, -0.55]} scale={0.4} />
      </group>
      <Fan />
    </>
  );
}

function Fan() {
  return (
    <group position={[3.05, 0, 1.55]}>
      <Cylinder
        at={[0, 0.08, 0]}
        args={[0.36, 0.4, 0.12, 16]}
        color="#dce0d2"
      />
      <Cylinder
        at={[0, 0.63, 0]}
        args={[0.045, 0.055, 1.1, 8]}
        color="#a4ad9c"
      />
      <Cylinder
        at={[0, 1.3, 0]}
        args={[0.43, 0.43, 0.13, 24]}
        rotation={[Math.PI / 2, 0, 0]}
        color="#d7ddd0"
      />
      {[0, 1, 2].map((i) => (
        <group
          key={i}
          position={[0, 1.3, 0.09]}
          rotation={[0, 0, (i * Math.PI * 2) / 3]}
        >
          <mesh position={[0.17, 0, 0]} scale={[0.24, 0.09, 0.025]}>
            <sphereGeometry args={[1, 8, 6]} />
            <meshStandardMaterial color="#8b9d82" />
          </mesh>
        </group>
      ))}
      {[0.2, 0.32, 0.43].map((r) => (
        <mesh key={r} position={[0, 1.3, 0.13]}>
          <torusGeometry args={[r, 0.012, 4, 32]} />
          <meshStandardMaterial color="#bbc3b2" />
        </mesh>
      ))}
      <Cylinder
        at={[0, 1.3, 0.15]}
        args={[0.07, 0.07, 0.04, 12]}
        rotation={[Math.PI / 2, 0, 0]}
        color={cream}
      />
    </group>
  );
}

function AirConditioner({
  running,
  reduced,
}: {
  running: boolean;
  reduced: boolean;
}) {
  const stream = useRef<Group>(null);
  useFrame(({ clock }) => {
    if (stream.current && running && !reduced)
      stream.current.position.y =
        -0.25 - (Math.sin(clock.elapsedTime * 2) + 1) * 0.06;
  });
  return (
    <>
      <Soft
        at={[0, 0, 0]}
        size={[1.75, 0.43, 0.34]}
        color="#f7f5ee"
        radius={0.055}
      />
      <Block at={[0, -0.1, 0.18]} size={[1.48, 0.065, 0.025]} color="#a1aba4" />
      <Block
        at={[0, -0.13, 0.19]}
        size={[1.45, 0.025, 0.04]}
        color={running ? "#7faeac" : "#d4d9cd"}
      />
      <mesh position={[0.63, 0.035, 0.18]}>
        <circleGeometry args={[0.019, 12]} />
        <meshBasicMaterial color={running ? "#8ba771" : "#c3c7bc"} />
      </mesh>
      {running && (
        <group ref={stream} position={[0, -0.3, 0.3]}>
          {[-0.45, 0, 0.45].map((x) => (
            <mesh key={x} position={[x, 0, 0.2]} rotation={[-0.6, 0, 0]}>
              <boxGeometry args={[0.018, 0.45, 0.014]} />
              <meshBasicMaterial color="#a6d6d2" transparent opacity={0.6} />
            </mesh>
          ))}
        </group>
      )}
    </>
  );
}

function Washer({ running, reduced }: { running: boolean; reduced: boolean }) {
  const drum = useRef<Group>(null);
  useFrame((_, delta) => {
    if (drum.current && running && !reduced)
      drum.current.rotation.z += delta * 1.6;
  });
  return (
    <>
      <Soft
        at={[0, 0.65, 0]}
        size={[1.12, 1.22, 1.02]}
        color="#f1f2e9"
        radius={0.045}
      />
      <Block at={[0, 1.11, 0.525]} size={[1.02, 0.19, 0.015]} color="#e4e6dd" />
      <Block at={[-0.3, 1.11, 0.54]} size={[0.3, 0.09, 0.02]} color="#c7cec0" />
      <Cylinder
        at={[0.05, 1.11, 0.55]}
        args={[0.064, 0.064, 0.025, 16]}
        rotation={[Math.PI / 2, 0, 0]}
        color="#aeb9a7"
      />
      <Block
        at={[0.32, 1.11, 0.54]}
        size={[0.18, 0.07, 0.02]}
        color={running ? "#799b83" : "#4c5b53"}
      />
      <Cylinder
        at={[0, 0.61, 0.53]}
        args={[0.41, 0.41, 0.07, 32]}
        rotation={[Math.PI / 2, 0, 0]}
        color="#adb5aa"
      />
      <Cylinder
        at={[0, 0.61, 0.575]}
        args={[0.335, 0.335, 0.025, 32]}
        rotation={[Math.PI / 2, 0, 0]}
        color="#3c4c48"
      />
      <group position={[0, 0.61, 0.595]} ref={drum}>
        {[0, 1, 2].map((i) => (
          <group rotation={[0, 0, i * 2.1]} key={i}>
            <mesh position={[0.13, 0, 0]} scale={[0.16, 0.1, 0.02]}>
              <icosahedronGeometry args={[1, 1]} />
              <meshStandardMaterial color={i === 0 ? "#b7ccb8" : "#90a6a0"} />
            </mesh>
          </group>
        ))}
      </group>
      <mesh position={[0, 0.61, 0.62]}>
        <circleGeometry args={[0.32, 32]} />
        <meshStandardMaterial
          color="#a3c0b8"
          transparent
          opacity={0.16}
          roughness={0.15}
          depthWrite={false}
        />
      </mesh>
      <Block
        at={[0.38, 0.65, 0.61]}
        size={[0.07, 0.22, 0.07]}
        color="#f8f6ed"
      />
    </>
  );
}

function Lamp({ on, reduced }: { on: boolean; reduced: boolean }) {
  const light = useRef<PointLight>(null),
    material = useRef<MeshStandardMaterial>(null);
  useFrame((_, dt) => {
    const alpha = reduced ? 1 : 1 - Math.exp(-dt * 18);
    if (light.current)
      light.current.intensity = MathUtils.lerp(
        light.current.intensity,
        on ? 26 : 0,
        alpha,
      );
    if (material.current)
      material.current.emissiveIntensity = MathUtils.lerp(
        material.current.emissiveIntensity,
        on ? 1.4 : 0,
        alpha,
      );
  });
  return (
    <>
      <Cylinder at={[0, 0.27, 0]} args={[0.012, 0.012, 0.55, 6]} color={dark} />
      <Cylinder
        at={[0, -0.04, 0]}
        args={[0.16, 0.38, 0.25, 16]}
        color="#c2a679"
      />
      <mesh position={[0, -0.2, 0]} castShadow>
        <sphereGeometry args={[0.13, 12, 8]} />
        <meshStandardMaterial
          ref={material}
          color="#fff3cf"
          emissive={new Color("#ffd99a")}
          emissiveIntensity={on ? 1.4 : 0}
        />
      </mesh>
      <pointLight
        ref={light}
        position={[0, -0.35, 0]}
        intensity={on ? 26 : 0}
        color="#ffdfac"
        distance={9}
        decay={2}
      />
    </>
  );
}

function CameraRig({ reset }: { reset: number }) {
  const controls = useRef<OrbitControlsImpl>(null);
  const { camera, size } = useThree();
  useEffect(() => {
    camera.position.set(10, 14.942, 10);
    camera.zoom = Math.min(size.width / 14, size.height / 11);
    camera.updateProjectionMatrix();
    controls.current?.target.set(0, 0.8, 0);
    controls.current?.update();
    // Resize adjusts framing; polling and device selection do not touch the camera.
  }, [camera, reset, size.width, size.height]);
  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enablePan={false}
      enableDamping
      dampingFactor={0.08}
      minPolarAngle={Math.PI / 7}
      maxPolarAngle={Math.PI / 2.5}
      minZoom={Math.min(size.width / 14, size.height / 11) * 0.65}
      maxZoom={Math.min(size.width / 14, size.height / 11) * 2.3}
    />
  );
}

export function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => setReduced(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  return reduced;
}
class SceneBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <Fallback /> : this.props.children;
  }
}
function Fallback() {
  return (
    <div className="scene-fallback">
      <b>3D 畫面暫時無法顯示</b>
      <p>瀏覽器需要支援 WebGL。你仍可使用下方設備清單與控制面板。</p>
    </div>
  );
}

export default function Scene({
  selected,
  onSelect,
  lightOn,
  home,
  realNow,
  reset,
}: {
  selected: DeviceId;
  onSelect: (id: DeviceId) => void;
  lightOn: boolean;
  home: Home | null;
  realNow: number;
  reset: number;
}) {
  const reduced = useReducedMotion();
  const [hovered, setHovered] = useState<DeviceId | null>(null);
  const label = useRef<HTMLDivElement>(null);
  const activeLabel = hovered ?? selected;
  const pick = (id: DeviceId, at: Vec, children: ReactNode) => (
    <Pickable
      id={id}
      at={at}
      selected={selected}
      onSelect={onSelect}
      onHover={setHovered}
    >
      {children}
    </Pickable>
  );
  return (
    <SceneBoundary>
      <Suspense
        fallback={<div className="scene-fallback">正在準備你的客廳…</div>}
      >
        <div className="scene-root">
          <Canvas
            orthographic
            shadows={{ type: PCFShadowMap }}
            dpr={[1, 1.5]}
            camera={{
              position: [10, 14.942, 10],
              zoom: 50,
              near: 0.1,
              far: 100,
            }}
            gl={{
              antialias: true,
              alpha: true,
              powerPreference: "high-performance",
            }}
            fallback={<span>可透過設備清單查看家電。</span>}
          >
            <CameraRig reset={reset} />
            <LabelTracker id={activeLabel} element={label} />
            <ambientLight intensity={0.75} color="#fff6e6" />
            <hemisphereLight args={["#f3f6ed", "#c3b59c", 1.25]} />
            <directionalLight
              position={[-3, 9, 5]}
              intensity={2.4}
              color="#fff2d4"
              castShadow
              shadow-mapSize={[1024, 1024]}
              shadow-camera-left={-7}
              shadow-camera-right={7}
              shadow-camera-top={7}
              shadow-camera-bottom={-7}
              shadow-normalBias={0.04}
            />
            <Furniture />
            {pick(
              "ac-1",
              [-0.65, 2.61, -2.77],
              <AirConditioner
                running={isAnimating(
                  home?.devices["ac-1"],
                  realNow,
                  home?.clock.clock_id,
                )}
                reduced={reduced}
              />,
            )}
            {pick(
              "washer-1",
              [2.85, 0, -1.95],
              <Washer
                running={isAnimating(
                  home?.devices["washer-1"],
                  realNow,
                  home?.clock.clock_id,
                )}
                reduced={reduced}
              />,
            )}
            {pick(
              "tv-1",
              [-0.7, 1.5, -2.69],
              <>
                <Soft
                  at={[0, 0, 0]}
                  size={[2.2, 1.25, 0.09]}
                  color="#343d39"
                  radius={0.025}
                />
                <Block
                  at={[0, 0, 0.052]}
                  size={[2.05, 1.1, 0.015]}
                  color="#586b65"
                />
                <mesh position={[0.3, 0.1, 0.065]}>
                  <circleGeometry args={[0.34, 32]} />
                  <meshBasicMaterial color="#acb6a0" />
                </mesh>
                <Block
                  at={[-0.35, -0.27, 0.075]}
                  size={[1.15, 0.035, 0.005]}
                  color="#819488"
                />
                <Block
                  at={[0, -0.75, 0]}
                  size={[0.1, 0.3, 0.08]}
                  color={dark}
                />
              </>,
            )}
            {pick(
              "curtain-1",
              [-3.9, 0, -0.35],
              <>
                <Block
                  at={[0, 1.52, 0]}
                  size={[0.07, 2.65, 3.7]}
                  color="#b4c4bd"
                />
                <Block
                  at={[0.05, 1.52, 0]}
                  size={[0.04, 2.65, 0.06]}
                  color="#ddd5c2"
                />
                <Block
                  at={[0.05, 1.52, -1.8]}
                  size={[0.04, 2.65, 0.06]}
                  color="#ddd5c2"
                />
                <Block
                  at={[0.05, 1.52, 1.8]}
                  size={[0.04, 2.65, 0.06]}
                  color="#ddd5c2"
                />
                <Block
                  at={[0.05, 0.2, 0]}
                  size={[0.04, 0.06, 3.65]}
                  color="#ddd5c2"
                />
                {[-1, 1].map((side) =>
                  Array.from({ length: 6 }, (_, i) => (
                    <Cylinder
                      key={`${side}${i}`}
                      at={[
                        0.12 + (i % 2) * 0.04,
                        1.5,
                        side * (1.05 + i * 0.16),
                      ]}
                      args={[0.11, 0.11, 2.6, 6]}
                      color={i % 2 ? "#ded6bf" : "#ece4d0"}
                    />
                  )),
                )}
                <Block
                  at={[0.1, 2.87, 0]}
                  size={[0.08, 0.06, 4]}
                  color={wood}
                />
              </>,
            )}
            {pick(
              "bulb-1",
              [-0.55, 2.7, 0.1],
              <Lamp on={lightOn} reduced={reduced} />,
            )}
            {pick(
              "plug-1",
              [1.35, 0.37, -2.96],
              <>
                <Soft
                  at={[0, 0, 0]}
                  size={[0.25, 0.3, 0.045]}
                  color="#faf6e9"
                  radius={0.02}
                />
                {[-0.045, 0.045].map((x) => (
                  <Block
                    key={x}
                    at={[x, 0.025, 0.027]}
                    size={[0.017, 0.05, 0.006]}
                    color="#7f887e"
                  />
                ))}
                <mesh position={[0, -0.065, 0.027]}>
                  <circleGeometry args={[0.015, 8]} />
                  <meshBasicMaterial color="#879a77" />
                </mesh>
              </>,
            )}
            <mesh
              rotation={[-Math.PI / 2, 0, 0]}
              position={[0, -0.43, 0]}
              receiveShadow
            >
              <planeGeometry args={[200, 200]} />
              <shadowMaterial transparent opacity={0.1} />
            </mesh>
          </Canvas>
          <div ref={label} className="floating-scene-label" aria-hidden="true">
            <div
              className={`scene-label ${activeLabel === selected ? "is-selected" : ""}`}
            >
              <span />
              {devices.find((d) => d.id === activeLabel)!.name}
            </div>
          </div>
        </div>
      </Suspense>
    </SceneBoundary>
  );
}
