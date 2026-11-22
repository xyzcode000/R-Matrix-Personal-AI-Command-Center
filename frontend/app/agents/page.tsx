"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../../components/api";

type Agent = { id: string; name: string; description: string; permissions: string[] };
type Tool = {
  name: string;
  purpose: string;
  required_permission: string;
  risk: string;
  execution: string;
};
type Memory = {
  id: string;
  content: string;
  provenance: string;
  created_at: string;
  updated_at: string;
  category: string | null;
};
type MemoryProposal = Memory & { status: string };
type Approval = {
  id: string;
  operation: string;
  target: string;
  summary: string;
  status: string;
  created_at: string;
};
type Execution = {
  id: string;
  agent_id: string;
  reason: string;
  status: string;
  tools: string[];
  created_at: string;
};

export default function AgentsPage() {
  const [agentList, setAgentList] = useState<Agent[]>([]);
  const [toolList, setToolList] = useState<Tool[]>([]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [proposals, setProposals] = useState<MemoryProposal[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [memoryDraft, setMemoryDraft] = useState("");
  const [memoryQuery, setMemoryQuery] = useState("");
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingMemoryContent, setEditingMemoryContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const memoryPath = memoryQuery.trim()
      ? `/api/memories?query=${encodeURIComponent(memoryQuery.trim())}`
      : "/api/memories";
    const [catalog, approved, pending, actionProposals, recent] = await Promise.all([
      api<{ agents: Agent[]; tools: Tool[]; authorized_project_ids: string[] }>("/api/agents"),
      api<Memory[]>(memoryPath),
      api<MemoryProposal[]>("/api/memory-proposals"),
      api<Approval[]>("/api/action-approvals"),
      api<Execution[]>("/api/agent-executions?limit=20"),
    ]);
    setAgentList(catalog.agents);
    setToolList(catalog.tools);
    setProjectIds(catalog.authorized_project_ids);
    setMemories(approved);
    setProposals(pending);
    setApprovals(actionProposals);
    setExecutions(recent);
  }, [memoryQuery]);

  useEffect(() => {
    void refresh().catch((reason: Error) => setError(reason.message));
  }, [refresh]);

  async function reviewProposal(id: string, approve: boolean) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/memory-proposals/${id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve }),
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not review memory proposal.");
    } finally {
      setBusy(false);
    }
  }

  async function deleteMemory(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/memories/${id}`, { method: "DELETE" });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not remove memory.");
    } finally {
      setBusy(false);
    }
  }

  async function createMemoryProposal() {
    setBusy(true);
    setError("");
    try {
      await api("/api/memory-proposals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: memoryDraft }),
      });
      setMemoryDraft("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not propose this memory.");
    } finally {
      setBusy(false);
    }
  }

  async function saveMemory(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/memories/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editingMemoryContent }),
      });
      setEditingMemoryId(null);
      setEditingMemoryContent("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update memory.");
    } finally {
      setBusy(false);
    }
  }

  async function clearMemories() {
    if (!window.confirm("Delete all approved memories? This cannot be undone.")) return;
    setBusy(true);
    setError("");
    try {
      await api<{ deleted: number }>("/api/memories", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not clear memories.");
    } finally {
      setBusy(false);
    }
  }

  async function searchMemories(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const path = memoryQuery.trim()
        ? `/api/memories?query=${encodeURIComponent(memoryQuery.trim())}`
        : "/api/memories";
      setMemories(await api<Memory[]>(path));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not search memories.");
    } finally {
      setBusy(false);
    }
  }

  async function cancelApproval(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/action-approvals/${id}/cancel`, { method: "POST" });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel proposal.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-heading"><div className="eyebrow">ORCHESTRATION</div><h1>Agents &amp; Memory</h1><p className="subheading">See the configured specialists, restricted tools, approved memories, and real recent execution records.</p></div>
      {error && <div className="notice" role="alert">{error}</div>}
      <div className="notice">This is a local single-user application. Consequential actions have no executor: a proposal is not an approval or execution. Documents and project files remain local unless document context is explicitly consented to for a cloud model.</div>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Agent capabilities</h2><button className="button button-secondary" disabled={busy} onClick={() => void refresh()}>Refresh</button></div>
        {agentList.map((agent) => <div className="list-row" key={agent.id}><div className="list-copy"><strong>{agent.name}</strong><small>{agent.description} · Permissions: {agent.permissions.join(", ") || "selected model only"}</small></div></div>)}
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Registered tools</h2><span className="quiet-label">Backend allowlist</span></div>
        {toolList.map((tool) => <div className="list-row" key={tool.name}><div className="list-copy"><strong>{tool.name} · {tool.risk} risk</strong><small>{tool.purpose} · Permission: {tool.required_permission} · {tool.execution}</small></div></div>)}
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Authorized project IDs</h2><span className="quiet-label">Read-only</span></div>
        <div className="empty-state">{projectIds.length ? projectIds.join(" · ") : "No project folders are configured."}</div>
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Memory proposals</h2><span className="quiet-label">Review the exact text before saving</span></div>
        <div className="list-row"><input aria-label="Proposed memory text" className="field" maxLength={500} placeholder="Write a concise preference or fact…" value={memoryDraft} onChange={(event) => setMemoryDraft(event.target.value)} /><button className="button" disabled={busy || memoryDraft.trim().length < 5} onClick={() => void createMemoryProposal()}>Propose memory</button></div>
        {proposals.length === 0 ? <div className="empty-state">No pending memory proposals.</div> : proposals.map((proposal) => <div className="list-row" key={proposal.id}><div className="list-copy"><strong>{proposal.content}</strong><small>{proposal.provenance} · {new Date(proposal.created_at).toLocaleString()}</small></div><button className="button" disabled={busy} onClick={() => void reviewProposal(proposal.id, true)}>Approve</button><button className="button button-danger" disabled={busy} onClick={() => void reviewProposal(proposal.id, false)}>Reject</button></div>)}
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Approved memories</h2><button className="button button-danger" disabled={busy} onClick={() => void clearMemories()}>Clear all</button></div>
        <form className="document-toolbar" onSubmit={(event) => void searchMemories(event)}><input aria-label="Search approved memories" className="field search-field" maxLength={200} placeholder="Search approved memories…" value={memoryQuery} onChange={(event) => setMemoryQuery(event.target.value)} /><button className="button button-secondary" disabled={busy}>Search</button></form>
        {memories.length === 0 ? <div className="empty-state">{memoryQuery.trim() ? "No matching approved memories." : "No approved memories saved."}</div> : memories.map((memory) => <div className="list-row" key={memory.id}>{editingMemoryId === memory.id ? <><div className="list-copy"><input aria-label="Edit memory text" className="field" maxLength={500} value={editingMemoryContent} onChange={(event) => setEditingMemoryContent(event.target.value)} /><small>The text above will replace this approved memory when saved.</small></div><button className="button" disabled={busy || editingMemoryContent.trim().length < 5} onClick={() => void saveMemory(memory.id)}>Save</button><button className="button button-secondary" disabled={busy} onClick={() => setEditingMemoryId(null)}>Cancel</button></> : <><div className="list-copy"><strong>{memory.content}</strong><small>{memory.provenance} · Created {new Date(memory.created_at).toLocaleString()} · Updated {new Date(memory.updated_at || memory.created_at).toLocaleString()}</small></div><button className="button button-secondary" disabled={busy} onClick={() => { setEditingMemoryId(memory.id); setEditingMemoryContent(memory.content); }}>Edit</button><button className="button button-danger" disabled={busy} onClick={() => void deleteMemory(memory.id)}>Remove</button></>}</div>)}
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Pending action proposals</h2></div>
        {approvals.length === 0 ? <div className="empty-state">No pending proposals.</div> : approvals.map((approval) => <div className="list-row" key={approval.id}><div className="list-copy"><strong>{approval.operation}</strong><small>{approval.summary} · {approval.target} · Not executable in this version.</small></div><button className="button button-secondary" disabled={busy} onClick={() => void cancelApproval(approval.id)}>Dismiss</button></div>)}
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Recent real executions</h2></div>
        {executions.length === 0 ? <div className="empty-state">No agent executions recorded.</div> : executions.map((execution) => <div className="list-row" key={execution.id}><div className="list-copy"><strong>{execution.agent_id.replaceAll("_", " ")} · {execution.status}</strong><small>{execution.reason} · Tools: {execution.tools.join(", ") || "none"} · {new Date(execution.created_at).toLocaleString()}</small></div></div>)}
      </section>
    </>
  );
}
