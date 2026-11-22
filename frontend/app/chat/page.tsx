"use client";

import Link from "next/link";
import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Conversation,
  type Message,
  type MessageSource,
  type ProviderInfo,
  type ProviderInventory,
  type SearchResult,
  type WebResearchSource,
  type WebResearchStatus,
} from "@/components/api";

type WebSearchMode = "auto" | "always" | "never";
type PendingCloudSend = {
  message: string;
  provider: string;
  model: string;
  sources: SearchResult[];
  webSearch: WebSearchMode;
};

function maySearchWeb(message: string, mode: WebSearchMode): boolean {
  const explicit = /\b(?:search (?:the )?web|search online|web search|browse (?:the )?web|browse online|look up(?: online)?|find online|google)\b/i.test(message);
  const current = /\b(?:latest|newest|recent(?:ly)?|current(?:ly)?|today|right now|as of|this week|this month|this year|202[4-9])\b/i.test(message);
  const liveTopic = /\b(?:news|headlines|current events|weather|stocks?|market|election|scores?|pricing|price|cost|availability|available|president|prime minister|ceo|leader)\b/i.test(message);
  const softwareTopic = /\b(?:software|release|released|version|changelog|documentation|docs|api|sdk|benchmark|deprecation|security advisory)\b/i.test(message);
  const modelComparison = /\bmodel comparisons?\b|\b(?:compare|comparison|versus|vs\.?)\b.{0,100}\b(?:model|llm|chatgpt|gemini|claude|gpt[- ]?\d|llama|deepseek|mistral)\b/i.test(message);
  if (
    mode === "never" ||
    /\b(?:don't|do not|never|avoid|without)\b.{0,30}\b(?:web|internet|online|browse|search)\b|\bno (?:web|internet|online)(?:\s+(?:search|access|browsing))?\b|\boffline only\b/i.test(message)
  ) {
    return false;
  }
  if (mode === "always") return true;
  if (/^(?:what(?:'s| is) (?:the )?(?:current |today'?s )?(?:date|day|time)|what day is it|today'?s date)[?.!]*$/i.test(message.trim())) {
    return false;
  }
  return explicit || (current && softwareTopic) || liveTopic || modelComparison;
}

function formatResearchTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "time unavailable";
  return date.toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Kolkata",
  });
}

