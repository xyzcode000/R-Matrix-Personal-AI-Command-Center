"use client";

import { useEffect, useState } from "react";
import type { ProviderInventory, ProviderInfo } from "@/components/api";
import { API_BASE, api } from "@/components/api";

type OllamaStatus = { available: boolean; model_available: boolean; error: string | null };
type OwnerProfile = {
  full_name: string;
  preferred_name: string;
  user_role: string;
  personal_context: string;
  project_context: string;
  current_priorities: string;
  updated_at?: string;
};
type ReminderSettings = {
  recipient_email: string;
  smtp_configured: boolean;
  smtp_host: string | null;
  counts: Record<string, number>;
  local_scheduler_notice: string;
};
type TelegramSettings = {
  enabled: boolean;
  configured: boolean;
  bot_username: string | null;
  paired: boolean;
  pairing: {
    telegram_username: string | null;
    chat_id_suffix: string | null;
    paired_at: string | null;
    owner_account: string | null;
    active_codes: number;
    code_ttl_minutes: number;
  };
  polling: {
    last_update_id: number;
    last_poll_at: string | null;
    worker_active: boolean;
    last_error: string | null;
    last_error_at: string | null;
  };
  activity: { messages_received: number; messages_sent: number };
  limits: string;
};
type TelegramPairingCode = {
  code: string;
  expires_at: string;
  ttl_minutes: number;
  bot_username: string | null;
  instructions: string;
};

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "unknown" : parsed.toLocaleString();
}

