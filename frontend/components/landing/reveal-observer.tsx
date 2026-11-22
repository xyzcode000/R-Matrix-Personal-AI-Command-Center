"use client";

import { useEffect } from "react";

export function RevealObserver() {
  useEffect(() => {
    const landingPage = document.querySelector(".landing-page");
    landingPage?.classList.add("landing-page--enhanced");
    const targets = document.querySelectorAll<HTMLElement>("[data-reveal]");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (reducedMotion || !("IntersectionObserver" in window)) {
      targets.forEach((target) => target.classList.add("is-revealed"));
      return () => landingPage?.classList.remove("landing-page--enhanced");
    }

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-revealed");
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });

    targets.forEach((target) => observer.observe(target));
    return () => {
      observer.disconnect();
      landingPage?.classList.remove("landing-page--enhanced");
    };
  }, []);

  return null;
}
