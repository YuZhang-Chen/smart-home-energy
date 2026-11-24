
import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { MathUtils, type Group } from "three";

interface CurtainPanelsProps {
  position?: number;
  speed?: number;
}

const FOLD_COUNT = 12;
const FOLD_SPACING = 0.15;
const OPEN_SCALE = 0.2;
const OPEN_SHIFT = 1.45;

export default function CurtainPanels({
  position = 0,
  speed = 7,
}: CurtainPanelsProps) {
  const leftRef = useRef<Group>(null);
  const rightRef = useRef<Group>(null);

  const currentPosition = MathUtils.clamp(position, 0, 100);

  const progress = currentPosition / 100;

  // 0%：完整展開，遮住窗戶
  // 100%：左右布料收至窗戶兩側
  const targetScale = MathUtils.lerp(
    1,
    OPEN_SCALE,
    progress
  );

  const targetShift = OPEN_SHIFT * progress;

  useFrame((_, delta) => {
    if (leftRef.current) {
      leftRef.current.position.z = MathUtils.damp(
        leftRef.current.position.z,
        -targetShift,
        speed,
        delta
      );

      leftRef.current.scale.z = MathUtils.damp(
        leftRef.current.scale.z,
        targetScale,
        speed,
        delta
      );
    }

    if (rightRef.current) {
      rightRef.current.position.z = MathUtils.damp(
        rightRef.current.position.z,
        targetShift,
        speed,
        delta
      );

      rightRef.current.scale.z = MathUtils.damp(
        rightRef.current.scale.z,
        targetScale,
        speed,
        delta
      );
    }
  });

  return (
    <>
      {/* 左側窗簾：覆蓋窗戶左半部 */}
      <group ref={leftRef}>
        {Array.from({ length: FOLD_COUNT }, (_, i) => (
          <mesh
            key={`left-${i}`}
            position={[
              0.12 + (i % 2) * 0.04,
              1.5,
              -(FOLD_SPACING / 2 + i * FOLD_SPACING),
            ]}
            castShadow
            receiveShadow
          >
            <cylinderGeometry args={[0.11, 0.11, 2.6, 8]} />
            <meshStandardMaterial
              color={i % 2 ? "#ded6bf" : "#ece4d0"}
              roughness={0.95}
            />
          </mesh>
        ))}
      </group>

      {/* 右側窗簾：覆蓋窗戶右半部 */}
      <group ref={rightRef}>
        {Array.from({ length: FOLD_COUNT }, (_, i) => (
          <mesh
            key={`right-${i}`}
            position={[
              0.12 + (i % 2) * 0.04,
              1.5,
              FOLD_SPACING / 2 + i * FOLD_SPACING,
            ]}
            castShadow
            receiveShadow
          >
            <cylinderGeometry args={[0.11, 0.11, 2.6, 8]} />
            <meshStandardMaterial
              color={i % 2 ? "#ded6bf" : "#ece4d0"}
              roughness={0.95}
            />
          </mesh>
        ))}
      </group>
    </>
  );
}
