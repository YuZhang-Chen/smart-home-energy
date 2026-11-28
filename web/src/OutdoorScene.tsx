import { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { RoundedBox } from "@react-three/drei";
import {
  Color,
  Group,
  MathUtils,
  MeshStandardMaterial,
  PointLight,
} from "three";
import {
  calculateDayNight,
  displayedMinute,
  type DayNightState,
} from "./dayNight";

type Vec = [number, number, number];

const GRASS_Y = -0.16;
const WOOD_FENCE = "#b78f66";
const WOOD_DARK = "#8b6949";
const STONE_LIGHT = "#dfd8c8";
const STONE_MID = "#cfc7b4";

function LowPolyTree({
  at,
  scale = 1,
  phase = 0,
  variant = "pine",
  reduced,
}: {
  at: Vec;
  scale?: number;
  phase?: number;
  variant?: "pine" | "round";
  reduced: boolean;
}) {
  const canopy = useRef<Group>(null);
  useFrame(({ clock }) => {
    if (!canopy.current) return;
    if (reduced) {
      canopy.current.rotation.z = 0;
      canopy.current.rotation.x = 0;
      return;
    }
    const t = clock.elapsedTime * 0.9 + phase;
    canopy.current.rotation.z = Math.sin(t) * 0.022;
    canopy.current.rotation.x = Math.cos(t * 0.7) * 0.014;
  });

  return (
    <group position={at} scale={scale}>
      <mesh position={[0, 0.55, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.11, 0.16, 1.1, 7]} />
        <meshStandardMaterial color={WOOD_DARK} roughness={0.92} />
      </mesh>
      <group ref={canopy} position={[0, 1.05, 0]}>
        {variant === "pine" ? (
          <>
            <mesh position={[0, 0.45, 0]} castShadow receiveShadow>
              <coneGeometry args={[0.88, 1.25, 7]} />
              <meshStandardMaterial color="#5e7a54" roughness={0.88} />
            </mesh>
            <mesh position={[0, 1.12, 0]} castShadow receiveShadow>
              <coneGeometry args={[0.68, 1.05, 7]} />
              <meshStandardMaterial color="#6e8c60" roughness={0.88} />
            </mesh>
            <mesh position={[0, 1.7, 0]} castShadow receiveShadow>
              <coneGeometry args={[0.44, 0.78, 7]} />
              <meshStandardMaterial color="#809d6e" roughness={0.88} />
            </mesh>
          </>
        ) : (
          <>
            <mesh position={[0, 0.58, 0]} castShadow receiveShadow>
              <icosahedronGeometry args={[0.78, 1]} />
              <meshStandardMaterial color="#68855b" roughness={0.9} flatShading />
            </mesh>
            <mesh position={[0.42, 0.36, 0.22]} castShadow receiveShadow>
              <icosahedronGeometry args={[0.48, 1]} />
              <meshStandardMaterial color="#7d9a6c" roughness={0.9} flatShading />
            </mesh>
            <mesh position={[-0.36, 0.42, -0.2]} castShadow receiveShadow>
              <icosahedronGeometry args={[0.5, 1]} />
              <meshStandardMaterial color="#5c7751" roughness={0.9} flatShading />
            </mesh>
          </>
        )}
      </group>
    </group>
  );
}

function Bush({
  at,
  scale = 1,
  color = "#779364",
  flowers,
}: {
  at: Vec;
  scale?: number;
  color?: string;
  flowers?: string;
}) {
  return (
    <group position={at} scale={scale}>
      <mesh position={[0, 0.24, 0]} castShadow receiveShadow>
        <dodecahedronGeometry args={[0.38, 0]} />
        <meshStandardMaterial color={color} roughness={0.92} flatShading />
      </mesh>
      <mesh position={[0.26, 0.17, 0.14]} castShadow receiveShadow>
        <dodecahedronGeometry args={[0.25, 0]} />
        <meshStandardMaterial color="#88a372" roughness={0.92} flatShading />
      </mesh>
      <mesh position={[-0.24, 0.16, -0.1]} castShadow receiveShadow>
        <dodecahedronGeometry args={[0.24, 0]} />
        <meshStandardMaterial color="#678257" roughness={0.92} flatShading />
      </mesh>
      {flowers && (
        <>
          <mesh position={[0.08, 0.46, 0.16]}>
            <octahedronGeometry args={[0.06, 0]} />
            <meshStandardMaterial color={flowers} roughness={0.75} />
          </mesh>
          <mesh position={[-0.14, 0.38, 0.2]}>
            <octahedronGeometry args={[0.055, 0]} />
            <meshStandardMaterial color={flowers} roughness={0.75} />
          </mesh>
          <mesh position={[0.28, 0.3, 0.05]}>
            <octahedronGeometry args={[0.05, 0]} />
            <meshStandardMaterial color="#f5e7c1" roughness={0.75} />
          </mesh>
        </>
      )}
    </group>
  );
}

function SteppingStone({
  at,
  size = [0.78, 0.06, 0.56],
  rotationY = 0,
  color = STONE_LIGHT,
}: {
  at: Vec;
  size?: Vec;
  rotationY?: number;
  color?: string;
}) {
  return (
    <group position={at} rotation={[0, rotationY, 0]}>
      <RoundedBox args={size} radius={0.025} smoothness={2} receiveShadow>
        <meshStandardMaterial color={color} roughness={0.92} />
      </RoundedBox>
    </group>
  );
}

function FenceRun({
  start,
  count,
  step,
  rotationY = 0,
}: {
  start: Vec;
  count: number;
  step: number;
  rotationY?: number;
}) {
  const span = (count - 1) * step;
  return (
    <group position={start} rotation={[0, rotationY, 0]}>
      {Array.from({ length: count }, (_, i) => (
        <mesh
          key={i}
          position={[i * step, 0.42, 0]}
          castShadow
          receiveShadow
        >
          <boxGeometry args={[0.1, 0.84, 0.1]} />
          <meshStandardMaterial color={WOOD_FENCE} roughness={0.88} />
        </mesh>
      ))}
      {[0.28, 0.64].map((y) => (
        <mesh
          key={y}
          position={[span / 2, y, 0]}
          castShadow
          receiveShadow
        >
          <boxGeometry args={[span + 0.08, 0.075, 0.055]} />
          <meshStandardMaterial color="#c49d74" roughness={0.88} />
        </mesh>
      ))}
    </group>
  );
}

function GardenBench({ at, rotationY = 0 }: { at: Vec; rotationY?: number }) {
  return (
    <group position={at} rotation={[0, rotationY, 0]}>
      {[-0.68, 0.68].map((x) =>
        [-0.2, 0.2].map((z) => (
          <mesh key={`${x}${z}`} position={[x, 0.22, z]} castShadow receiveShadow>
            <boxGeometry args={[0.08, 0.44, 0.08]} />
            <meshStandardMaterial color="#46514b" roughness={0.75} />
          </mesh>
        )),
      )}
      <RoundedBox
        position={[0, 0.46, 0]}
        args={[1.7, 0.08, 0.56]}
        radius={0.025}
        smoothness={2}
        castShadow
        receiveShadow
      >
        <meshStandardMaterial color="#bc9367" roughness={0.86} />
      </RoundedBox>
      {[-0.68, 0.68].map((x) => (
        <mesh
          key={x}
          position={[x, 0.72, -0.24]}
          rotation={[-0.14, 0, 0]}
          castShadow
          receiveShadow
        >
          <boxGeometry args={[0.07, 0.52, 0.07]} />
          <meshStandardMaterial color="#46514b" roughness={0.75} />
        </mesh>
      ))}
      <RoundedBox
        position={[0, 0.84, -0.26]}
        rotation={[-0.14, 0, 0]}
        args={[1.7, 0.22, 0.06]}
        radius={0.02}
        smoothness={2}
        castShadow
        receiveShadow
      >
        <meshStandardMaterial color="#c49c70" roughness={0.86} />
      </RoundedBox>
    </group>
  );
}

function GardenLamp({
  at,
  clock,
  reduced,
}: {
  at: Vec;
  clock: DayNightState;
  reduced: boolean;
}) {
  const light = useRef<PointLight>(null);
  const bulbMat = useRef<MeshStandardMaterial>(null);

  useFrame((_, delta) => {
    const targetFactor = calculateDayNight(displayedMinute(clock)).outdoorLampIntensity;
    const alpha = reduced ? 1 : 1 - Math.exp(-delta * 4);
    if (light.current) {
      light.current.intensity = MathUtils.lerp(
        light.current.intensity,
        targetFactor * 5.5,
        alpha,
      );
    }
    if (bulbMat.current) {
      bulbMat.current.emissiveIntensity = MathUtils.lerp(
        bulbMat.current.emissiveIntensity,
        targetFactor * 2.2,
        alpha,
      );
    }
  });

  return (
    <group position={at}>
      <mesh position={[0, 0.05, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.13, 0.16, 0.1, 8]} />
        <meshStandardMaterial color="#49534d" roughness={0.75} />
      </mesh>
      <mesh position={[0, 0.45, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.055, 0.065, 0.72, 8]} />
        <meshStandardMaterial color="#3c4641" roughness={0.7} />
      </mesh>
      <mesh position={[0, 0.88, 0]}>
        <cylinderGeometry args={[0.105, 0.105, 0.2, 8]} />
        <meshStandardMaterial
          ref={bulbMat}
          color="#fff4da"
          emissive="#ffca7a"
          emissiveIntensity={0}
          roughness={0.35}
        />
      </mesh>
      <mesh position={[0, 1.03, 0]} castShadow>
        <coneGeometry args={[0.17, 0.13, 8]} />
        <meshStandardMaterial color="#3c4641" roughness={0.7} />
      </mesh>
      <pointLight
        ref={light}
        position={[0, 0.9, 0]}
        color="#ffd28a"
        intensity={0}
        distance={5.5}
        decay={2}
      />
    </group>
  );
}

const CLOUD_CONFIGS = [
  { baseX: -7.5, y: 6.2, z: -6.2, scale: 1.05, speed: 0.18 },
  { baseX: 1.5, y: 6.8, z: -7.4, scale: 0.85, speed: 0.13 },
  { baseX: -10.5, y: 5.9, z: -1.5, scale: 0.78, speed: 0.15 },
];

function FloatingClouds({
  clock,
  reduced,
}: {
  clock: DayNightState;
  reduced: boolean;
}) {
  const groupRefs = useRef<(Group | null)[]>([]);
  const matRefs = useRef<(MeshStandardMaterial | null)[]>([]);
  const tint = useMemo(() => new Color(), []);
  const white = useMemo(() => new Color("#ffffff"), []);

  useFrame(({ clock: r3fClock }, delta) => {
    const lighting = calculateDayNight(displayedMinute(clock));
    const nightWeight = lighting.outdoorLampIntensity;
    tint.set(lighting.sky).lerp(white, 0.72 - nightWeight * 0.35);
    const alpha = reduced ? 1 : 1 - Math.exp(-delta * 3);

    CLOUD_CONFIGS.forEach((cfg, idx) => {
      const grp = groupRefs.current[idx];
      if (grp && !reduced) {
        const span = 22;
        const offset = ((cfg.baseX + 11 + r3fClock.elapsedTime * cfg.speed) % span) - 11;
        grp.position.x = offset;
      }
      const mat = matRefs.current[idx];
      if (mat) {
        mat.color.lerp(tint, alpha);
      }
    });
  });

  return (
    <group>
      {CLOUD_CONFIGS.map((cfg, idx) => (
        <group
          key={idx}
          ref={(el) => {
            groupRefs.current[idx] = el;
          }}
          position={[cfg.baseX, cfg.y, cfg.z]}
          scale={cfg.scale}
        >
          <mesh position={[0, 0, 0]}>
            <dodecahedronGeometry args={[0.72, 1]} />
            <meshStandardMaterial
              ref={(el) => {
                matRefs.current[idx] = el;
              }}
              color="#f7f7f2"
              roughness={0.95}
              transparent
              opacity={0.88}
            />
          </mesh>
          <mesh position={[0.68, -0.08, 0.08]}>
            <dodecahedronGeometry args={[0.52, 1]} />
            <meshStandardMaterial color="#f4f5ef" roughness={0.95} transparent opacity={0.85} />
          </mesh>
          <mesh position={[-0.64, -0.1, -0.05]}>
            <dodecahedronGeometry args={[0.56, 1]} />
            <meshStandardMaterial color="#f4f5ef" roughness={0.95} transparent opacity={0.85} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

export default function OutdoorScene({
  clock,
  reduced,
}: {
  clock: DayNightState;
  reduced: boolean;
}) {
  return (
    <group name="outdoor-garden">
      {/* 微縮庭園雙層底座：大地土壤基座 + 柔和草皮層 */}
      <RoundedBox
        position={[0, -0.58, 0.3]}
        args={[17.6, 0.56, 14.6]}
        radius={0.18}
        smoothness={3}
        receiveShadow
      >
        <meshStandardMaterial color="#9c856b" roughness={0.95} />
      </RoundedBox>
      <RoundedBox
        position={[0, -0.24, 0.3]}
        args={[17.2, 0.16, 14.2]}
        radius={0.12}
        smoothness={2}
        receiveShadow
      >
        <meshStandardMaterial color="#8ca473" roughness={0.94} />
      </RoundedBox>

      {/* 客廳前緣石材台階（銜接客廳底座與庭園草地） */}
      <RoundedBox
        position={[0.4, -0.11, 3.68]}
        args={[2.7, 0.11, 0.72]}
        radius={0.03}
        smoothness={2}
        castShadow
        receiveShadow
      >
        <meshStandardMaterial color={STONE_LIGHT} roughness={0.9} />
      </RoundedBox>

      {/* 前庭石板步道 */}
      <SteppingStone at={[0.45, GRASS_Y + 0.025, 4.55]} size={[1.15, 0.055, 0.64]} rotationY={0.03} />
      <SteppingStone at={[0.25, GRASS_Y + 0.025, 5.42]} size={[1.05, 0.055, 0.6]} rotationY={-0.06} color={STONE_MID} />
      <SteppingStone at={[0.55, GRASS_Y + 0.025, 6.28]} size={[1.1, 0.055, 0.62]} rotationY={0.04} />

      {/* 左側落地窗外弧形石板小徑 */}
      <SteppingStone at={[-5.35, GRASS_Y + 0.025, 2.35]} size={[0.72, 0.05, 0.58]} rotationY={0.22} />
      <SteppingStone at={[-5.7, GRASS_Y + 0.025, 1.35]} size={[0.76, 0.05, 0.54]} rotationY={-0.12} color={STONE_MID} />
      <SteppingStone at={[-5.55, GRASS_Y + 0.025, 0.25]} size={[0.8, 0.05, 0.56]} rotationY={0.08} />
      <SteppingStone at={[-5.75, GRASS_Y + 0.025, -0.85]} size={[0.74, 0.05, 0.55]} rotationY={-0.16} color={STONE_MID} />
      <SteppingStone at={[-5.45, GRASS_Y + 0.025, -1.9]} size={[0.78, 0.05, 0.54]} rotationY={0.14} />

      {/* 左側落地窗外主景樹群（拉開窗簾可直接看見） */}
      <LowPolyTree at={[-6.85, GRASS_Y, -1.65]} scale={1.15} phase={0.4} variant="round" reduced={reduced} />
      <LowPolyTree at={[-7.2, GRASS_Y, 1.45]} scale={1.0} phase={1.7} variant="pine" reduced={reduced} />
      <LowPolyTree at={[-6.6, GRASS_Y, -4.6]} scale={1.28} phase={2.9} variant="pine" reduced={reduced} />

      {/* 後側背景樹群 */}
      <LowPolyTree at={[-2.4, GRASS_Y, -5.35]} scale={1.18} phase={3.8} variant="round" reduced={reduced} />
      <LowPolyTree at={[2.9, GRASS_Y, -5.2]} scale={1.25} phase={5.1} variant="pine" reduced={reduced} />
      <LowPolyTree at={[6.6, GRASS_Y, -4.75]} scale={1.05} phase={0.9} variant="round" reduced={reduced} />

      {/* 灌木與花叢（點綴窗外、前庭與右庭） */}
      <Bush at={[-6.1, GRASS_Y, -0.2]} scale={0.95} flowers="#f2c6b6" />
      <Bush at={[-5.1, GRASS_Y, 3.65]} scale={0.85} color="#6f8c5e" />
      <Bush at={[-7.4, GRASS_Y, 3.8]} scale={1.05} flowers="#f3d69c" />
      <Bush at={[-3.3, GRASS_Y, 4.75]} scale={0.9} flowers="#f2c6b6" />
      <Bush at={[5.6, GRASS_Y, 4.2]} scale={0.95} color="#749061" flowers="#e8c3d0" />
      <Bush at={[6.8, GRASS_Y, 0.8]} scale={1.05} />
      <Bush at={[6.3, GRASS_Y, -2.2]} scale={0.9} color="#69855a" />
      <Bush at={[0.2, GRASS_Y, -5.3]} scale={1.1} flowers="#f3d69c" />

      {/* 低矮木圍籬（界定左側與後側微縮底座邊界） */}
      <FenceRun start={[-8.1, GRASS_Y, -6.2]} count={9} step={2.02} rotationY={0} />
      <FenceRun start={[-8.1, GRASS_Y, -6.2]} count={7} step={2.0} rotationY={-Math.PI / 2} />

      {/* 戶外休閒長椅（前庭右側，低矮不遮擋客廳視角） */}
      <GardenBench at={[4.3, GRASS_Y, 5.45]} rotationY={-0.38} />

      {/* 日夜自動點亮的庭園柱燈（左窗外、前庭、右庭、後庭） */}
      <GardenLamp at={[-5.15, GRASS_Y, -0.35]} clock={clock} reduced={reduced} />
      <GardenLamp at={[-1.45, GRASS_Y, 5.15]} clock={clock} reduced={reduced} />
      <GardenLamp at={[6.15, GRASS_Y, 2.6]} clock={clock} reduced={reduced} />
      <GardenLamp at={[0.4, GRASS_Y, -4.65]} clock={clock} reduced={reduced} />

      {/* 高空柔和 Low-Poly 飄浮雲朵 */}
      <FloatingClouds clock={clock} reduced={reduced} />
    </group>
  );
}
