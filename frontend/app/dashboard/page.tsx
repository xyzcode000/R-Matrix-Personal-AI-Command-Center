"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type ApplicationTask, type Conversation, type DocumentItem } from "@/components/api";

type ModelStatus = { provider: string; configured: boolean; model: string | null; error: string | null };
type OllamaStatus = { available: boolean; model: string; model_available: boolean; error: string | null };
type Execution = { id: string; agent_id: string; status: string; reason: string; created_at: string };

const prompts = [
  "Help me find the key ideas in my indexed documents.",
  "Summarize what I have been working on recently.",
  "Help me break a project goal into practical steps.",
  "What can you answer from my local knowledge?",
];

export default function WorkspaceDashboardPage() {
  const [backend, setBackend] = useState(false);
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null);
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [tasks, setTasks] = useState<ApplicationTask[]>([]);
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [error, setError] = useState("");
  const [today, setToday] = useState("");

  useEffect(() => {
    const checkConnections = () => {
      void api<{ status: string }>("/api/health").then(() => setBackend(true)).catch(() => setBackend(false));
      void api<ModelStatus>("/api/health/model").then((result) => {
        setModelStatus(result);
        if (result.provider === "ollama") {
          void api<OllamaStatus>("/api/health/ollama").then(setOllamaStatus).catch(() => setOllamaStatus(null));
        } else {
          setOllamaStatus(null);
        }
      }).catch(() => setModelStatus(null));
      void api<DocumentItem[]>("/api/documents").then(setDocuments).catch((reason: Error) => setError(reason.message));
      void api<Conversation[]>("/api/conversations").then(setConversations).catch((reason: Error) => setError(reason.message));
      void api<ApplicationTask[]>("/api/tasks").then(setTasks).catch((reason: Error) => setError(reason.message));
      void api<Execution[]>("/api/agent-executions?limit=10").then(setExecutions).catch((reason: Error) => setError(reason.message));
    };
    checkConnections();
    const interval = window.setInterval(checkConnections, 15000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    const updateToday = () => setToday(new Intl.DateTimeFormat("en-IN", {
      weekday: "long",
      day: "numeric",
      month: "long",
      timeZone: "Asia/Kolkata",
    }).format(new Date()));
    updateToday();
    const interval = window.setInterval(updateToday, 60_000);
    return () => window.clearInterval(interval);
  }, []);

  return (
    <>
      <section className="hero hero--command">
        <div className="hero-copy">
          <div className="hero-tag"><span aria-hidden="true">R</span> PRIVATE COMMAND DECK{today ? ` · ${today}` : ""}</div>
          <h1>Intelligence,<br /><em>in your corner.</em></h1>
          <p className="subheading">A private command center for conversations, personal knowledge, projects, and the work ahead. Your documents stay on this device; excerpts reach a cloud model only when you explicitly confirm.</p>
          <div className="hero-actions">
            <Link className="button" href="/chat">Start a conversation <span aria-hidden="true">↗</span></Link>
            <Link className="button button-secondary" href="/documents">Explore documents</Link>
          </div>
        </div>
        <div className="hero-index" aria-hidden="true"><span>01 — PRIVATE BY DEFAULT</span><span>LOCAL-FIRST · ALWAYS IN FOCUS</span></div>
      </section>
      {error && <div className="notice" role="alert">{error}</div>}
      <section className="metric-grid" aria-label="Workspace status">
        <div className="metric-card"><div className="metric-label">Backend</div><div className="metric-status"><span className={`status-dot ${backend ? "online" : "offline"}`} /><div className="metric-value">{backend ? "Connected" : "Unavailable"}</div></div><div className="metric-sub">Local FastAPI service</div></div>
        <div className="metric-card"><div className="metric-label">{modelStatus?.provider ?? "Model provider"} · {modelStatus?.model ?? "checking configuration"}</div><div className="metric-status"><span className={`status-dot ${(modelStatus?.provider === "ollama" ? ollamaStatus?.available && ollamaStatus.model_available : modelStatus?.configured) ? "online" : "offline"}`} /><div className="metric-value">{modelStatus?.provider === "ollama" ? ollamaStatus?.available ? ollamaStatus.model_available ? "Ready" : "Model missing" : "Unavailable" : modelStatus?.configured ? "Configured · not tested" : "Needs configuration"}</div></div><div className="metric-sub">{modelStatus?.provider === "ollama" ? ollamaStatus?.error ?? "Local model status" : modelStatus?.error ?? "Credentials are configured; provider connection has not been tested."}</div></div>
        <div className="metric-card"><div className="metric-label">Indexed documents</div><div className="metric-value">{documents.length}</div><div className="metric-sub">Stored in your local app data directory</div></div>
      </section>
      <div className="section-head"><h2>Your workspace</h2><span className="quiet-label">{conversations.length} saved conversations</span></div>
      <section className="content-grid">
        <div className="panel">
          <div className="panel-title"><h2>Recent conversations</h2><Link href="/chat" className="text-link">Open chat →</Link></div>
          {conversations.length === 0 ? <div className="empty-state">Your recent conversations will appear here.<br /><Link className="text-link" href="/chat">Start a conversation →</Link></div> :
            conversations.slice(0, 4).map((item) => <Link className="list-row" href={`/chat?conversation=${encodeURIComponent(item.id)}`} key={item.id}>
              <span className="list-icon">✳</span><span className="list-copy"><strong>{item.title}</strong><small>{item.last_message?.slice(0, 80) ?? "No messages"} · {new Date(item.updated_at).toLocaleDateString()}</small></span><span className="quiet-label">↗</span>
            </Link>)}
        </div>
        <div className="panel">
          <div className="panel-title"><h2>Recent task activity</h2><Link href="/task-history" className="text-link">View history →</Link></div>
          {tasks.filter((task) => task.status !== "done").length === 0 ? <div className="empty-state">No active tasks.<br />Create a task to track a deadline.</div> : tasks.filter((task) => task.status !== "done").slice(0, 4).map((task) => {
            const overdue = task.due_at && new Date(task.due_at).getTime() < Date.now();
            return <Link className="list-row" href="/task-history" key={task.id}>
              <span className="list-icon">◷</span><span className="list-copy"><strong>{task.title}</strong><small>{overdue ? "Overdue" : task.due_at ? `Due ${new Date(task.due_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}` : "No deadline"} · {task.status.replace("_", " ")}</small></span>
            </Link>;
          })}
        </div>
      </section>
      <div className="section-head"><h2>Recent activity</h2></div>
      <section className="content-grid">
        <div className="panel">
          <div className="panel-title"><h2>Recent documents</h2><Link href="/documents" className="text-link">Open library →</Link></div>
          {documents.length === 0 ? <div className="empty-state">No uploaded documents yet.</div> : documents.slice(0, 4).map((document) => <Link className="list-row" href="/documents" key={document.id}>
            <span className="list-icon">▤</span><span className="list-copy"><strong>{document.original_name}</strong><small>{document.status === "failed" ? `Failed · ${document.processing_error ?? "retry extraction"}` : "Ready"} · {new Date(document.created_at).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" })}</small></span>
          </Link>)}
        </div>
        <div className="panel">
          <div className="panel-title"><h2>Recent assistant activity</h2></div>
          {executions.length === 0 ? <div className="empty-state">No agent executions recorded yet.</div> : executions.slice(0, 4).map((execution) => <div className="list-row" key={execution.id}>
            <span className="list-icon">✧</span><span className="list-copy"><strong>{execution.agent_id} · {execution.status}</strong><small>{execution.reason} · {new Date(execution.created_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}</small></span>
          </div>)}
        </div>
      </section>
      <div className="section-head"><h2>Try asking</h2><span className="quiet-label">Your local assistant</span></div>
      <section className="prompt-grid">{prompts.map((prompt) => <Link className="prompt-card" href={`/chat?prompt=${encodeURIComponent(prompt)}`} key={prompt}>{prompt} <span aria-hidden="true">↗</span></Link>)}</section>
      <section className="about-section" id="about" aria-labelledby="about-title">
        <div className="about-copy">
          <div className="eyebrow">ABOUT R MATRIX AI</div>
          <h2 id="about-title">A calmer command center for your digital life.</h2>
          <p>One considered workspace for thinking, finding, and moving work forward—without losing sight of where your information lives.</p>
        </div>
        <div className="about-principles">
          <article className="about-principle">
            <span>01 / LOCAL FIRST</span>
            <strong>Your workspace stays yours.</strong>
            <p>Documents and indexed knowledge are stored by the local application on this device.</p>
          </article>
          <article className="about-principle">
            <span>02 / EXPLICIT SHARING</span>
            <strong>You choose what travels.</strong>
            <p>Document excerpts are shared with a cloud model only after your per-request confirmation.</p>
          </article>
        </div>
      </section>
    </>
  );
}
