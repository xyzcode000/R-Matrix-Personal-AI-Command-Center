"use client";

import { useEffect, useRef, useState } from "react";

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function buildRibbonPath(width: number, height: number, time: number, velocity: number) {
  const sampleCount = 22;
  const pathHeight = height * 2;
  const verticalOffset = ((time * 76) % height) - height;
  const amplitude = width * (0.105 + velocity * 0.0025);
  const points: Array<[number, number]> = [];

  for (let index = 0; index <= sampleCount; index += 1) {
    const progress = index / sampleCount;
    const y = progress * pathHeight + verticalOffset;
    const phase = y / height * Math.PI * 4;
    const x = width / 2
      + Math.sin(phase) * amplitude
      + Math.sin(phase * 0.5) * width * 0.075;
    points.push([clamp(x, width * 0.12, width * 0.88), y]);
  }

  return points.map(([x, y], index) => {
    if (index === 0) return `M ${x.toFixed(1)} ${y.toFixed(1)}`;

    const previous = points[index - 1];
    const beforePrevious = points[Math.max(0, index - 2)];
    const next = points[Math.min(points.length - 1, index + 1)];
    const firstControlX = previous[0] + (x - beforePrevious[0]) / 6;
    const firstControlY = previous[1] + (y - beforePrevious[1]) / 6;
    const secondControlX = x - (next[0] - previous[0]) / 6;
    const secondControlY = y - (next[1] - previous[1]) / 6;

    return `C ${firstControlX.toFixed(1)} ${firstControlY.toFixed(1)}, ${secondControlX.toFixed(1)} ${secondControlY.toFixed(1)}, ${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
}

export function EnergyRibbon() {
  const [viewport, setViewport] = useState({ width: 1440, height: 900 });
  const glowPath = useRef<SVGPathElement>(null);
  const bodyPath = useRef<SVGPathElement>(null);
  const velocityRef = useRef(0);
  const scrollYRef = useRef(0);

  useEffect(() => {
    const updateViewport = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    updateViewport();
    window.addEventListener("resize", updateViewport);

    const handleScroll = () => {
      const currentY = window.scrollY;
      velocityRef.current = clamp(velocityRef.current * 0.68 + Math.abs(currentY - scrollYRef.current) * 0.08, 0, 14);
      scrollYRef.current = currentY;
    };
    window.addEventListener("scroll", handleScroll, { passive: true });

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrameId = 0;
    let lastFrameAt = 0;
    const tick = (timestamp: number) => {
      if (timestamp - lastFrameAt >= 40) {
        const time = reducedMotion ? 0 : timestamp / 1000;
        const path = buildRibbonPath(viewport.width, viewport.height, time, velocityRef.current);
        glowPath.current?.setAttribute("d", path);
        bodyPath.current?.setAttribute("d", path);
        velocityRef.current *= 0.94;
        lastFrameAt = timestamp;
      }
      if (!reducedMotion) animationFrameId = window.requestAnimationFrame(tick);
    };

    animationFrameId = window.requestAnimationFrame(tick);
    return () => {
      window.removeEventListener("resize", updateViewport);
      window.removeEventListener("scroll", handleScroll);
      window.cancelAnimationFrame(animationFrameId);
    };
  }, [viewport.width, viewport.height]);

  return (
    <div className="energy-ribbon-layer" aria-hidden="true">
      <svg className="energy-ribbon-svg" viewBox={`0 0 ${viewport.width} ${viewport.height}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="energy-ribbon-main" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#5C5278" />
            <stop offset="52%" stopColor="#B5A177" />
            <stop offset="100%" stopColor="#EAD8B2" />
          </linearGradient>
          <filter id="energy-ribbon-glow" x="-60%" y="-30%" width="220%" height="160%">
            <feGaussianBlur stdDeviation="14" />
          </filter>
        </defs>
        <path ref={glowPath} className="energy-ribbon energy-ribbon--outer" stroke="#C9B183" strokeWidth="34" filter="url(#energy-ribbon-glow)" opacity="0.16" />
        <path ref={bodyPath} className="energy-ribbon energy-ribbon--main" stroke="url(#energy-ribbon-main)" strokeWidth="14" opacity="0.34" />
      </svg>
    </div>
  );
}
