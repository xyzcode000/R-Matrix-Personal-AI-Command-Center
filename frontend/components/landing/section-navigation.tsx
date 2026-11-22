"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type SectionLink = Readonly<{ id: string; label: string; mark: string }>;
type SectionNavigationProps = Readonly<{ sections: readonly SectionLink[] }>;

export function SectionNavigation({ sections }: SectionNavigationProps) {
  const [activeSection, setActiveSection] = useState(sections[0]?.id ?? "");

  useEffect(() => {
    const observedSections = sections
      .map(({ id }) => document.getElementById(id))
      .filter((section): section is HTMLElement => section !== null);
    if (!observedSections.length) return undefined;

    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((left, right) => right.intersectionRatio - left.intersectionRatio);
      if (visible[0]?.target instanceof HTMLElement) setActiveSection(visible[0].target.id);
    }, { rootMargin: "-25% 0px -50% 0px", threshold: [0, 0.15, 0.35, 0.6] });

    observedSections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [sections]);

  return (
    <>
      <Link href="/" className="landing-brand" aria-label="R MATRIX AI home">
        <span className="landing-brand__mark">R</span>
        <span><strong>R MATRIX <i>AI</i></strong><small>PERSONAL INTELLIGENCE ENVIRONMENT</small></span>
      </Link>
      <nav className="section-navigation" aria-label="Landing page sections">
        {sections.map((section) => (
          <a
            href={`#${section.id}`}
            key={section.id}
            aria-label={section.label}
            aria-current={activeSection === section.id ? "location" : undefined}
            className={activeSection === section.id ? "is-active" : ""}
          >
            <span>{section.mark}</span><small>{section.label}</small>
          </a>
        ))}
      </nav>
    </>
  );
}
