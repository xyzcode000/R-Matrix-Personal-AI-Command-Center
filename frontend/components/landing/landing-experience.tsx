"use client";

import Image from "next/image";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { navigation } from "@/components/navigation";
import { TunnelTransition } from "@/components/tunnel/tunnel-transition";
import { MirrorHallSection } from "./mirror-hall-section";
import { RevealObserver } from "./reveal-observer";
import { SectionNavigation } from "./section-navigation";

const commandRoutes = navigation;

const capabilities = [
  {
    href: "/chat",
    label: "AI CONVERSATION",
    title: "A place to think out loud.",
    description: "Continue conversations with the configured AI provider, personal context, and connected tools.",
    state: "active",
    status: "ACTIVE",
  },
  {
    href: "/documents",
    label: "DOCUMENT INTELLIGENCE",
    title: "Your knowledge, in reach.",
    description: "Upload originals, read extracted pages and retrieve exact values with citations. Scanned pages use the local OCR engine.",
    state: "active",
    status: "ACTIVE",
  },
  {
    href: "/documents",
    label: "PERSONAL KNOWLEDGE",
    title: "An archive that answers.",
    description: "One private library of identity papers, statements and records, searchable by name, description and page text.",
    state: "active",
    status: "ACTIVE",
  },
  {
    href: "/projects",
    label: "PROJECTS",
    title: "Work with clear boundaries.",
    description: "Search project folders that you have explicitly authorized in the application.",
    state: "read-only",
    status: "READ ONLY",
  },
  {
    href: "/task-history",
    label: "TASK HISTORY",
    title: "Pick up the thread.",
    description: "Track tasks, execution status, and activity as work moves forward.",
    state: "active",
    status: "ACTIVE",
  },
  {
    href: "/agents",
    label: "AGENTS & AUTOMATION",
    title: "Specialists with limits.",
    description: "Document, project and research specialists with recorded executions and explicit approval gates.",
    state: "active",
    status: "ACTIVE",
  },
  {
    href: "/agents",
    label: "MEMORY",
    title: "Context you approve.",
    description: "Proposed memories stay pending until you accept them. Nothing is learned silently.",
    state: "gated",
    status: "APPROVAL GATED",
  },
  {
    href: "/remote",
    label: "REMOTE ACCESS",
    title: "Your connected environments.",
    description: "Manage the existing remote-access connections configured for this workspace.",
    state: "active",
    status: "CONTROL PLANE",
  },
];

const sections = [
  { id: "introduction", label: "Introduction", mark: "01" },
  { id: "about", label: "Identity", mark: "02" },
  { id: "capabilities", label: "Capabilities", mark: "03" },
  { id: "command-center", label: "Command center", mark: "04" },
  { id: "mirror-hall", label: "Mirror Hall", mark: "05" },
];

