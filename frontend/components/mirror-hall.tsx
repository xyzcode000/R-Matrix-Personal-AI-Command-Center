"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { navigation } from "./navigation";

type MirrorHallProps = Readonly<{ onClose: () => void }>;

const transitionDuration = 380;
const trackpadStepThreshold = 48;
const trackpadCooldown = 115;
const trackpadPauseReset = 240;
const wheelCooldown = 190;
const lineDeltaPixels = 16;
const pageDeltaRatio = 0.65;

function relativeOffset(index: number, activeIndex: number) {
  return index - activeIndex;
}

export function MirrorHall({ onClose }: MirrorHallProps) {
  const pathname = usePathname();
  const router = useRouter();
  const activePathIndex = navigation.findIndex((item) => item.href === pathname);
  const [selectedIndex, setSelectedIndex] = useState(Math.max(activePathIndex, 0));
  const [viewportWidth, setViewportWidth] = useState(1024);
  const [entering, setEntering] = useState(false);
  const hallRef = useRef<HTMLElement>(null);
  const initialPath = useRef(pathname);
  const navigationTimer = useRef<number | null>(null);
  const touchStart = useRef<number | null>(null);
  const selectedIndexRef = useRef(selectedIndex);
  const wheelAccumulator = useRef(0);
  const lastWheelDirection = useRef(0);
  const nextWheelAt = useRef(0);
  const lastTrackpadAt = useRef(0);
  const active = navigation[selectedIndex];

  const selectIndex = useCallback((index: number) => {
    const boundedIndex = Math.max(0, Math.min(navigation.length - 1, index));
    selectedIndexRef.current = boundedIndex;
    setSelectedIndex(boundedIndex);
  }, []);

  const moveSelection = useCallback((direction: number) => {
    selectIndex(selectedIndexRef.current + direction);
  }, [selectIndex]);

  const activateSelection = useCallback(() => {
    if (navigationTimer.current !== null) return;
    setEntering(true);
    navigationTimer.current = window.setTimeout(() => {
      router.push(active.href);
      if (active.href === pathname) onClose();
      navigationTimer.current = window.setTimeout(() => {
        navigationTimer.current = null;
        setEntering(false);
      }, 700);
    }, transitionDuration);
  }, [active.href, onClose, pathname, router]);

  useEffect(() => {
    hallRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    const previousOverscrollBehavior = document.body.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.body.style.overscrollBehavior = "none";

    return () => {
      document.body.style.overflow = previousOverflow;
      document.body.style.overscrollBehavior = previousOverscrollBehavior;
      if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current);
    };
  }, []);

  useEffect(() => {
    const hall = hallRef.current;
    if (!hall) return undefined;

    const handleWheel = (event: WheelEvent) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.isContentEditable || target.closest("input, textarea, select, [contenteditable='true']")) return;

      let ancestor = target.parentElement;
      while (ancestor && ancestor !== hall) {
        const style = window.getComputedStyle(ancestor);
        const scrollableY = /(auto|scroll|overlay)/.test(style.overflowY)
          && ancestor.scrollHeight > ancestor.clientHeight;
        const scrollableX = /(auto|scroll|overlay)/.test(style.overflowX)
          && ancestor.scrollWidth > ancestor.clientWidth;
        if (scrollableX || scrollableY) return;
        ancestor = ancestor.parentElement;
      }

      const rawDelta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (rawDelta === 0) return;

      const axisDelta = event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? rawDelta * lineDeltaPixels
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
          ? rawDelta * hall.clientHeight * pageDeltaRatio
          : rawDelta;
      const direction = Math.sign(axisDelta);
      const currentIndex = selectedIndexRef.current;

      if (event.cancelable) event.preventDefault();

      if (direction !== lastWheelDirection.current) {
        wheelAccumulator.current = 0;
        lastWheelDirection.current = direction;
        lastTrackpadAt.current = 0;
        nextWheelAt.current = 0;
      }

      const now = performance.now();
      const isDiscreteWheel = event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL || Math.abs(axisDelta) >= 55;
      const cooldown = isDiscreteWheel ? wheelCooldown : trackpadCooldown;

      if (!isDiscreteWheel) {
        if (now - lastTrackpadAt.current > trackpadPauseReset) wheelAccumulator.current = 0;
        lastTrackpadAt.current = now;
        wheelAccumulator.current += Math.min(Math.abs(axisDelta), trackpadStepThreshold);
        if (wheelAccumulator.current < trackpadStepThreshold) return;
      }

      if (now < nextWheelAt.current) return;
      wheelAccumulator.current = 0;
      nextWheelAt.current = now + cooldown;
      selectIndex(currentIndex + direction);
    };

    hall.addEventListener("wheel", handleWheel, { passive: false });
    return () => hall.removeEventListener("wheel", handleWheel);
  }, [selectIndex]);

  useEffect(() => {
    const updateWidth = () => setViewportWidth(window.innerWidth);
    updateWidth();
    window.addEventListener("resize", updateWidth);
    return () => window.removeEventListener("resize", updateWidth);
  }, []);

  useEffect(() => {
    if (pathname !== initialPath.current) onClose();
  }, [onClose, pathname]);

  const isMobile = viewportWidth <= 640;
  const openDestination = useCallback(
    (href: string) => {
      router.push(href);
      if (href === pathname) onClose();
    },
    [onClose, pathname, router],
  );

  if (isMobile) {
    return (
      <section
        ref={hallRef}
        className="mirror-hall mirror-hall--mobile"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mirror-hall-title"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onClose();
          }
        }}
      >
        <div className="mirror-hall__atmosphere" aria-hidden="true" />
        <header className="mirror-hall__header">
          <div>
            <p className="mirror-hall__eyebrow">R MATRIX AI · PERSONAL COMMAND CENTER</p>
            <h1 id="mirror-hall-title">The Mirror Hall</h1>
          </div>
          <button className="mirror-hall__close" type="button" onClick={onClose} aria-label="Close Mirror Hall">
            <span aria-hidden="true">×</span><span className="mirror-hall__close-label">Close</span>
          </button>
        </header>

        <div className="mirror-mobile">
          <p className="mirror-mobile__eyebrow">SELECT A DESTINATION</p>
          <div className="mirror-mobile__list">
            {navigation.map((item, index) => {
              const selected = index === selectedIndex;
              return (
                <button
                  key={item.href}
                  type="button"
                  className={`mirror-mobile-card${selected ? " is-selected" : ""}`}
                  onClick={() => openDestination(item.href)}
                  aria-current={selected ? "true" : undefined}
                >
                  <span className="mirror-mobile-card__number">{String(index + 1).padStart(2, "0")}</span>
                  <span className="mirror-mobile-card__copy">
                    <strong>{item.label}</strong>
                    <small>{item.description}</small>
                  </span>
                  <span className="mirror-mobile-card__enter">
                    {item.planned ? "PLANNED" : "OPEN"}<i aria-hidden="true">↗</i>
                  </span>
                </button>
              );
            })}
          </div>
          <p className="mirror-mobile__hint">Tap a section to open it · close with ×</p>
        </div>
      </section>
    );
  }

  const angleStep = isMobile ? 0.55 : viewportWidth <= 900 ? 0.38 : 0.4;
  const curveRadius = viewportWidth * (isMobile ? 0.7 : 0.72);

  return (
    <section
      ref={hallRef}
      className={`mirror-hall${entering ? " mirror-hall--entering" : ""}`}
      data-boundary={selectedIndex === 0 ? "start" : selectedIndex === navigation.length - 1 ? "end" : undefined}
      role="dialog"
      aria-modal="true"
      aria-labelledby="mirror-hall-title"
      tabIndex={-1}
      onKeyDown={(event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (target.isContentEditable || target.matches("input, textarea, select")) return;

        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        } else if (event.key === "ArrowLeft") {
          event.preventDefault();
          moveSelection(-1);
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          moveSelection(1);
        } else if (event.key === "Enter" && target.closest(".mirror-portal")) {
          event.preventDefault();
          activateSelection();
        } else if (event.key === "Enter" && !target.closest("button, a")) {
          event.preventDefault();
          activateSelection();
        } else if (event.key === "Tab") {
          const focusable = Array.from(hallRef.current?.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled])',
          ) ?? []);
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (target === hallRef.current) {
            event.preventDefault();
            (event.shiftKey ? last : first)?.focus();
          } else if (event.shiftKey && target === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && target === last) {
            event.preventDefault();
            first?.focus();
          }
        }
      }}
      onTouchStart={(event) => {
        if (event.target instanceof HTMLElement && event.target.closest("button, a")) return;
        touchStart.current = event.changedTouches[0]?.clientX ?? null;
      }}
      onTouchEnd={(event) => {
        if (touchStart.current === null) return;
        const delta = event.changedTouches[0]?.clientX;
        if (delta !== undefined && Math.abs(delta - touchStart.current) > 48) {
          moveSelection(delta < touchStart.current ? 1 : -1);
        }
        touchStart.current = null;
      }}
    >
      <div className="mirror-hall__atmosphere" aria-hidden="true" />
      <header className="mirror-hall__header">
        <div>
          <p className="mirror-hall__eyebrow">R MATRIX AI · PERSONAL COMMAND CENTER</p>
          <h1 id="mirror-hall-title">The Mirror Hall</h1>
        </div>
        <button className="mirror-hall__close" type="button" onClick={onClose} aria-label="Close Mirror Hall">
          <span aria-hidden="true">×</span><span className="mirror-hall__close-label">Close</span>
        </button>
      </header>

      <div className="mirror-hall__stage" aria-label="Choose a command center destination">
        <div className="mirror-hall__horizon" aria-hidden="true" />
        <div className="mirror-hall__floor" aria-hidden="true" />
        <div className="mirror-hall__portals">
          {navigation.map((item, index) => {
            const offset = relativeOffset(index, selectedIndex);
            const distance = Math.abs(offset);
            const angle = offset * angleStep;
            const x = Math.sin(angle) * curveRadius;
            const depth = Math.cos(angle) * curveRadius - curveRadius - distance * 18 + (offset === 0 ? 112 : 0);
            const rotation = -angle * (180 / Math.PI) * 0.8;
            const scale = 1 - Math.min(distance, 3) * (isMobile ? 0.045 : 0.055);

            return (
              <button
                key={item.href}
                type="button"
                className={`mirror-portal${offset === 0 ? " is-selected" : ""}${entering && offset === 0 ? " is-entering" : ""}`}
                style={{
                  left: "50%",
                  zIndex: navigation.length - distance,
                  opacity: distance > 3 ? 0 : 1 - Math.min(distance, 3) * 0.14,
                  transform: `translate3d(calc(-50% + ${x}px), -50%, ${depth}px) rotateY(${rotation}deg) scale(${scale})`,
                  pointerEvents: distance > 3 ? "none" : "auto",
                }}
                aria-pressed={offset === 0}
                aria-hidden={distance > 3}
                aria-label={`${String(index + 1).padStart(2, "0")} ${item.label}${offset === 0 ? ", selected" : ""}`}
                tabIndex={distance > 3 ? -1 : 0}
                onClick={() => {
                  if (index === selectedIndex) activateSelection();
                  else selectIndex(index);
                }}
              >
                <span className="mirror-portal__reflection" aria-hidden="true" />
                <span className="mirror-portal__topline">
                  <span className="mirror-portal__number">{String(index + 1).padStart(2, "0")}</span>
                  <span className="mirror-portal__mark" aria-hidden="true">{item.mark}</span>
                </span>
                <span className="mirror-portal__copy">
                  <span className="mirror-portal__title">{item.label}</span>
                  <span className="mirror-portal__description">{item.description}</span>
                </span>
                <span className="mirror-portal__bottomline">
                  <span>{item.planned ? "SECTION PLANNED" : offset === 0 ? "SELECTED DESTINATION" : "R MATRIX PORTAL"}</span>
                  <span aria-hidden="true">↗</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <footer className="mirror-hall__controls">
        <button className="mirror-hall__arrow" type="button" onClick={() => moveSelection(-1)} aria-label="Previous destination">
          <span aria-hidden="true">←</span>
        </button>
        <div className="mirror-hall__selection">
          <span className="mirror-hall__selection-count">{String(selectedIndex + 1).padStart(2, "0")} / {String(navigation.length).padStart(2, "0")}</span>
          <span className="mirror-hall__selection-title">{active.label}</span>
          {(selectedIndex === 0 || selectedIndex === navigation.length - 1) && (
            <span className="mirror-hall__endpoint" aria-live="polite">
              {selectedIndex === 0 ? "BEGINNING OF HALL" : "END OF HALL"}
            </span>
          )}
          <span className="mirror-hall__dots" aria-label={`Destination ${selectedIndex + 1} of ${navigation.length}`}>
            {navigation.map((item, index) => (
              <button
                key={item.href}
                type="button"
                className={`mirror-hall__dot${index === selectedIndex ? " is-selected" : ""}`}
                onClick={() => selectIndex(index)}
                aria-label={`Select ${item.label}`}
                aria-current={index === selectedIndex ? "step" : undefined}
              />
            ))}
          </span>
        </div>
        <button className="mirror-hall__arrow" type="button" onClick={() => moveSelection(1)} aria-label="Next destination">
          <span aria-hidden="true">→</span>
        </button>
        <Link
          href={active.href}
          className="mirror-hall__enter"
          onClick={(event) => {
            event.preventDefault();
            activateSelection();
          }}
        >
          Enter {active.label}<span aria-hidden="true">↗</span>
        </Link>
        <span className="mirror-hall__keyboard-hint">SCROLL TO EXPLORE <span aria-hidden="true">·</span> ← → SELECT <span aria-hidden="true">·</span> ENTER TO COMMAND <span aria-hidden="true">·</span> ESC EXIT</span>
      </footer>
    </section>
  );
}
