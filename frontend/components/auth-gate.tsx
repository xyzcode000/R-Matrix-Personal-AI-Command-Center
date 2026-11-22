"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { LoginExperience } from "./auth/login-experience";

type AuthState = "checking" | "required" | "open";

/**
 * Keeps the private workspace behind the existing owner session.
 * Sign-in reuses the FastAPI owner authentication endpoints; no second auth system.
 */
export function AuthGate({ children }: Readonly<{ children: React.ReactNode }>) {
  const [state, setState] = useState<AuthState>("checking");
  const router = useRouter();

  // Signing in opens the original five-part website (the root experience), never
  // the standalone dashboard route.
  const handleAuthenticated = useCallback(() => {
    setState("open");
    window.setTimeout(() => {
      if (window.location.pathname !== "/") router.replace("/");
    }, 0);
  }, [router]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const health = await api<{ auth_required?: boolean }>("/api/health");
        if (health.auth_required === false) {
          if (active) setState("open");
          return;
        }
        await api("/api/auth/me");
        if (active) setState("open");
      } catch {
        if (active) setState("required");
      }
    })();
    const onUnauthorized = () => setState("required");
    const onSignedOut = () => setState("required");
    window.addEventListener("r-matrix:unauthorized", onUnauthorized);
    window.addEventListener("r-matrix:signed-out", onSignedOut);
    return () => {
      active = false;
      window.removeEventListener("r-matrix:unauthorized", onUnauthorized);
      window.removeEventListener("r-matrix:signed-out", onSignedOut);
    };
  }, []);

  // Broadcast the resolved auth state so shared chrome (for example the Mirror
  // Hall trigger) is only offered to an authenticated owner. The gate itself
  // remains the single route guard.
  useEffect(() => {
    if (state === "checking") return undefined;
    const timer = window.setTimeout(() => {
      window.dispatchEvent(
        new Event(state === "open" ? "r-matrix:authenticated" : "r-matrix:signed-out"),
      );
    }, 0);
    return () => window.clearTimeout(timer);
  }, [state]);

  if (state === "checking") {
    return (
      <div className="auth-boot" role="status" aria-live="polite">
        <span className="auth-boot__mark" aria-hidden="true">R</span>
        <span>Checking the owner session…</span>
      </div>
    );
  }

  if (state === "open") return <>{children}</>;

  return <LoginExperience onAuthenticated={handleAuthenticated} />;
}