function conversationSummary(value: string | null): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [providerOptions, setProviderOptions] = useState<ProviderInfo[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [providerError, setProviderError] = useState("");
  const [modelsLoading, setModelsLoading] = useState(false);
  const [allowCloudDocumentContext, setAllowCloudDocumentContext] = useState(false);
  const [webSearchMode, setWebSearchMode] = useState<WebSearchMode>("auto");
  const [isSearchingWeb, setIsSearchingWeb] = useState(false);
  const [pendingCloudSend, setPendingCloudSend] = useState<PendingCloudSend | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ ids: string[]; title: string } | null>(null);
  const [deletingIds, setDeletingIds] = useState<string[]>([]);
  const [selectedConversationIds, setSelectedConversationIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const selectedProviderInfo = providerOptions.find((item) => item.id === selectedProvider);
  const selectedIds = conversations
    .filter((item) => selectedConversationIds.includes(item.id))
    .map((item) => item.id);
  const allSelected = conversations.length > 0 && selectedIds.length === conversations.length;

  const refreshConversations = useCallback(async () => {
    const items = await api<Conversation[]>("/api/conversations");
    setConversations(items);
    return items;
  }, []);

  const selectConversation = useCallback(async (id: string) => {
    setError("");
    setConversationId(id);
    try {
      setMessages(await api<Message[]>(`/api/conversations/${encodeURIComponent(id)}/messages`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load this conversation.");
    }
  }, []);

  useEffect(() => {
    void api<ProviderInventory>("/api/providers")
      .then((result) => {
        setProviderOptions(result.providers);
        const configuredDefault = result.providers.find(
          (option) => option.id === result.selected_provider && option.configured,
        );
        const fallback = result.providers.find((option) => option.configured);
        setSelectedProvider(configuredDefault?.id ?? fallback?.id ?? result.selected_provider);
      })
      .catch((reason: Error) => setProviderError(reason.message));
    void refreshConversations().then((items) => {
      const searchParams = new URLSearchParams(window.location.search);
      const requested = searchParams.get("conversation");
      const selected = requested && items.some((item) => item.id === requested) ? requested : null;
      if (selected) void selectConversation(selected);
      else if (items.length) void selectConversation(items[0].id);
      const prompt = searchParams.get("prompt");
      if (prompt && !initialized.current) setInput(prompt);
      initialized.current = true;
    }).catch((reason: Error) => setError(reason.message));
  }, [refreshConversations, selectConversation]);

  useEffect(() => {
    if (!selectedProvider) return;
    const option = providerOptions.find((item) => item.id === selectedProvider);
    if (!option?.configured) {
      setModels([]);
      setSelectedModel("");
      setProviderError(option?.configuration_error ?? "Configure this provider in backend/.env.");
      return;
    }
    let active = true;
    setModelsLoading(true);
    setProviderError("");
    void api<{ models: string[]; configured_model: string }>(
      `/api/models?provider=${encodeURIComponent(selectedProvider)}`,
    ).then((result) => {
      if (!active) return;
      setModels(result.models);
      setSelectedModel((current) =>
        result.models.includes(current)
          ? current
          : result.models.includes(option.model ?? "")
            ? option.model ?? ""
            : result.models[0] ?? "",
      );
    }).catch((reason: Error) => {
      if (active) {
        setModels([]);
        setSelectedModel("");
        setProviderError(reason.message);
      }
    }).finally(() => {
      if (active) setModelsLoading(false);
    });
    return () => { active = false; };
  }, [providerOptions, selectedProvider]);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selectedIds.length > 0 && !allSelected;
    }
  }, [allSelected, selectedIds.length]);

  async function transmitMessage(
    message: string,
    includeCloudDocumentContext: boolean,
    provider = selectedProvider,
    model = selectedModel,
    documentContextPreview: SearchResult[] = [],
    webSearch: WebSearchMode = webSearchMode,
  ) {
    const requestProvider = providerOptions.find((option) => option.id === provider)?.name ?? provider;
    setIsSearchingWeb(maySearchWeb(message, webSearch));
    setInput("");
    setError("");
    setLoading(true);
    setAllowCloudDocumentContext(false);
    setMessages((current) => [...current, {
      id: `pending-${Date.now()}`, conversation_id: conversationId ?? "", role: "user",
      content: message, sources: [], created_at: new Date().toISOString(),
    }]);
    try {
      const response = await api<{
        conversation_id: string;
        user_message: Message;
        message: Message;
        provider: string | null;
        model: string | null;
        execution?: Message["execution"];
      }>("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          conversation_id: conversationId,
          provider,
          model,
          web_search: webSearch,
          allow_cloud_document_context: includeCloudDocumentContext,
          ...(includeCloudDocumentContext ? { document_context_preview: documentContextPreview } : {}),
        }),
      });
      if (
        !response ||
        typeof response.conversation_id !== "string" ||
        typeof response.user_message?.content !== "string" ||
        !Array.isArray(response.user_message.sources) ||
        typeof response.message?.content !== "string" ||
        !response.message.content.trim() ||
        !Array.isArray(response.message.sources)
      ) {
        throw new Error("The backend returned an empty or invalid chat response. Please retry.");
      }
      setConversationId(response.conversation_id);
      setMessages((current) => [...current.filter((item) => !item.id.startsWith("pending-")), response.user_message, response.message]);
      if (response.execution?.model_used && response.provider && response.model) {
        setSelectedProvider(response.provider);
        setSelectedModel(response.model);
      }
      await refreshConversations();
    } catch (reason) {
      setMessages((current) => current.filter((item) => !item.id.startsWith("pending-")));
      setInput(message);
      const failure = reason instanceof Error ? reason.message : "Chat request failed.";
      setError(`${requestProvider} request failed: ${failure}`);
      void refreshConversations().then(async (items) => {
        if (!conversationId && items.length) {
          await selectConversation(items[0].id);
          setError(`${requestProvider} request failed: ${failure}`);
        }
      }).catch(() => undefined);
    } finally {
      setLoading(false);
      setIsSearchingWeb(false);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const message = input.trim();
    if (!message || loading || !selectedProvider || pendingCloudSend) return;
    if (selectedProviderInfo?.cloud && allowCloudDocumentContext) {
      setLoading(true);
      setError("");
      try {
        const sources = await api<SearchResult[]>("/api/documents/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
        });
        setPendingCloudSend({
          message,
          provider: selectedProvider,
          model: selectedModel,
          sources,
          webSearch: webSearchMode,
        });
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Could not preview local document excerpts.");
      } finally {
        setLoading(false);
      }
      return;
    }
    await transmitMessage(message, false);
  }

  async function confirmCloudSend() {
    if (!pendingCloudSend) return;
    const pending = pendingCloudSend;
    setPendingCloudSend(null);
    await transmitMessage(
      pending.message,
      pending.sources.length > 0,
      pending.provider,
      pending.model,
      pending.sources,
      pending.webSearch,
    );
  }

  function onInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  function newConversation() {
    setConversationId(null);
    setMessages([]);
    setError("");
    setInput("");
    setPendingCloudSend(null);
    setPendingDelete(null);
  }

  function toggleSelected(id: string) {
    setSelectedConversationIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function toggleSelectAll() {
    setSelectedConversationIds(allSelected ? [] : conversations.map((item) => item.id));
  }

  function clearSelection() {
    setSelectedConversationIds([]);
  }

  async function confirmDelete() {
    if (!pendingDelete || deletingIds.length > 0) return;
    const ids = pendingDelete.ids;
    setDeletingIds(ids);
    setError("");
    const failed: string[] = [];
    for (const id of ids) {
      try {
        await api(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
      } catch {
        failed.push(id);
      }
    }
    const deletedIds = ids.filter((id) => !failed.includes(id));
    setDeletingIds([]);
    setPendingDelete(null);
    setSelectedConversationIds((current) => current.filter((id) => !deletedIds.includes(id)));
    if (failed.length) {
      setError(
        failed.length === 1
          ? "One conversation could not be deleted. It is still in your history."
          : `${failed.length} conversations could not be deleted. They are still in your history.`,
      );
    }
    let remaining: Conversation[];
    try {
      remaining = await refreshConversations();
    } catch (reason) {
      if (!failed.length) {
        setError(reason instanceof Error ? reason.message : "Conversation history could not be refreshed.");
      }
      return;
    }
    if (conversationId && deletedIds.includes(conversationId)) {
      const next = remaining[0] ?? null;
      setConversationId(next?.id ?? null);
      if (!next) {
        setMessages([]);
        return;
      }
      try {
        setMessages(await api<Message[]>(`/api/conversations/${encodeURIComponent(next.id)}/messages`));
      } catch {
        setMessages([]);
      }
    }
  }

  return (
    <>
      <div className="page-heading"><div className="eyebrow">MODEL WORKSPACE</div><h1>AI Chat</h1><p className="subheading">The central Manager routes each request to one specialist or your selected model. The server supplies the current Asia/Kolkata date and time. Cloud providers receive your message and eligible conversation history; local document passages require explicit per-request consent, and project files are never sent to cloud models.</p></div>
      <section className="chat-layout">
        <aside className="chat-history">
          <button className="button chat-history__new" onClick={newConversation}>＋ New conversation</button>
          <div className="chat-history__head">
            <span className="chat-history__label">Chat history</span>
            <label className="chat-history__select-all">
              <input
                ref={selectAllRef}
                type="checkbox"
                checked={allSelected}
                onChange={toggleSelectAll}
                disabled={conversations.length === 0}
                aria-label={allSelected ? "Deselect all conversations" : "Select all conversations"}
              />
              <span>All</span>
            </label>
          </div>
          {selectedIds.length > 0 && (
            <div className="chat-history__bulk" role="group" aria-label="Selected conversations">
              <span className="chat-history__count">{selectedIds.length} selected</span>
              <button
                type="button"
                className="chat-history__bulk-delete"
                disabled={deletingIds.length > 0}
                onClick={() => setPendingDelete({ ids: selectedIds, title: `${selectedIds.length} selected conversations` })}
              >
                Delete
              </button>
              <button type="button" className="chat-history__bulk-clear" onClick={clearSelection} disabled={deletingIds.length > 0}>Clear</button>
            </div>
          )}
          <div className="chat-history__list">
            {conversations.map((item) => {
              const selected = selectedIds.includes(item.id);
              const deleting = deletingIds.includes(item.id);
              const summary = conversationSummary(item.last_message);
              return (
                <div key={item.id} className={`history-row ${conversationId === item.id ? "is-active" : ""} ${selected ? "is-checked" : ""}`}>
                  <input
                    type="checkbox"
                    className="history-row__check"
                    checked={selected}
                    onChange={() => toggleSelected(item.id)}
                    aria-label={`Select conversation ${item.title}`}
                  />
                  <button
                    type="button"
                    className={`history-item ${conversationId === item.id ? "selected" : ""}`}
                    onClick={() => void selectConversation(item.id)}
                    title={item.title}
                    aria-current={conversationId === item.id ? "true" : undefined}
                  >
                    <span className="history-item__title">{item.title}</span>
                    {summary && <span className="history-item__summary">{summary}</span>}
                  </button>
                  <button
                    type="button"
                    className="history-row__delete"
                    aria-label={`Delete conversation ${item.title}`}
                    title="Delete conversation"
                    disabled={deleting}
                    onClick={() => setPendingDelete({ ids: [item.id], title: item.title })}
                  >
                    {deleting ? <span className="spinner history-row__spinner" /> : (
                      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                        <path d="M4 7h16M9.5 4h5M6.5 7l.9 12.1a2 2 0 0 0 2 1.9h5.2a2 2 0 0 0 2-1.9L17.5 7M10 11v6M14 11v6" />
                      </svg>
                    )}
                  </button>
                </div>
              );
            })}
            {conversations.length === 0 && <div className="chat-history__empty">No conversations yet. Start one to see it here.</div>}
          </div>
        </aside>
        <div className="chat-main">
          <div className="chat-toolbar">
            <label className="provider-picker">Provider
              <select aria-label="AI provider" className="field provider-select" value={selectedProvider} disabled={pendingCloudSend !== null} onChange={(event) => { setSelectedProvider(event.target.value); setAllowCloudDocumentContext(false); }}>
                {providerOptions.map((option) => <option key={option.id} value={option.id} disabled={!option.configured}>{option.name}{option.cloud ? " · Cloud" : " · Local"}{!option.configured ? " · Not configured" : ""}</option>)}
              </select>
            </label>
            <label className="provider-picker">Model
              <select aria-label="AI model" className="field model-select" value={selectedModel} disabled={pendingCloudSend !== null || modelsLoading || models.length === 0} onChange={(event) => setSelectedModel(event.target.value)}>
                {models.length === 0 && <option value="">{modelsLoading ? "Loading…" : "No configured model"}</option>}
                {models.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="provider-picker">Web research
              <select aria-label="Web research mode" className="field model-select" value={webSearchMode} disabled={pendingCloudSend !== null} onChange={(event) => setWebSearchMode(event.target.value as WebSearchMode)}>
                <option value="auto">Automatic</option>
                <option value="always">Always search</option>
                <option value="never">Never search</option>
              </select>
            </label>
            <span className={`model-badge ${selectedProviderInfo?.cloud ? "cloud-badge" : ""}`}>{selectedProviderInfo ? `${selectedProviderInfo.name} · ${selectedModel || "No model selected"}` : "Loading provider…"}</span>
          </div>
          <div className="web-research-hint">Automatic mode searches only for time-sensitive public information. When it runs, Tavily receives only a sanitized query — never chat history, memories, profile details, indexed documents, or project files.</div>
          {providerOptions.some((option) => !option.configured) && <div className="provider-config-notice" role="status">{providerOptions.filter((option) => !option.configured).map((option) => <div key={option.id}>{option.name}: {option.configuration_error}</div>)}</div>}
          {selectedProviderInfo?.cloud && <div className="cloud-notice" role="note">Your message and eligible conversation history will be sent to {selectedProviderInfo.name}. Do not include sensitive information you do not want that provider to receive.</div>}
          {selectedProviderInfo?.cloud && <label className="consent-toggle"><input type="checkbox" checked={allowCloudDocumentContext} onChange={(event) => setAllowCloudDocumentContext(event.target.checked)} /> Preview matching local document excerpts and explicitly confirm before they are sent with this request.</label>}
          {providerError && <div className="notice" role="alert">{providerError}</div>}
          {error && <div className="notice" role="alert">{error}</div>}
          {pendingCloudSend && <section className="panel remote-panel" aria-live="polite">
            <div className="panel-title"><h2>Review cloud document context</h2><span className="quiet-label">{pendingCloudSend.sources.length} excerpts</span></div>
            <p className="document-meta">The excerpts shown below will be sent to {providerOptions.find((option) => option.id === pendingCloudSend.provider)?.name ?? pendingCloudSend.provider} with your question only after you confirm. Nothing has been sent yet.</p>
            {pendingCloudSend.sources.length === 0 ? <div className="empty-state">No matching document excerpts were found. Continuing will send only your question and eligible conversation history.</div> : pendingCloudSend.sources.map((source, index) => <article className="search-result" key={`${source.document_id}-${source.page_number}-${index}`}><strong>{source.filename}{source.page_number ? ` · page ${source.page_number}` : source.section_heading ? ` · ${source.section_heading}` : ""}</strong><p>{source.snippet}</p></article>)}
            <button className="button" disabled={loading} onClick={() => void confirmCloudSend()}>{pendingCloudSend.sources.length ? "Confirm and send excerpts" : "Continue without document excerpts"}</button>
            <button className="button button-secondary" disabled={loading} onClick={() => { setPendingCloudSend(null); setAllowCloudDocumentContext(false); }}>Cancel</button>
          </section>}
          {pendingDelete && <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="delete-conversation-title">
            <div className="panel confirm-dialog">
              <div className="panel-title"><h2 id="delete-conversation-title">{pendingDelete.ids.length > 1 ? `Delete ${pendingDelete.ids.length} conversations?` : "Delete conversation?"}</h2></div>
              <p>
                {pendingDelete.ids.length > 1
                  ? `${pendingDelete.ids.length} conversations and all of their messages will be permanently deleted.`
                  : `“${pendingDelete.title}” and all of its messages will be permanently deleted.`}
                {" "}Your uploaded documents and saved memories will not be deleted.
              </p>
              <div className="confirm-dialog__actions">
                <button className="button button-secondary" disabled={deletingIds.length > 0} onClick={() => setPendingDelete(null)}>Cancel</button>
                <button className="button button-danger" disabled={deletingIds.length > 0} onClick={() => void confirmDelete()}>
                  {deletingIds.length > 0 ? "Deleting…" : pendingDelete.ids.length > 1 ? `Delete ${pendingDelete.ids.length} conversations` : "Delete conversation"}
                </button>
              </div>
            </div>
          </div>}
          <div className="messages" aria-live="polite">
            {messages.length === 0 && !loading && <div className="empty-state chat-empty">Start a conversation with R Matrix Assistant.<br />Ask about your indexed documents or any other topic.</div>}
            {messages.map((message) => {
              const sourceKey = (item: MessageSource) =>
                `${("document_id" in item ? item.document_id : "") ?? ("path" in item ? item.path : "")}-${"page_number" in item ? item.page_number ?? "" : ""}`;
              const documentSources = message.sources
                .filter((source) => source.kind !== "web" && source.kind !== "web_research_status")
                .filter((source, index, all) => all.findIndex((item) => sourceKey(item) === sourceKey(source)) === index);
              const answerState = message.execution?.answer_state
                ?? (documentSources.find((source) => "answer_state" in source && source.answer_state)
                  && (documentSources.find((source) => "answer_state" in source && source.answer_state) as SearchResult).answer_state)
                ?? null;
              const stateLabel: Record<string, string> = {
                confirmed: "Verified document field",
                ambiguous: "Needs visual verification",
                unreadable: "Field could not be read",
                incomplete: "Processing incomplete",
                answered: "Document-grounded",
                user_correction: "Your correction",
              };
              const webSources = message.sources.filter(
                (source): source is WebResearchSource => source.kind === "web",
              );
              const researchStatus = message.sources.find(
                (source): source is WebResearchStatus => source.kind === "web_research_status",
              );
              return <article className={`message ${message.role}`} key={message.id}>
                <span className="message-avatar">{message.role === "assistant" ? "R" : "You"}</span>
                <div className="message-content"><strong>{message.role === "assistant" ? "R MATRIX ASSISTANT" : "YOU"}</strong>{message.content}
                  {message.role === "assistant" && message.provider && <span className="message-provider">{message.provider} · {message.model}</span>}
                  {message.role === "assistant" && answerState && stateLabel[answerState] && (documentSources.length > 0 || answerState === "user_correction") && (
                    <span className={`message-provider grounded-badge state-${answerState}`}>
                      {stateLabel[answerState]}
                      {documentSources.length > 0 && answerState !== "user_correction" ? ` · ${documentSources.length} page${documentSources.length === 1 ? "" : "s"}` : ""}
                    </span>
                  )}
                  {documentSources.length > 0 && <div className="message-sources">{documentSources.map((source, index) => {
                    const label = "filename" in source ? source.filename : "path" in source ? source.path : "Document";
                    const location = "page_number" in source && source.page_number
                      ? ` · page ${source.page_number}`
                      : "section_heading" in source && source.section_heading
                        ? ` · ${source.section_heading}`
                        : "";
                    const passage = "excerpt" in source && source.excerpt
                      ? source.excerpt
                      : "snippet" in source
                        ? source.snippet
                        : "";
                    const sourceDocumentId = "document_id" in source && source.document_id ? source.document_id : null;
                    const sourcePage = "page_number" in source && source.page_number ? source.page_number : 1;
                    const sourceState = "answer_state" in source ? source.answer_state : null;
                    const needsVerification = "requires_verification" in source && source.requires_verification;
                    return <div className={`source-card${sourceState ? ` source-card--${sourceState}` : ""}`} key={`${sourceDocumentId ?? "project"}-${sourcePage}-${index}`}>
                      <strong>Source {index + 1} · {label}{location}</strong>
                      {needsVerification && <small className="source-verification-note">Check this page visually before relying on the value.</small>}
                      {sourceDocumentId && (
                        <Link className="source-open" href={`/documents?document=${encodeURIComponent(sourceDocumentId)}&page=${sourcePage}`}>
                          Open document <span aria-hidden="true">↗</span>
                        </Link>
                      )}
                      {"excerpt" in source && source.excerpt
                        ? <details className="source-passage"><summary>View retrieved passage</summary><span>{passage}</span></details>
                        : <span>{passage}</span>}
                    </div>;
                  })}</div>}
                  {researchStatus && <div className="web-research-status" role="status">
                    <span>{researchStatus.status === "retrieved" ? "Current information retrieved" : researchStatus.status === "no_results" ? "No current sources found" : "Current information could not be verified"} · {formatResearchTime(researchStatus.retrieved_at)} IST</span>
                    {researchStatus.errors.map((issue, index) => <small key={`${issue.code}-${index}`}>{issue.message}</small>)}
                    {researchStatus.warnings.map((issue, index) => <small key={`${issue.code}-${index}`}>{issue.message}</small>)}
                    {webSources.length > 0 && <details>
                      <summary>Web sources ({webSources.length})</summary>
                      <div className="web-source-list">{webSources.map((source, index) => <article className="web-source-card" key={`${source.url}-${index}`}>
                        <a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a>
                        <small>{source.domain}{source.published_at ? ` · Published ${source.published_at}` : ""}</small>
                        <p>{source.snippet}</p>
                      </article>)}</div>
                    </details>}
                    <small>Retrieved passages are untrusted evidence; the assistant’s synthesis is interpretation.</small>
                  </div>}
                  {message.role === "assistant" && message.execution && (message.execution.task_ids.length > 0 || message.execution.approval_ids.length > 0) && <div className="agent-execution">
                    {message.execution.task_ids.map((taskId) => <small key={taskId}>Task record: {taskId}</small>)}
                    {message.execution.approval_ids.map((approvalId) => <small key={approvalId}>Pending proposal: {approvalId}</small>)}
                  </div>}
                </div>
              </article>;
            })}
            {loading && <div className="message"><span className="message-avatar">R</span><div className="typing" aria-label="Manager is handling request"><span>{isSearchingWeb ? "Searching the web for current sources" : "Manager is routing the request"}</span><i /><i /><i /></div></div>}
            <div ref={bottom} />
          </div>
          <div className="composer-wrap">
            <form className="composer" onSubmit={(event) => void sendMessage(event)}>
              <textarea aria-label="Message" placeholder="Ask your local assistant…" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onInputKeyDown} maxLength={8000} disabled={pendingCloudSend !== null} />
              <button className="button" type="submit" disabled={!input.trim() || loading || !selectedProvider || pendingCloudSend !== null}>{loading ? <span className="spinner" /> : error && input.trim() ? "Retry" : "Send"}{!loading && <span aria-hidden="true">↑</span>}</button>
            </form>
            <div className="composer-hint">Enter to send · Shift + Enter for a new line · {selectedProviderInfo?.cloud ? "Cloud chat sends your message to the selected provider." : "Local Ollama chat stays on this device."}</div>
          </div>
        </div>
      </section>
    </>
  );
}
