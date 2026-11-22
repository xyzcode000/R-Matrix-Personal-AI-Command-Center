"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, api } from "./api";
import { AuthGate } from "./auth-gate";
import { EnergyRibbon } from "./energy-ribbon";
import { MirrorHall } from "./mirror-hall";
import { navigation, navigationGroups } from "./navigation";

type IntroPhase = "checking" | "playing" | "leaving" | "finished";

export function AppShell({ children }: Readonly<{ children: React.ReactNode }>) {
  const pathname = usePathname();
  const activeDestination = navigation.find((item) => item.href === pathname);
  const activeIndex = navigation.findIndex((item) => item.href === pathname);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [introPhase, setIntroPhase] = useState<IntroPhase>(pathname === "/" ? "finished" : "checking");
  const [mirrorHallOpen, setMirrorHallOpen] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const skipIntroButton = useRef<HTMLButtonElement>(null);
  const mirrorHallTrigger = useRef<HTMLButtonElement>(null);
  const initialPathname = useRef(pathname);
  const introOverlayVisible = pathname !== "/" && introPhase !== "finished";
  const groups = navigationGroups.map((label) => ({
    label,
    items: navigation.filter((item) => item.group === label),
  }));

  const closeMirrorHall = useCallback(() => {
    setMirrorHallOpen(false);
    window.requestAnimationFrame(() => mirrorHallTrigger.current?.focus());
  }, []);

  const finishIntro = useCallback(() => {
    skipIntroButton.current?.blur();
    setIntroPhase("leaving");
  }, []);

  const signOut = useCallback(async () => {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      // The local session may already have expired; close it in the interface either way.
    }
    window.dispatchEvent(new Event("r-matrix:signed-out"));
    setMirrorHallOpen(false);
    window.setTimeout(() => setSigningOut(false), 120);
  }, [signingOut]);

  useEffect(() => {
    if (initialPathname.current !== "/") setIntroPhase("playing");
  }, []);

  useEffect(() => {
    if (pathname === "/" && introPhase !== "finished") setIntroPhase("finished");
  }, [introPhase, pathname]);

  useEffect(() => {
    if (introPhase !== "playing" || pathname === "/") return undefined;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timeout = window.setTimeout(finishIntro, reducedMotion ? 850 : 3000);
    return () => window.clearTimeout(timeout);
  }, [finishIntro, introPhase, pathname]);

  useEffect(() => {
    if (introPhase !== "leaving") return undefined;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timeout = window.setTimeout(() => {
      setIntroPhase("finished");
    }, reducedMotion ? 50 : 550);
    return () => window.clearTimeout(timeout);
  }, [introPhase]);

  useEffect(() => {
    if (introPhase === "playing" && pathname !== "/") skipIntroButton.current?.focus();
  }, [introPhase, pathname]);

  useEffect(() => {
    const onAuthenticated = () => setAuthenticated(true);
    const onSignedOut = () => setAuthenticated(false);
    window.addEventListener("r-matrix:authenticated", onAuthenticated);
    window.addEventListener("r-matrix:signed-out", onSignedOut);
    return () => {
      window.removeEventListener("r-matrix:authenticated", onAuthenticated);
      window.removeEventListener("r-matrix:signed-out", onSignedOut);
    };
  }, []);

  useEffect(() => {
    if (!authenticated) setMirrorHallOpen(false);
  }, [authenticated]);

  useEffect(() => {
    if (introPhase === "finished" || pathname === "/") return undefined;

    const previousOverflow = document.body.style.overflow;
    const previousOverscrollBehavior = document.body.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.body.style.overscrollBehavior = "none";

    return () => {
      document.body.style.overflow = previousOverflow;
      document.body.style.overscrollBehavior = previousOverscrollBehavior;
    };
  }, [introPhase, pathname]);

  useEffect(() => {
    let active = true;
    const check = () => api<{ status: string }>("/api/health")
      .then(() => active && setBackendOnline(true))
      .catch(() => active && setBackendOnline(false));
    void check();
    const interval = window.setInterval(check, 15000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const backendLabel = backendOnline === true ? "Connected" : backendOnline === false ? "Offline" : "Connecting";

  return (
    <div className={`app-frame${pathname === "/" ? " app-frame--landing" : ""}`}>
      {pathname === "/" && <EnergyRibbon />}
      {introOverlayVisible && (
        <section
          className={`cinematic-intro cinematic-intro--${introPhase}`}
          role="dialog"
          aria-modal={introPhase === "playing"}
          aria-labelledby="cinematic-intro-title"
          aria-hidden={introPhase === "checking" || introPhase === "leaving"}
          onKeyDown={(event) => {
            if (introPhase !== "playing") return;
            if (event.key === "Escape") {
              finishIntro();
            } else if (event.key === "Tab") {
              event.preventDefault();
              skipIntroButton.current?.focus();
            }
          }}
        >
          <div className="cinematic-intro__portrait" aria-hidden="true" />
          <div className="cinematic-intro__architecture" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div className="cinematic-intro__shade" aria-hidden="true" />
          <div className="cinematic-intro__welcome">
            <div className="cinematic-intro__identity">
              <span className="cinematic-intro__monogram" aria-hidden="true">R</span>
              <p className="cinematic-intro__eyebrow">A PRIVATE SPACE, DESIGNED FOR YOU</p>
            </div>
            <h1 id="cinematic-intro-title">R MATRIX AI</h1>
            <p className="cinematic-intro__subtitle">PERSONAL AI COMMAND CENTER</p>
            <div className="cinematic-intro__divider" aria-hidden="true" />
            <div className="cinematic-intro__status" aria-label="Your command center awaits">
              <p className="cinematic-intro__status-heading">A MOMENT BEFORE WHAT&apos;S NEXT</p>
              <p className="cinematic-intro__status-line">YOUR WORKSPACE, IN FOCUS</p>
              <p className="cinematic-intro__status-line">THE MIRROR HALL AWAITS</p>
            </div>
            <p className="cinematic-intro__signature">WELCOME, RAHIL.</p>
          </div>
          <button
            ref={skipIntroButton}
            className="cinematic-intro__skip"
            type="button"
            onClick={finishIntro}
            tabIndex={introPhase === "playing" ? 0 : -1}
          >
            Skip introduction
          </button>
        </section>
      )}
      {mirrorHallOpen && <MirrorHall onClose={closeMirrorHall} />}
      <div
        className="app-content"
        aria-hidden={introOverlayVisible || mirrorHallOpen}
        inert={introOverlayVisible || mirrorHallOpen}
      >
        {pathname !== "/" && (
          <a className="skip-link" href="#main-content">Skip to workspace</a>
        )}
        {pathname !== "/" && (
          <aside className="sidebar" aria-label="Command center destinations">
            <Link href="/dashboard" className="brand-lockup" aria-label="R MATRIX AI dashboard">
              <span className="brand-glyph">R</span>
              <span>
                <strong>R MATRIX AI</strong>
                <small>PERSONAL AI COMMAND CENTER</small>
              </span>
            </Link>

            <p className="sidebar-owner">
              <span className="live-indicator" aria-hidden="true" />
              <span>PRIVATE · OWNER WORKSPACE</span>
            </p>

            <nav className="side-nav" aria-label="Primary destinations">
              {groups.map((group) => (
                <div className="side-nav__group" key={group.label}>
                  <p className="side-nav__caption">{group.label}</p>
                  {group.items.map((item) => (
                    <Link
                      key={item.href}
                      href={item.href}
                      className={`nav-link${pathname === item.href ? " active" : ""}`}
                      aria-current={pathname === item.href ? "page" : undefined}
                    >
                      <span className="nav-mark" aria-hidden="true">{item.mark}</span>
                      <span className="nav-copy">
                        <strong>{item.label}</strong>
                        <small>{item.description}</small>
                      </span>
                      <span className="nav-index" aria-hidden="true">
                        {String(navigation.findIndex((entry) => entry.href === item.href) + 1).padStart(2, "0")}
                      </span>
                    </Link>
                  ))}
                </div>
              ))}
            </nav>

            <div className="sidebar-bottom">
              <div className="sidebar-status">
                <span className={`status-dot ${backendOnline === true ? "online" : backendOnline === false ? "offline" : ""}`} aria-hidden="true" />
                <span>{backendLabel}</span>
                <small title={API_BASE}>LOCAL API</small>
              </div>
              {authenticated ? (
                <button
                  ref={mirrorHallTrigger}
                  className="mirror-hall-trigger"
                  type="button"
                  onClick={() => setMirrorHallOpen(true)}
                  aria-haspopup="dialog"
                  aria-expanded={mirrorHallOpen}
                  aria-label="Open Mirror Hall navigation"
                >
                  <span aria-hidden="true">⌘</span>
                  <span>Mirror Hall</span>
                </button>
              ) : null}
              {authenticated ? (
                <button
                  className="sidebar-signout"
                  type="button"
                  onClick={() => void signOut()}
                  disabled={signingOut}
                >
                  {signingOut ? "Closing session…" : "Sign out"}
                </button>
              ) : null}
              <p className="sidebar-legal">PRIVATE SYSTEM · SINGLE OWNER · LOCAL-FIRST</p>
            </div>
          </aside>
        )}

        <main className="main-area">
          {pathname !== "/" && (
            <header className="topbar">
              <div className="topbar-context">
                <span className="live-indicator" aria-hidden="true" />
                <span>{activeDestination?.label ?? "Private workspace"}</span>
                <span className="topbar-rule" aria-hidden="true" />
                <span className="topbar-sub">
                  R MATRIX / {activeIndex >= 0 ? String(activeIndex + 1).padStart(2, "0") : "—"}
                </span>
              </div>
              <div className="topbar-right">
                <span
                  className="topbar-status"
                  title={`${backendLabel} · ${API_BASE.replace(/^https?:\/\//, "")}`}
                >
                  <span className={`status-dot ${backendOnline === true ? "online" : backendOnline === false ? "offline" : ""}`} />
                  <span>{backendLabel}</span>
                </span>
                <Link className="topbar-about" href="/#about">The world</Link>
                <span className="avatar" aria-hidden="true">R</span>
              </div>
            </header>
          )}
          <div className="page-content" id="main-content">
            <AuthGate>{children}</AuthGate>
          </div>
        </main>
      </div>
    </div>
  );
}