export default function SettingsPage() {
  const [inventory, setInventory] = useState<ProviderInventory | null>(null);
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [profile, setProfile] = useState<OwnerProfile | null>(null);
  const [recipient, setRecipient] = useState("");
  const [reminderSettings, setReminderSettings] = useState<ReminderSettings | null>(null);
  const [telegramSettings, setTelegramSettings] = useState<TelegramSettings | null>(null);
  const [pairingCode, setPairingCode] = useState<TelegramPairingCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    void api<ProviderInventory>("/api/providers").then((result) => {
      setInventory(result);
      if (result.selected_provider === "ollama") {
        void api<OllamaStatus>("/api/health/ollama").then(setOllamaStatus).catch((reason: Error) => setError(reason.message));
      }
    }).catch((reason: Error) => setError(reason.message));
    void api<OwnerProfile>("/api/settings/profile").then(setProfile).catch((reason: Error) => setError(reason.message));
    void api<ReminderSettings>("/api/settings/reminders").then((result) => {
      setReminderSettings(result);
      setRecipient(result.recipient_email);
    }).catch((reason: Error) => setError(reason.message));
    void api<TelegramSettings>("/api/settings/telegram").then(setTelegramSettings).catch((reason: Error) => setError(reason.message));
  }, []);
  const selected = inventory?.providers.find((item) => item.id === inventory.selected_provider);

  async function saveProfile(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!profile || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const profileFields = { ...profile };
      delete profileFields.updated_at;
      setProfile(await api<OwnerProfile>("/api/settings/profile", { method: "PUT", body: JSON.stringify(profileFields) }));
      setNotice("Owner profile saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save the owner profile.");
    } finally { setBusy(false); }
  }

  async function saveRecipient(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<ReminderSettings>("/api/settings/reminders", { method: "PUT", body: JSON.stringify({ recipient_email: recipient }) });
      setReminderSettings(result);
      setNotice("Reminder recipient saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save the recipient.");
    } finally { setBusy(false); }
  }

  async function sendTestEmail() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<{ message: string }>("/api/settings/reminders/test", { method: "POST", body: "{}" });
      setNotice(result.message);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The test email could not be sent.");
    } finally { setBusy(false); }
  }

  async function createTelegramPairingCode() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<TelegramPairingCode>("/api/settings/telegram/pairing-code", { method: "POST" });
      setPairingCode(result);
      setTelegramSettings(await api<TelegramSettings>("/api/settings/telegram"));
      setNotice("Pairing code created. Send it to the bot from your Telegram account.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create a pairing code.");
    } finally { setBusy(false); }
  }

  async function verifyTelegramConnection() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<TelegramSettings>("/api/settings/telegram/verify", { method: "POST" });
      setTelegramSettings(result);
      setNotice(result.bot_username ? `Telegram bot @${result.bot_username} answered the connection check.` : "Telegram answered the connection check.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Telegram could not be reached.");
    } finally { setBusy(false); }
  }

  async function disconnectTelegram() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      setTelegramSettings(await api<TelegramSettings>("/api/settings/telegram/disconnect", { method: "POST" }));
      setPairingCode(null);
      setNotice("Telegram disconnected from this owner account.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not disconnect Telegram.");
    } finally { setBusy(false); }
  }

  async function exportData() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = await api<Record<string, unknown>>("/api/export");
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `r-matrix-export-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      setNotice("Personal data export downloaded.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The export could not be generated.");
    } finally { setBusy(false); }
  }

  return (
    <>
      <div className="page-heading"><div className="eyebrow">PREFERENCES & CONNECTIONS</div><h1>Settings</h1><p className="subheading">This MVP uses local services and stores application data on this device.</p></div>
      {error && <div className="notice" role="alert">{error}</div>}
      {notice && <div className="notice success" role="status">{notice}</div>}
      {profile && <section className="panel remote-panel">
        <div className="panel-title"><h2>Owner profile</h2><span className="quiet-label">Used by local chat for identity and personalization</span></div>
        <form className="remote-form" onSubmit={(event) => void saveProfile(event)}>
          <label>Full name<input className="remote-input" value={profile.full_name} maxLength={120} onChange={(event) => setProfile({ ...profile, full_name: event.target.value })} required /></label>
          <label>Preferred name<input className="remote-input" value={profile.preferred_name} maxLength={80} onChange={(event) => setProfile({ ...profile, preferred_name: event.target.value })} required /></label>
          <label>Role<input className="remote-input" value={profile.user_role} maxLength={120} onChange={(event) => setProfile({ ...profile, user_role: event.target.value })} required /></label>
          <label>Personal context<textarea className="remote-input" value={profile.personal_context} maxLength={2000} rows={3} onChange={(event) => setProfile({ ...profile, personal_context: event.target.value })} /></label>
          <label>Project context<textarea className="remote-input" value={profile.project_context} maxLength={2000} rows={3} onChange={(event) => setProfile({ ...profile, project_context: event.target.value })} /></label>
          <label>Current priorities<textarea className="remote-input" value={profile.current_priorities} maxLength={2000} rows={3} onChange={(event) => setProfile({ ...profile, current_priorities: event.target.value })} /></label>
          <button className="button" disabled={busy}>{busy ? "Saving…" : "Save profile"}</button>
        </form>
        <p className="quiet-label">The initial Rahil profile is created only when no profile exists. Profile context is not sent to cloud providers by default.</p>
      </section>}
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Email task reminders</h2><span className={reminderSettings?.smtp_configured ? "configured-label" : "unconfigured-label"}>{reminderSettings?.smtp_configured ? `SMTP configured${reminderSettings.smtp_host ? ` · ${reminderSettings.smtp_host}` : ""}` : "SMTP not configured"}</span></div>
        <form className="remote-form" onSubmit={(event) => void saveRecipient(event)}>
          <label>Recipient email<input className="remote-input" type="email" maxLength={254} value={recipient} onChange={(event) => setRecipient(event.target.value)} placeholder="you@example.com" /></label>
          <button className="button button-secondary" disabled={busy}>{busy ? "Saving…" : "Save recipient"}</button>
        </form>
        <button className="button" disabled={busy || !recipient || !reminderSettings?.smtp_configured} onClick={() => void sendTestEmail()}>Send test email</button>
        <p className="quiet-label">Delivery status · Pending {reminderSettings?.counts.pending ?? 0} · Sent {reminderSettings?.counts.sent ?? 0} · Failed {reminderSettings?.counts.failed ?? 0} · Cancelled {reminderSettings?.counts.cancelled ?? 0}</p>
        <div className="notice">{reminderSettings?.local_scheduler_notice ?? "The backend and machine must be running to send reminders on time."}</div>
        <details><summary>SMTP setup (Gmail example)</summary><p>Set SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USE_TLS=true, SMTP_FROM to the sender address, and SMTP_USERNAME / SMTP_PASSWORD in backend/.env. For Gmail, use a Google App Password (not your account password) with 2-Step Verification enabled. Restart the backend after changing environment variables. SMTP credentials remain server-side and are never stored in SQLite.</p></details>
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Telegram bot</h2><span className={telegramSettings?.paired ? "configured-label" : "unconfigured-label"}>{!telegramSettings ? "Checking…" : !telegramSettings.configured ? "Bot token not configured" : telegramSettings.paired ? `Paired${telegramSettings.bot_username ? ` · @${telegramSettings.bot_username}` : ""}` : "Not paired"}</span></div>
        <div className="setting-row"><div><strong>Long polling</strong><p>{telegramSettings?.polling.worker_active ? "The local polling worker is running and receiving updates." : "No polling worker is active right now. Start the backend to receive Telegram messages."} Last poll: {formatTimestamp(telegramSettings?.polling.last_poll_at)}. Offset: {telegramSettings?.polling.last_update_id ?? 0}.{telegramSettings?.polling.last_error ? ` Last error: ${telegramSettings.polling.last_error}` : ""}</p></div><span className="setting-value">{telegramSettings ? `${telegramSettings.activity.messages_received} in · ${telegramSettings.activity.messages_sent} out` : "—"}</span></div>
        <div className="setting-row"><div><strong>Paired account</strong><p>{telegramSettings?.paired ? `Telegram @${telegramSettings.pairing.telegram_username ?? "user"} · chat ending ${telegramSettings.pairing.chat_id_suffix ?? "—"} · paired ${formatTimestamp(telegramSettings.pairing.paired_at)}` : "No Telegram chat is linked. Create a pairing code, then send /start CODE to your bot from a private chat."}</p></div><span className="setting-value">{telegramSettings?.pairing.owner_account ?? "Not paired"}</span></div>
        <form className="remote-form" onSubmit={(event) => { event.preventDefault(); void createTelegramPairingCode(); }}>
          <button className="button" disabled={busy || !telegramSettings?.configured}>{busy ? "Working…" : "Create pairing code"}</button>
        </form>
        {pairingCode && <div className="notice success" role="status">Send <strong>/start {pairingCode.code}</strong> to {pairingCode.bot_username ? `@${pairingCode.bot_username}` : "your bot"} from your own private Telegram chat. The code is single use and expires in {pairingCode.ttl_minutes} minutes.</div>}
        <div className="remote-form">
          <button className="button button-secondary" disabled={busy || !telegramSettings?.configured} onClick={() => void verifyTelegramConnection()}>Test Telegram connection</button>
          <button className="button button-secondary" disabled={busy || !telegramSettings?.paired} onClick={() => void disconnectTelegram()}>Disconnect Telegram</button>
        </div>
        <p className="quiet-label">{telegramSettings?.limits ?? "A bot can only read messages sent or forwarded to it; it cannot read your Telegram account history."} Commands: /today, /tasks, /deadlines, /projects, /summary, /help. The bot token stays in backend/.env and is never sent to this page.</p>
      </section>
      <section className="panel settings-grid">
        <div className="panel-title"><h2>Model providers</h2></div>
        <div className="setting-row"><div><strong>Backend API</strong><p>Local FastAPI service address used by the dashboard.</p></div><span className="setting-value">{API_BASE}</span></div>
        <div className="setting-row"><div><strong>Selected provider</strong><p>{!selected ? `The configured provider "${inventory?.selected_provider}" is unsupported. Select a supported provider in AI Chat.` : selected.cloud ? "Cloud provider: your message and eligible conversation history are sent to this provider. Document excerpts require per-request consent." : "Local provider: chat requests remain on this device."}</p></div><span className="setting-value">{selected?.name ?? inventory?.selected_provider ?? "Checking…"}</span></div>
        <div className="setting-row"><div><strong>Configured model</strong><p>{!selected ? "No model is available for the unsupported provider." : selected.id === "ollama" ? ollamaStatus?.available ? ollamaStatus.model_available ? "Ollama is reachable and the model is installed." : ollamaStatus.error : ollamaStatus?.error ?? "Checking local Ollama status." : selected.configured ? "Credentials and model are configured; no live provider request has been made." : selected.configuration_error ?? "Checking provider configuration."}</p></div><span className="setting-value">{selected?.model ?? "Not configured"}</span></div>
        <div className="provider-status-list">{inventory?.providers.map((provider: ProviderInfo) => <div className="provider-status-row" key={provider.id}><span>{provider.name}{provider.cloud ? " · Cloud" : " · Local"}</span><span className={provider.configured ? "configured-label" : "unconfigured-label"}>{provider.configured ? provider.id === "ollama" ? "Available to check" : "Configured · not connection-tested" : provider.configuration_error}</span></div>)}</div>
        <div className="setting-row"><div><strong>Data location</strong><p>SQLite database and uploaded files are stored under the local application data directory. Configure R_MATRIX_DATA_DIR before starting the backend to change it.</p></div><span className="setting-value">Local device</span></div>
        <div className="setting-row"><div><strong>API key storage</strong><p>Cloud provider keys are read only by the backend from backend/.env. They are never returned by status endpoints or sent to the browser.</p></div><span className="setting-value">Server-side only</span></div>
        <div className="setting-row"><div><strong>Cloud document sharing</strong><p>Off by default. On AI Chat, matching excerpts are sent only when you explicitly enable the consent checkbox for that request.</p></div><span className="setting-value">Explicit opt-in</span></div>
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Data &amp; privacy</h2><span className="quiet-label">Owner-controlled</span></div>
        <div className="setting-row"><div><strong>Export my data</strong><p>Downloads a JSON file with your profile, conversations, projects, tasks, memories and document metadata. Password hashes, session tokens and API keys are excluded.</p></div><button className="button" disabled={busy} onClick={() => void exportData()}>{busy ? "Preparing…" : "Download export"}</button></div>
        {inventory && <details><summary>Where each model provider processes your data</summary>
          {inventory.providers.map((provider: ProviderInfo) => <div className="setting-row" key={provider.id}><div><strong>{provider.name} · {provider.privacy?.processing === "local" ? "On this device" : "External service"}</strong><p>{provider.privacy?.data_sent ?? "Provider behaviour is not documented for this integration."}</p></div><span className="setting-value">{provider.privacy?.leaves_device === false ? "Stays local" : "Leaves device"}</span></div>)}
        </details>}
        <details><summary>Backup and restore</summary>
          <p>Run these commands in the <code>backend</code> folder. Backups never overwrite an existing folder and exclude <code>.env</code> secrets.</p>
          <pre>{`.\\.venv\\Scripts\\python.exe -m app.backup create
.\\.venv\\Scripts\\python.exe -m app.backup verify --backup <backup-folder>
.\\.venv\\Scripts\\python.exe -m app.backup restore --backup <backup-folder> --data-dir <empty-folder>`}</pre>
          <p>To restore, stop the backend, restore into an empty folder, verify the copy, then move the databases and documents folder into your data directory.</p>
        </details>
        <p className="quiet-label">Deleting a document removes its stored file, extracted pages and indexed chunks. Conversations delete their messages. Copies inside an existing backup remain until you remove that backup folder.</p>
      </section>
      <div className="planned-banner"><strong>Private workspace.</strong> Every private API requires the owner session, so keep the backend, frontend, and Ollama bound to localhost. Sign-in is rate limited after five failed attempts.</div>
    </>
  );
}
