"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { mirrorHallNavigation } from "@/components/navigation";

const trackpadThreshold = 44;
const wheelCooldown = 180;

export function MirrorHallSection() {
  const pathname = usePathname();
  const router = useRouter();
  const initialIndex = Math.max(mirrorHallNavigation.findIndex((item) => item.href.split("#")[0] === pathname), 0);
  const [selectedIndex, setSelectedIndex] = useState(initialIndex);
  const [viewportWidth, setViewportWidth] = useState(1280);
  const selectedIndexRef = useRef(initialIndex);
  const hallRef = useRef<HTMLElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const touchStart = useRef<number | null>(null);
  const touchMoved = useRef(false);
  const touchSuppressTimer = useRef<number | null>(null);
  const wheelAccumulator = useRef(0);
  const nextWheelAt = useRef(0);
  const lastWheelDirection = useRef(0);
  const activeDestination = mirrorHallNavigation[selectedIndex] ?? mirrorHallNavigation[0];

  const selectIndex = useCallback((index: number) => {
    const boundedIndex = Math.max(0, Math.min(mirrorHallNavigation.length - 1, index));
    selectedIndexRef.current = boundedIndex;
    setSelectedIndex(boundedIndex);
  }, []);

  useEffect(() => () => {
    if (touchSuppressTimer.current !== null) window.clearTimeout(touchSuppressTimer.current);
  }, []);

  const moveSelection = useCallback((direction: number) => {
    selectIndex(selectedIndexRef.current + direction);
  }, [selectIndex]);

  useEffect(() => {
    const currentHash = window.location.hash;
    const currentIndex = mirrorHallNavigation.findIndex((item) =>
      item.href.split("#")[0] === pathname
      && (item.href.endsWith(currentHash) || !item.href.includes("#") || !currentHash));
    if (currentIndex >= 0) selectIndex(currentIndex);
  }, [pathname, selectIndex]);

  useEffect(() => {
    const updateViewportWidth = () => setViewportWidth(window.innerWidth);
    updateViewportWidth();
    window.addEventListener("resize", updateViewportWidth);
    return () => window.removeEventListener("resize", updateViewportWidth);
  }, []);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return undefined;

    const handleWheel = (event: WheelEvent) => {
      const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (!delta) return;
      const direction = Math.sign(delta);
      const nextIndex = selectedIndexRef.current + direction;
      if (nextIndex < 0 || nextIndex >= mirrorHallNavigation.length) return;
      if (event.cancelable) event.preventDefault();
      if (direction !== lastWheelDirection.current) {
        wheelAccumulator.current = 0;
        lastWheelDirection.current = direction;
      }

      const now = performance.now();
      const isDiscrete = event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL || Math.abs(delta) >= 55;
      if (!isDiscrete) {
        if (now < nextWheelAt.current) {
          event.preventDefault();
          return;
        }
        wheelAccumulator.current += Math.min(Math.abs(delta), trackpadThreshold);
        if (wheelAccumulator.current < trackpadThreshold) return;
      } else if (now < nextWheelAt.current) {
        event.preventDefault();
        return;
      }

      wheelAccumulator.current = 0;
      nextWheelAt.current = now + wheelCooldown;
      moveSelection(direction);
    };

    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [moveSelection]);

  const navigateToSelected = useCallback(() => {
    if (activeDestination) router.push(activeDestination.href);
  }, [activeDestination, router]);

  if (!activeDestination) return null;

  if (viewportWidth <= 760) {
    return (
      <section
        id="mirror-hall"
        className="landing-section mirror-hall-section mirror-hall-section--compact"
        aria-labelledby="mirror-hall-title"
      >
        <div className="landing-section__heading">
          <p className="landing-kicker"><span /> THE FINAL DESTINATION</p>
          <span className="landing-section__index">05 / NAVIGATE</span>
        </div>
        <div className="mirror-hall-section__intro" data-reveal>
          <div>
            <p className="mirror-hall-section__eyebrow">R MATRIX AI · PERSONAL COMMAND CENTER</p>
            <h2 id="mirror-hall-title">Enter the <em>Mirror Hall.</em></h2>
          </div>
          <p>Every destination in the workspace, in one corridor. Tap a section to step inside.</p>
        </div>
        <div className="mirror-compact">
          {mirrorHallNavigation.map((item, index) => (
            <Link
              key={item.href}
              href={item.href}
              className={`mirror-compact-card${index === selectedIndex ? " is-selected" : ""}`}
            >
              <span className="mirror-compact-card__number">{String(index + 1).padStart(2, "0")}</span>
              <span className="mirror-compact-card__copy">
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </span>
              <span className="mirror-compact-card__enter" aria-hidden="true">↗</span>
            </Link>
          ))}
        </div>
        <p className="mirror-hall-section__hint">TAP A SECTION TO ENTER · {mirrorHallNavigation.length} DESTINATIONS</p>
      </section>
    );
  }

  const visibleDistance = viewportWidth <= 760 ? 1 : 3;
  const horizontalStep = viewportWidth <= 760 ? Math.min(viewportWidth * 0.39, 150) : 180;

  return (
    <section
      id="mirror-hall"
      className="landing-section mirror-hall-section"
      aria-labelledby="mirror-hall-title"
      ref={hallRef}
      onKeyDown={(event) => {
        const target = event.target;
        if (target instanceof HTMLElement && target.matches("input, textarea, select, [contenteditable='true']")) return;
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          moveSelection(-1);
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          moveSelection(1);
        } else if (event.key === "Enter" && target === hallRef.current) {
          event.preventDefault();
          navigateToSelected();
        }
      }}
    >
      <div className="landing-section__heading">
        <p className="landing-kicker"><span /> THE FINAL DESTINATION</p>
        <span className="landing-section__index">05 / NAVIGATE</span>
      </div>
      <div className="mirror-hall-section__intro" data-reveal>
        <div>
          <p className="mirror-hall-section__eyebrow">R MATRIX AI · PERSONAL COMMAND CENTER</p>
          <h2 id="mirror-hall-title">Enter the <em>Mirror Hall.</em></h2>
        </div>
        <p>A corridor through the workspace. Move between real destinations, choose a portal, and step inside.</p>
      </div>

      <div
        className="landing-mirror-stage"
        ref={stageRef}
        tabIndex={0}
        aria-label="Mirror Hall destinations. Use arrow keys or scroll to navigate."
        data-reveal
        onTouchStart={(event) => {
          touchMoved.current = false;
          touchStart.current = event.changedTouches[0]?.clientX ?? null;
        }}
        onTouchEnd={(event) => {
          if (touchStart.current === null) return;
          const endX = event.changedTouches[0]?.clientX;
          if (endX !== undefined && Math.abs(endX - touchStart.current) > 42) {
            touchMoved.current = true;
            moveSelection(endX < touchStart.current ? 1 : -1);
            if (touchSuppressTimer.current !== null) window.clearTimeout(touchSuppressTimer.current);
            touchSuppressTimer.current = window.setTimeout(() => {
              touchMoved.current = false;
              touchSuppressTimer.current = null;
            }, 500);
          }
          touchStart.current = null;
        }}
      >
        <div className="landing-mirror-stage__horizon" aria-hidden="true" />
        <div className="landing-mirror-stage__floor" aria-hidden="true" />
        <div className="landing-mirror-stage__portals">
          {mirrorHallNavigation.map((item, index) => {
            const offset = index - selectedIndex;
            const distance = Math.abs(offset);
            const hidden = distance > visibleDistance;
            const angle = offset * 0.16;
            const x = offset * horizontalStep;
            const z = distance * 38 - 42;
            const scale = 1 + Math.min(distance, visibleDistance) * 0.035;
            const rotation = -angle * (180 / Math.PI);

            return (
              <Link
                href={item.href}
                key={item.href}
                className={`landing-portal${offset === 0 ? " is-selected" : ""}`}
                style={{
                  left: "50%",
                  zIndex: offset === 0 ? 20 : distance + 1,
                  opacity: hidden ? 0 : 1 - distance * 0.13,
                  transform: `translate3d(calc(-50% + ${x}px), -50%, ${z}px) rotateY(${rotation}deg) scale(${scale})`,
                  pointerEvents: hidden ? "none" : "auto",
                }}
                aria-label={`${String(index + 1).padStart(2, "0")} ${item.label}${offset === 0 ? ", selected destination" : ""}`}
                aria-hidden={hidden}
                tabIndex={hidden ? -1 : undefined}
                onClick={(event) => {
                  if (!touchMoved.current) return;
                  event.preventDefault();
                  touchMoved.current = false;
                  if (touchSuppressTimer.current !== null) window.clearTimeout(touchSuppressTimer.current);
                }}
              >
                <span className="landing-portal__reflection" aria-hidden="true" />
                <span className="landing-portal__top"><span>{String(index + 1).padStart(2, "0")} / {String(mirrorHallNavigation.length).padStart(2, "0")}</span><b aria-hidden="true">{item.mark}</b></span>
                <span className="landing-portal__copy"><strong>{item.label}</strong><small>{item.description}</small></span>
                <span className="landing-portal__bottom"><span>{offset === 0 ? "ACTIVE DESTINATION" : "R MATRIX PORTAL"}</span><i aria-hidden="true">↗</i></span>
              </Link>
            );
          })}
        </div>
      </div>

      <div className="mirror-hall-section__controls">
        <button type="button" className="mirror-control" onClick={() => moveSelection(-1)} aria-label="Previous destination">←</button>
        <div className="mirror-hall-section__selection">
          <span>{String(selectedIndex + 1).padStart(2, "0")} <i>/</i> {String(mirrorHallNavigation.length).padStart(2, "0")}</span>
          <strong>{activeDestination.label}</strong>
          <div className="mirror-hall-section__dots" aria-label="Select destination">
            {mirrorHallNavigation.map((item, index) => (
              <button
                key={item.href}
                type="button"
                onClick={() => selectIndex(index)}
                className={index === selectedIndex ? "is-active" : ""}
                aria-label={`Select ${item.label}`}
                aria-current={index === selectedIndex ? "step" : undefined}
              />
            ))}
          </div>
        </div>
        <button type="button" className="mirror-control" onClick={() => moveSelection(1)} aria-label="Next destination">→</button>
        <Link className="landing-button mirror-hall-section__enter" href={activeDestination.href}>Enter {activeDestination.label} <span aria-hidden="true">↗</span></Link>
      </div>
      <p className="mirror-hall-section__hint">SCROLL OR SWIPE TO EXPLORE <i>·</i> ← → NAVIGATE <i>·</i> SELECT A PORTAL TO ENTER</p>
    </section>
  );
}
