"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/components/api";

type ResearchResult = {
  title: string;
  url: string;
  snippet: string;
  published_at: string | null;
  domain: string;
  score: number | null;
};
type ResearchSource = {
  url: string;
  final_url: string | null;
  title: string | null;
  domain: string;
  text: string;
  retrieved_at: string;
  truncated: boolean;
  is_untrusted: true;
};
type ResearchResponse = {
  query: string;
  results: ResearchResult[];
  sources: ResearchSource[];
  retrieved_at: string;
  errors: Array<{ code: string; message: string }>;
  warnings: Array<{ code: string; message: string; source_url: string | null }>;
};

export default function ResearchPage() {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState("5");
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [response, setResponse] = useState<ResearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void api<{ configured: boolean }>("/api/research/status")
      .then((result) => setConfigured(result.configured))
      .catch((reason: Error) => setError(reason.message));
  }, []);

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim() || busy) return;
    setBusy(true);
    setError("");
    setResponse(null);
    try {
      setResponse(await api<ResearchResponse>("/api/research", {
        method: "POST",
        body: JSON.stringify({ query: query.trim(), limit: Number(limit), extract_sources: true }),
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Web research failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-heading"><div className="eyebrow">WEB INTELLIGENCE</div><h1>Research</h1><p className="subheading">Search results and bounded source excerpts from the configured web research provider.</p></div>
      {error && <div className="notice" role="alert">{error}</div>}
      <div className="notice">{configured ? "Web research is configured. Your search query is sent to Tavily; retrieved public pages are treated as untrusted source content." : "Web research is not configured. Set TAVILY_API_KEY in backend/.env, then restart FastAPI."}</div>
      <section className="panel remote-panel">
        <form className="remote-toolbar" onSubmit={(event) => void search(event)}>
          <input className="field" aria-label="Research query" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={1000} placeholder="What would you like to research?" />
          <select className="field" aria-label="Maximum results" value={limit} onChange={(event) => setLimit(event.target.value)}><option value="5">5 sources</option><option value="10">10 sources</option></select>
          <button className="button" disabled={busy || !query.trim()}>{busy ? "Researching…" : "Search the web"}</button>
        </form>
      </section>
      {response && <>
        {response.errors.map((item, index) => <div className="notice" role="alert" key={`${item.code}-${index}`}>{item.message}</div>)}
        {response.warnings.map((item, index) => <div className="notice" key={`${item.code}-${index}`}>{item.message}</div>)}
        <section className="panel remote-panel">
          <div className="panel-title"><h2>Search results</h2><span className="quiet-label">{response.results.length} results · {new Date(response.retrieved_at).toLocaleString()}</span></div>
          {!response.results.length && !response.errors.length ? <div className="empty-state">No results were returned for this search.</div> : response.results.map((item) => <article className="search-result" key={item.url}>
            <a className="document-title" href={item.url} target="_blank" rel="noreferrer"><strong>{item.title}</strong></a><p>{item.snippet}</p><small className="quiet-label">{item.domain}{item.published_at ? ` · ${item.published_at}` : ""}</small>
          </article>)}
        </section>
        {response.sources.length > 0 && <section className="panel remote-panel">
          <div className="panel-title"><h2>Extracted source passages</h2><span className="quiet-label">Untrusted reference text</span></div>
          {response.sources.map((source) => <article className="search-result" key={source.url}><a className="document-title" href={source.final_url ?? source.url} target="_blank" rel="noreferrer"><strong>{source.title ?? source.domain}</strong></a><p className="research-source-text">{source.text}</p>{source.truncated && <small className="quiet-label">Excerpt was truncated to the configured source limit.</small>}</article>)}
        </section>}
      </>}
    </>
  );
}
