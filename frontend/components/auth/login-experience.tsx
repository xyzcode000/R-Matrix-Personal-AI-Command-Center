"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { OtpInput } from "./otp-input";

type Stage = "credentials" | "sending" | "code" | "verifying" | "success";

type LoginResponse = {
  username?: string;
  two_factor?: boolean;
  challenge_id?: string;
  expires_at?: string;
  resend_at?: string;
  delivery?: string;
};

type ChallengeResponse = {
  expires_at?: string;
  resend_at?: string;
};

const CODE_LENGTH = 6;

function secondsUntil(target: number) {
  return Math.max(0, Math.ceil((target - Date.now()) / 1000));
}

function formatCountdown(total: number) {
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/**
 * Two-step owner sign-in: password first, then a Telegram code when the
 * backend reports that a code can actually be delivered.
 */
export function LoginExperience({ onAuthenticated }: Readonly<{ onAuthenticated: () => void }>) {
  const [stage, setStage] = useState<Stage>("credentials");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [code, setCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [resendAt, setResendAt] = useState(0);
  const [expiresAt, setExpiresAt] = useState(0);
  const [, setTick] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const usernameRef = useRef<HTMLInputElement>(null);
  const submitted = useRef("");

  const resendSeconds = secondsUntil(resendAt);
  const expirySeconds = secondsUntil(expiresAt);
  const expired = Boolean(expiresAt) && expirySeconds <= 0;

  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (stage === "credentials") usernameRef.current?.focus();
  }, [stage]);

  const verify = useCallback(
    async (candidate: string, challenge: string) => {
      if (candidate.length !== CODE_LENGTH || submitted.current === candidate) return;
      submitted.current = candidate;
      setStage("verifying");
      setError("");
      try {
        await api("/api/auth/verify-otp", {
          method: "POST",
          body: JSON.stringify({ challenge_id: challenge, code: candidate }),
        });
        setStage("success");
        window.setTimeout(onAuthenticated, 700);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "The verification code was not accepted.");
        setCode("");
        setStage("code");
        window.setTimeout(() => {
          submitted.current = "";
        }, 120);
      }
    },
    [onAuthenticated],
  );

  useEffect(() => {
    if (stage !== "code") return;
    if (code.length === CODE_LENGTH) void verify(code, challengeId);
  }, [challengeId, code, stage, verify]);

  async function submitCredentials(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (stage === "sending") return;
    setError("");
    setNotice("");
    setStage("sending");
    try {
      await api("/api/auth/csrf");
      const result = await api<LoginResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password }),
      });
      if (result.two_factor && result.challenge_id) {
        setChallengeId(result.challenge_id);
        setResendAt(result.resend_at ? Date.parse(result.resend_at) : Date.now() + 30_000);
        setExpiresAt(result.expires_at ? Date.parse(result.expires_at) : Date.now() + 300_000);
        setCode("");
        setPassword("");
        submitted.current = "";
        setStage("code");
        return;
      }
      setPassword("");
      setStage("success");
      window.setTimeout(onAuthenticated, 650);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed. Try again.");
      setStage("credentials");
    }
  }

  async function resendCode() {
    if (resendSeconds > 0 || !challengeId) return;
    setError("");
    setNotice("");
    try {
      const result = await api<ChallengeResponse>("/api/auth/resend-otp", {
        method: "POST",
        body: JSON.stringify({ challenge_id: challengeId }),
      });
      setResendAt(result.resend_at ? Date.parse(result.resend_at) : Date.now() + 30_000);
      if (result.expires_at) setExpiresAt(Date.parse(result.expires_at));
      setCode("");
      submitted.current = "";
      setNotice("A new verification code was sent to Telegram.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "A new code could not be sent. Try again.");
    }
  }

  async function restart() {
    if (challengeId) {
      void api("/api/auth/cancel-otp", {
        method: "POST",
        body: JSON.stringify({ challenge_id: challengeId }),
      }).catch(() => undefined);
    }
    setChallengeId("");
    setCode("");
    setPassword("");
    setError("");
    setNotice("");
    setResendAt(0);
    setExpiresAt(0);
    submitted.current = "";
    setStage("credentials");
  }

  const inCodeStep = stage === "code" || stage === "verifying" || stage === "success";

  return (
    <section className="auth-screen" aria-label="Owner sign-in">
      <div className="auth-screen__atmosphere" aria-hidden="true" />
      <div className="auth-screen__frame">
        <div className="auth-panel">
          <header className="auth-panel__brand">
            <span className="auth-brand-mark" aria-hidden="true">R</span>
            <span className="auth-brand-copy">
              <strong>R MATRIX <i>AI</i></strong>
              <small>PERSONAL COMMAND CENTER</small>
            </span>
          </header>

          {inCodeStep ? (
            <div className="auth-panel__body auth-panel__body--otp">
              <div
                className={`auth-orbit${stage === "verifying" ? " is-busy" : ""}${stage === "success" ? " is-verified" : ""}`}
                aria-hidden="true"
              >
                <span className="auth-orbit__ring" />
                <span className="auth-orbit__ring auth-orbit__ring--inner" />
                <span className="auth-orbit__core">{stage === "success" ? "✓" : "R"}</span>
              </div>

              <p className="auth-panel__eyebrow">TWO-STEP VERIFICATION</p>
              <h1 className="auth-panel__title">Check your Telegram</h1>
              <p className="auth-panel__lede">Enter the verification code sent by R Matrix.</p>

              <OtpInput
                value={code}
                onChange={setCode}
                disabled={stage === "verifying" || stage === "success" || expired}
                invalid={Boolean(error)}
                autoFocus
              />

              <div className="auth-panel__meta">
                <span className={`auth-panel__timer${expired ? " is-expired" : ""}`}>
                  {expired ? "Code expired" : `Expires in ${formatCountdown(expirySeconds)}`}
                </span>
                <button
                  type="button"
                  className="auth-panel__resend"
                  onClick={() => void resendCode()}
                  disabled={resendSeconds > 0 || expired || stage === "verifying"}
                >
                  {resendSeconds > 0 ? `Resend in ${resendSeconds}s` : "Resend code"}
                </button>
              </div>

              <div className="auth-panel__messages" aria-live="polite">
                {error ? <p className="auth-panel__error" role="alert">{error}</p> : null}
                {!error && notice ? <p className="auth-panel__notice">{notice}</p> : null}
                {stage === "verifying" ? <p className="auth-panel__notice">Verifying…</p> : null}
                {stage === "success" ? (
                  <p className="auth-panel__success">Verified — opening your command center…</p>
                ) : null}
              </div>

              <button type="button" className="auth-panel__back" onClick={() => void restart()}>
                <span aria-hidden="true">←</span> Use a different account
              </button>
            </div>
          ) : (
            <div className="auth-panel__body">
              <p className="auth-panel__eyebrow">OWNER ACCESS</p>
              <h1 className="auth-panel__title">Welcome back.</h1>
              <p className="auth-panel__lede">Your command center is waiting.</p>

              <form className="auth-form" onSubmit={(event) => void submitCredentials(event)} noValidate>
                <label className="auth-field">
                  <span className="auth-field__label">Username</span>
                  <input
                    ref={usernameRef}
                    className="auth-field__input"
                    type="text"
                    name="username"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    maxLength={80}
                    required
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                  />
                </label>

                <label className="auth-field">
                  <span className="auth-field__label">Password</span>
                  <span className="auth-field__control">
                    <input
                      className="auth-field__input auth-field__input--password"
                      type={showPassword ? "text" : "password"}
                      name="password"
                      autoComplete="current-password"
                      maxLength={200}
                      required
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                    />
                    <button
                      type="button"
                      className="auth-field__toggle"
                      onClick={() => setShowPassword((visible) => !visible)}
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      aria-pressed={showPassword}
                    >
                      {showPassword ? "Hide" : "Show"}
                    </button>
                  </span>
                </label>

                <div className="auth-panel__messages" aria-live="polite">
                  {error ? <p className="auth-panel__error" role="alert">{error}</p> : null}
                </div>

                <button
                  type="submit"
                  className="auth-submit"
                  disabled={stage === "sending" || !username.trim() || !password}
                >
                  {stage === "sending" ? (
                    <>
                      <span className="auth-submit__spinner" aria-hidden="true" /> Verifying credentials…
                    </>
                  ) : (
                    <>
                      Continue <span aria-hidden="true">→</span>
                    </>
                  )}
                </button>
              </form>

              <p className="auth-panel__hint">
                A verification code is sent to your paired Telegram chat after the password is
                accepted. Sessions expire automatically and end when you sign out.
              </p>
            </div>
          )}
        </div>

        <p className="auth-screen__foot">
          PRIVATE SYSTEM <span aria-hidden="true">·</span> SINGLE OWNER <span aria-hidden="true">·</span> LOCAL-FIRST
        </p>
      </div>
    </section>
  );
}
