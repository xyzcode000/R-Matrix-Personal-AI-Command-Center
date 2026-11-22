"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, remoteApi } from "../../components/api";

type Device = {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
};

type ControlJob = {
  id: string;
  device_id: string;
  operation: string;
  status: string;
  result: Record<string, string> | null;
  created_at: string;
};

export default function RemotePage() {
  const [signedInAs, setSignedInAs] = useState<string | null>(null);
  const [authState, setAuthState] = useState<"loading" | "signed-out" | "signed-in">("loading");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [devices, setDevices] = useState<Device[]>([]);
  const [jobs, setJobs] = useState<ControlJob[]>([]);
  const [enrollmentCode, setEnrollmentCode] = useState<string | null>(null);
  const [selectedDevice, setSelectedDevice] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadControlPlane = useCallback(async () => {
    let current: { username: string };
    try {
      current = await remoteApi<{ username: string }>("/api/auth/me");
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setSignedInAs(null);
        setAuthState("signed-out");
      }
      throw reason;
    }
    setSignedInAs(current.username);
    setAuthState("signed-in");
    const [deviceList, jobList] = await Promise.all([
      remoteApi<Device[]>("/api/control/devices"),
      remoteApi<ControlJob[]>("/api/control/jobs"),
    ]);
    setDevices(deviceList);
    setJobs(jobList);
    setSelectedDevice((selected) => selected || deviceList.find((device) => !device.revoked_at)?.id || "");
  }, []);

  useEffect(() => {
    let active = true;
    void loadControlPlane().catch((reason: unknown) => {
      if (active && !(reason instanceof ApiError && reason.status === 401)) {
        setError(reason instanceof Error ? reason.message : "Could not reach the control plane.");
      }
    });
    return () => {
      active = false;
    };
  }, [loadControlPlane]);

  async function signIn() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await remoteApi<{ csrf_token: string }>("/api/auth/csrf");
      await remoteApi<{ username: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      await loadControlPlane();
      setMessage("Signed in to the control plane.");
    } catch (reason) {
      setSignedInAs(null);
      setAuthState("signed-out");
      setError(reason instanceof Error ? reason.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await remoteApi<{ status: string }>("/api/auth/logout", { method: "POST" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not sign out.");
    } finally {
      setSignedInAs(null);
      setDevices([]);
      setJobs([]);
      setEnrollmentCode(null);
      setSelectedDevice("");
      setAuthState("signed-out");
      setBusy(false);
    }
  }

  async function issueEnrollmentCode() {
    setBusy(true);
    setError("");
    try {
      const result = await remoteApi<{ code: string }>("/api/control/enrollment-codes", { method: "POST" });
      setEnrollmentCode(result.code);
      setMessage("This single-use code is shown only once and expires in 10 minutes.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create an enrollment code.");
    } finally {
      setBusy(false);
    }
  }

  async function createJob(operation: string) {
    if (!selectedDevice) return;
    setBusy(true);
    setError("");
    try {
      await remoteApi("/api/control/jobs", {
        method: "POST",
        body: JSON.stringify({ device_id: selectedDevice, operation }),
      });
      await loadControlPlane();
      setMessage("Read-only request queued. It expires if the Windows agent does not poll in time.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not queue the request.");
    } finally {
      setBusy(false);
    }
  }

  async function revokeDevice(deviceId: string) {
    setBusy(true);
    setError("");
    try {
      await remoteApi(`/api/control/devices/${deviceId}/revoke`, { method: "POST" });
      await loadControlPlane();
      setMessage("Device credential revoked.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not revoke the device.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <div className="eyebrow">SECURE FOUNDATION</div>
        <h1>Remote Access</h1>
        <p className="subheading">An authenticated control plane for explicitly enrolled Windows agents.</p>
      </div>
      <div className="notice">
        Remote actions are currently limited to agent health and basic runtime status. This agent does not read files,
        run shell commands, or modify your computer. Use HTTPS for any non-local connection. Chat and document APIs
        remain local-only; enable control-plane-only mode before exposing a deployment.
      </div>
      {error && <div className="notice" role="alert">{error}</div>}
      {message && <div className="notice success" role="status">{message}</div>}
      {authState === "loading" ? (
        <div className="empty-state" role="status">Loading the authenticated control plane…</div>
      ) : authState === "signed-out" ? (
        <section className="panel remote-panel">
          <div className="panel-title"><h2>Owner sign-in</h2></div>
          <p className="quiet-label">
            Remote Access uses the single owner account created in the backend folder with
            {" "}<code>python -m app.bootstrap_owner</code>.
          </p>
          <form
            className="remote-toolbar"
            onSubmit={(event) => {
              event.preventDefault();
              void signIn();
            }}
          >
            <input
              className="remote-input"
              name="username"
              autoComplete="username"
              placeholder="Owner username"
              aria-label="Owner username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
            <input
              className="remote-input"
              name="password"
              type="password"
              autoComplete="current-password"
              placeholder="Owner password"
              aria-label="Owner password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <button className="button" type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </section>
      ) : (
        <>
          <section className="panel remote-panel">
            <div className="panel-title"><h2>Signed in as {signedInAs}</h2></div>
            <div className="remote-toolbar">
              <button className="button" disabled={busy} onClick={issueEnrollmentCode}>Create enrollment code</button>
              <button className="button button-secondary" disabled={busy} onClick={() => void loadControlPlane()}>Refresh</button>
              <button className="button button-secondary" disabled={busy} onClick={() => void signOut()}>Sign out</button>
            </div>
            {enrollmentCode && <div className="enrollment-code"><strong>Copy this code to the Windows agent prompt now:</strong><code>{enrollmentCode}</code></div>}
          </section>
          <section className="panel remote-panel">
            <div className="panel-title"><h2>Enrolled devices</h2><span className="quiet-label">{devices.filter((device) => !device.revoked_at).length} active</span></div>
            {devices.length === 0 ? <div className="empty-state">No devices enrolled yet.</div> : devices.map((device) => (
              <div className="list-row" key={device.id}>
                <div className="list-copy">
                  <strong>{device.name} {device.revoked_at ? "(revoked)" : ""}</strong>
                  <small>Last agent contact: {device.last_seen_at ? new Date(device.last_seen_at).toLocaleString() : "not yet seen"}</small>
                </div>
                {!device.revoked_at && <button className="button button-danger" disabled={busy} onClick={() => void revokeDevice(device.id)}>Revoke</button>}
              </div>
            ))}
          </section>
          <section className="panel remote-panel">
            <div className="panel-title"><h2>Read-only agent requests</h2></div>
            <div className="remote-toolbar">
              <select className="remote-input remote-select" value={selectedDevice} onChange={(event) => setSelectedDevice(event.target.value)}>
                <option value="">Select an active device</option>
                {devices.filter((device) => !device.revoked_at).map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}
              </select>
              <button className="button" disabled={busy || !selectedDevice} onClick={() => void createJob("health.check")}>Queue health check</button>
              <button className="button button-secondary" disabled={busy || !selectedDevice} onClick={() => void createJob("agent.status")}>Queue agent status</button>
            </div>
            {jobs.length === 0 ? <div className="empty-state">No remote requests yet.</div> : jobs.map((job) => (
              <div className="list-row" key={job.id}>
                <div className="list-copy"><strong>{job.operation} · {job.status}</strong><small>{job.result ? JSON.stringify(job.result) : new Date(job.created_at).toLocaleString()}</small></div>
              </div>
            ))}
          </section>
        </>
      )}
    </>
  );
}