export function LandingExperience() {
	const [sceneIndex, setSceneIndex] = useState(0);
	const [tunnel, setTunnel] = useState<{ from: number; to: number } | null>(null);
	const [welcome, setWelcome] = useState<"first" | "returning">("first");
	const sceneIndexRef = useRef(0);
	const finishTunnel = useCallback(() => setTunnel(null), []);

	// The first visit gets the full cinematic reveal; later visits keep the same
	// personal greeting but drop the entrance delays so daily use stays quick.
	useEffect(() => {
		const key = "rmatrix.welcome.seen";
		if (window.localStorage.getItem(key)) {
			setWelcome("returning");
			return;
		}
		window.localStorage.setItem(key, new Date().toISOString());
	}, []);

	// Track which of the five scenes owns the viewport and play the tunnel when
	// the scene changes. The section order here is the scene order.
	useEffect(() => {
		const ids = sections.map((section) => section.id);
		const elements = ids
			.map((id) => document.getElementById(id))
			.filter((element): element is HTMLElement => element !== null);
		if (!elements.length) return undefined;

		const observer = new IntersectionObserver(
			(entries) => {
				const visible = entries
					.filter((entry) => entry.isIntersecting)
					.sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
				if (!(visible?.target instanceof HTMLElement)) return;
				const next = ids.indexOf(visible.target.id);
				if (next < 0 || next === sceneIndexRef.current) return;
				const previous = sceneIndexRef.current;
				sceneIndexRef.current = next;
				setSceneIndex(next);
				setTunnel({ from: previous, to: next });
			},
			{ rootMargin: "-25% 0px -50% 0px", threshold: [0, 0.15, 0.35, 0.6] },
		);

		elements.forEach((element) => observer.observe(element));
		return () => observer.disconnect();
	}, []);

  return (
    <div
      className={`landing-page${welcome === "returning" ? " is-returning" : ""}`}
      data-scene={sections[sceneIndex]?.id}
      data-tunnel={tunnel ? "playing" : "idle"}
      data-welcome={welcome}
    >
      {tunnel && (
        <TunnelTransition
          key={`${tunnel.from}-${tunnel.to}`}
          showRobot={tunnel.from === 0 && tunnel.to === 1}
          duration={welcome === "returning" ? 760 : 1400}
          onComplete={finishTunnel}
        />
      )}
      <RevealObserver />
      <SectionNavigation sections={sections} />
      <section id="introduction" className="landing-intro" aria-labelledby="landing-title">
        <Image
          className="landing-intro__portrait"
          src="/images/r-matrix-portrait.png"
          alt="Rahil Wankhede in a black suit with arms crossed"
          fill
          priority
          sizes="100vw"
        />
        <div className="landing-intro__veil" aria-hidden="true" />
        <div className="landing-intro__frame" aria-hidden="true">
          <span>R / 01</span>
          <span>PRIVATE SYSTEM · EST. FOR ONE</span>
        </div>
        <div className="landing-intro__copy">
          <p className="landing-greeting">
            <span className="landing-greeting__salute">hi Rahil,</span>
            <span className="landing-greeting__welcome">rmatrix welcomes u</span>
          </p>
          <p className="landing-kicker"><span /> A PRIVATE INTELLIGENCE ENVIRONMENT</p>
          <p className="landing-intro__statement">BUILT AROUND THE WAY I THINK,<br />WORK, CREATE, AND BUILD.</p>
          <h1 id="landing-title">R MATRIX <span>AI</span></h1>
          <p className="landing-intro__subtitle">A personal intelligence and command center.</p>
          <div className="landing-intro__actions">
            <Link className="landing-button" href="/dashboard">Enter the command center <span aria-hidden="true">↗</span></Link>
            <a className="landing-text-link" href="#about">Discover the system <span aria-hidden="true">↓</span></a>
          </div>
        </div>
        <a className="landing-scroll-cue" href="#about" aria-label="Scroll to learn about R Matrix AI">
          <span>SCROLL TO ENTER</span><i aria-hidden="true" />
        </a>
        <span className="landing-intro__index" aria-hidden="true">01 — 05</span>
      </section>

      <section id="about" className="landing-section identity-section" aria-labelledby="identity-title">
        <div className="landing-section__heading">
          <p className="landing-kicker"><span /> ONE HUMAN. AN EXPANDING INTELLIGENCE.</p>
          <span className="landing-section__index">02 / IDENTITY</span>
        </div>
        <div className="identity-layout" data-reveal>
          <div className="identity-portrait">
            <Image src="/images/r-matrix-portrait.png" alt="Portrait of Rahil Wankhede" fill sizes="(max-width: 760px) 90vw, 42vw" />
            <span className="identity-portrait__caption">RAHIL WANKHEDE <i>·</i> BUILDER</span>
          </div>
          <div className="identity-copy">
            <p className="identity-copy__overline">THE HUMAN BEHIND THE SYSTEM.</p>
            <h2 id="identity-title">One human.<br /><em>An expanding intelligence.</em></h2>
            <p className="identity-copy__name">Rahil Wankhede</p>
            <p className="identity-copy__roles">Full Stack Developer <span>/</span> AI &amp; Automation Enthusiast</p>
            <p className="identity-copy__body">Created by Rahil Wankhede, R Matrix is a personal AI command center for knowledge, conversations, documents, projects, and daily operations. It brings useful context together while keeping control over what stays local and what gets shared.</p>
            <div className="identity-meta">
              <span><b>01</b> PERSONAL BY DESIGN</span>
              <span><b>02</b> BUILT TO STAY IN CONTEXT</span>
              <span><b>03</b> MADE FOR THE WORK AHEAD</span>
            </div>
            <Link className="landing-inline-link" href="/dashboard">A closer look at the workspace <span aria-hidden="true">↗</span></Link>
          </div>
        </div>
      </section>

      <section id="capabilities" className="landing-section capabilities-section" aria-labelledby="capabilities-title">
        <div className="landing-section__heading">
          <p className="landing-kicker"><span /> BUILT FOR THE WAY WORK HAPPENS</p>
          <span className="landing-section__index">03 / CAPABILITIES</span>
        </div>
        <div className="capabilities-intro" data-reveal>
          <h2 id="capabilities-title">One system.<br /><em>Many ways forward.</em></h2>
          <p>Every capability below connects to a real destination in the command center. No imagined tools—just the workspace that exists today.</p>
        </div>
        <div className="capability-index" data-reveal>
          {capabilities.map((capability, index) => (
            <Link className="capability-row" href={capability.href} key={`${capability.label}-${capability.href}`}>
              <span className="capability-row__number">{String(index + 1).padStart(2, "0")}</span>
              <span className="capability-row__body">
                <span className="capability-row__label">{capability.label}</span>
                <strong className="capability-row__title">{capability.title}</strong>
                <span className="capability-row__description">{capability.description}</span>
              </span>
              <span className="capability-row__status" data-state={capability.state}>{capability.status}</span>
              <span className="capability-row__arrow" aria-hidden="true">↗</span>
            </Link>
          ))}
        </div>
      </section>

      <section id="command-center" className="landing-section command-section" aria-labelledby="command-title">
        <div className="landing-section__heading">
          <p className="landing-kicker"><span /> THE WORKSPACE, IN VIEW</p>
          <span className="landing-section__index">04 / COMMAND CENTER</span>
        </div>
        <div className="command-layout">
          <div className="command-copy" data-reveal>
            <p className="command-copy__overline">YOUR TOOLS. ONE PRIVATE ENVIRONMENT.</p>
            <h2 id="command-title">More than a chatbot.<br /><em>A system that works with you.</em></h2>
            <p>The working command center brings conversations, documents, projects, and task activity together. Open a destination to use the real application—your existing routes and integrations remain in place.</p>
            <Link href="/dashboard" className="landing-button">Enter the command center <span aria-hidden="true">↗</span></Link>
            <div className="command-assurance"><span className="command-assurance__dot" /> LOCAL-FIRST WORKSPACE <i /> EXPLICIT SHARING</div>
          </div>
          <div className="command-map" aria-label="Command center route map" data-reveal>
            <div className="command-map__top"><span>R MATRIX / ROUTE MAP</span><span>{String(commandRoutes.length).padStart(2, "0")} DESTINATIONS</span></div>
            <div className="command-map__hero">
              <span className="command-map__glyph">R<span>.</span></span>
              <div><strong>PERSONAL COMMAND CENTER</strong><small>One environment for the work in motion.</small></div>
              <Link href="/dashboard" aria-label="Open the command center dashboard">↗</Link>
            </div>
            <div className="command-map__routes">
              {commandRoutes.map((item, index) => (
                <Link href={item.href} key={item.href} className="command-route">
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{item.label}</strong>
                  <small>{item.description}</small>
                  <i aria-hidden="true">↗</i>
                </Link>
              ))}
            </div>
            <div className="command-map__bottom"><span>CONNECTED TO THE REAL WORKSPACE</span><span>LOCAL · PRIVATE · YOURS</span></div>
          </div>
        </div>
      </section>

      <MirrorHallSection />
      <footer className="landing-footer">
        <Link href="/" className="landing-footer__brand">R MATRIX <span>AI</span></Link>
        <span>PERSONAL AI COMMAND CENTER · BUILT AROUND ONE MIND</span>
        <Link href="/settings">Workspace settings <span aria-hidden="true">↗</span></Link>
      </footer>
    </div>
  );
}
