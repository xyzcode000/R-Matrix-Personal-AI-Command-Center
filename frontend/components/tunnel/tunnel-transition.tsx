"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { RobotSilhouette } from "./robot-silhouette";

export type TunnelTransitionProps = Readonly<{
  /** Called once the tunnel has finished playing. */
  onComplete: () => void;
  /** Show the R Matrix Bot in the tunnel. Used for Introduction → Identity. */
  showRobot?: boolean;
  /** Total playback time in milliseconds. */
  duration?: number;
}>;

const RING_COUNT = 16;

/**
 * Full-viewport cinematic tunnel played between experience scenes.
 *
 * It is a temporary overlay: it mounts when the scene changes, calls
 * onComplete when the animation ends, and the caller unmounts it. Scrolling is
 * never blocked, and reduced-motion users skip straight to onComplete.
 */
export function TunnelTransition({ onComplete, showRobot = false, duration = 1400 }: TunnelTransitionProps) {
  const [leaving, setLeaving] = useState(false);
  const finished = useRef(false);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const total = reduceMotion ? 0 : Math.max(200, duration);
    const complete = window.setTimeout(() => {
      if (finished.current) return;
      finished.current = true;
      onComplete();
    }, total);
    const fade = window.setTimeout(() => setLeaving(true), Math.max(0, total - Math.min(360, total * 0.3)));
    return () => {
      window.clearTimeout(complete);
      window.clearTimeout(fade);
    };
  }, [duration, onComplete]);

  return (
    <div className={`tunnel-overlay${leaving ? " is-leaving" : ""}`} aria-hidden="true" data-testid="tunnel-transition">
      <div className="tunnel-stage">
        <span className="tunnel-ceiling" />
        <span className="tunnel-floor" />
        <div className="tunnel-rings">
          {Array.from({ length: RING_COUNT }, (_, index) => (
            <span
              key={index}
              className="tunnel-ring"
              style={
                {
                  "--ring-index": index,
                  "--ring-delay": `${(index / RING_COUNT) * 520}ms`,
                } as CSSProperties
              }
            />
          ))}
        </div>
        <span className="tunnel-core" />
        {showRobot && (
          <div className="tunnel-robot">
            <RobotSilhouette animated />
            <span className="tunnel-robot__label">R Matrix Bot</span>
          </div>
        )}
        <span className="tunnel-vignette" />
      </div>
    </div>
  );
}
