
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

function apiBaseForCurrentOrigin(): string {
  if (typeof window === "undefined") {
    return API_BASE.replace(/\/+$/, "");
  }

  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;

  if (!configured) {
    const hostname = window.location.hostname;
    const loopbackHosts = ["localhost", "127.0.0.1", "::1"];

    if (loopbackHosts.includes(hostname)) {
      return `${window.location.protocol}//${hostname}:8000`;
    }

    return API_BASE.replace(/\/+$/, "");
  }

  const base = new URL(configured);
  const loopbackHosts = ["localhost", "127.0.0.1", "::1"];

  if (
    loopbackHosts.includes(window.location.hostname) &&
    loopbackHosts.includes(base.hostname) &&
    base.hostname !== window.location.hostname
  ) {
    base.hostname = window.location.hostname;
  }

  return base.toString().replace(/\/+$/, "");
}

export type Source = {
  document_id: string;
  document_name?: string;
  category?: string;
  original_filename?: string;
  filename: string;
  page_number: number | null;
  snippet: string;
  excerpt?: string;
  section_heading?: string | null;
  relevance_score?: number;
  kind?: "document" | "project_file";
  project_id?: string;
  path?: string;
  answer_state?: string | null;
  requires_verification?: boolean;
  ocr_confidence?: number | null;
};

export type SearchResult = Source;
export type ProjectSource = {
  kind: "project_file";
  project_id: string;
  path: string;
  snippet: string;
  document_id?: string;
  filename?: string;
  page_number?: number | null;
  section_heading?: string | null;
};

export type WebResearchSource = {
  kind: "web";
  title: string;
  url: string;
  domain: string;
  published_at: string | null;
  retrieved_at: string;
  snippet: string;
  is_untrusted: true;
};

export type WebResearchStatus = {
  kind: "web_research_status";
  status: "retrieved" | "failed" | "no_results";
  query: string;
  retrieved_at: string;
  errors: Array<{ code: string; message: string }>;
  warnings: Array<{ code: string; message: string; source_url?: string | null }>;
};

export type MessageSource = Source | ProjectSource | WebResearchSource | WebResearchStatus;

/** Absolute API URL for the current origin, for authenticated binary fetches. */
export function apiUrl(path: string): string {
  return `${apiBaseForCurrentOrigin()}${path}`;
}

export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message: string | null;
};

export type Message = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  sources: MessageSource[];
  created_at: string;
  provider?: string | null;
  model?: string | null;
  execution?: AgentExecution | null;
};

export type AgentExecution = {
  id: string;
  agent_id: string;
  reason: string;
  status: "succeeded" | "partial" | "failed";
  tools: string[];
  sources: Array<Record<string, string | number | null>>;
  task_ids: string[];
  approval_ids: string[];
  model_used: boolean;
  steps: number;
  answer_state?: string | null;
};

export type ApplicationTask = {
  id: string;
  title: string;
  description: string;
  status: "pending" | "in_progress" | "blocked" | "done";
  conversation_id: string | null;
  created_at: string;
  updated_at: string;
  due_at: string | null;
  reminder_preference: "none" | "at_due" | "15_minutes" | "1_hour" | "1_day";
  completed_at: string | null;
  priority: "low" | "medium" | "high" | "urgent";
  project_id: string | null;
  milestone_id: string | null;
};

export type ProviderInfo = {
  id: string;
  name: string;
  cloud: boolean;
  configured: boolean;
  selected: boolean;
  model: string | null;
  configuration_error: string | null;
  connection_verified: boolean;
  privacy?: {
    processing: string;
    api_key_required: boolean;
    leaves_device: boolean;
    data_sent: string;
  };
};

export type ProviderInventory = {
  selected_provider: string;
  providers: ProviderInfo[];
};

export type DocumentItem = {
  id: string;
  original_name: string;
  display_name?: string | null;
  description?: string | null;
  category?: string | null;
  mime_type?: string | null;
  encryption?: string | null;
  extension: string;
  size_bytes: number;
  page_count: number | null;
  status: string;
  created_at: string;
  content_hash: string | null;
  extracted_text_available: boolean;
  chunk_count: number;
  last_processed_at: string | null;
  processing_error: string | null;
  extraction_method: string | null;
  processed_pages?: number;
  failed_pages?: number;
  ocr_status?: string | null;
  processing_stage?: string | null;
  processing_attempts?: number;
  last_attempt_at?: string | null;
  page_summary?: DocumentPageSummary;
};

export type DocumentPageSummary = {
  total: number;
  processed: number;
  failed: number;
  ocr_pages: number;
  pages: Array<{
    page_number: number | null;
    status: string;
    method: string | null;
    ocr_confidence: number | null;
    error_category: string | null;
    chunk_count: number;
  }>;
};

export type DocumentHealth = {
  ready: boolean;
  ocr: {
    available: boolean;
    version: string | null;
    source: string | null;
    executable_configured: boolean;
    languages: string[];
    usable_languages: string[];
    missing_languages: string[];
    missing_language_labels: string[];
    probe_passed: boolean;
    message: string;
    action: string | null;
    errors: Array<{ category?: string; message?: string; source?: string }>;
  };
  pdf_rendering: { available: boolean; message: string };
  image_processing: { available: boolean; message: string };
  storage: { available: boolean; message: string; encryption: string };
  worker: { mode: string; active_count: number; queued: number; stale_jobs: number; concurrency: number };
  documents: { documents: number; ready: number; partial: number; failed: number; retry_required: number; active: number };
  last_successful_processing: string | null;
  limits: { max_pdf_pages: number; ocr_max_pages: number; ocr_dpi: number };
};

export type DocumentFacet = { value: string; count: number };

export type DocumentLibrary = {
  items: DocumentItem[];
  total: number;
  limit: number;
  offset: number;
  facets: { categories: DocumentFacet[]; extensions: DocumentFacet[]; statuses: DocumentFacet[] };
};

export type DocumentPages = {
  document: DocumentItem;
  pages: Array<{
    page_number: number | null;
    content: string;
    status?: string;
    method?: string | null;
    ocr_confidence?: number | null;
    error_category?: string | null;
    chunk_count?: number;
  }>;
  requested_page: number | null;
  page_count: number;
};

const API_REQUEST_TIMEOUT_MS = 110_000;

const SAFE_METHODS = ["GET", "HEAD", "OPTIONS"];

const CSRF_COOKIE_NAME = "r_matrix_csrf";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Reads a cookie by name.
 * Returns undefined when the cookie is unavailable.
 */
function getCookie(name: string): string | undefined {
  if (typeof document === "undefined") {
    return undefined;
  }

  const prefix = `${name}=`;

  const cookie = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));

  if (!cookie) {
    return undefined;
  }

  const value = cookie.slice(prefix.length);

  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Adds CSRF protection to unsafe requests.
 */
function addCsrfHeader(headers: Headers): void {
  const csrfToken = getCookie(CSRF_COOKIE_NAME);

  if (csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
}

/**
 * Builds headers consistently for JSON and other request bodies.
 */
function prepareHeaders(
  init: RequestInit,
  method: string,
): Headers {
  const headers = new Headers(init.headers);

  // JSON.stringify() produces a string.
  // FormData must not receive a manually assigned Content-Type.
  if (
    typeof init.body === "string" &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  if (!SAFE_METHODS.includes(method)) {
    addCsrfHeader(headers);
  }

  return headers;
}

/**
 * Main API client.
 */
export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  let responseBody: string;
  let timedOut = false;

  const controller = new AbortController();

  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, API_REQUEST_TIMEOUT_MS);

  const method = (init.method ?? "GET").toUpperCase();
  const headers = prepareHeaders(init, method);

  const callerSignal = init.signal;

  const abortFromCaller = () => {
    controller.abort(callerSignal?.reason);
  };

  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, {
      once: true,
    });
  }

  try {
    const base = apiBaseForCurrentOrigin();

    response = await fetch(`${base}${path}`, {
      ...init,
      method,
      headers,
      credentials: init.credentials ?? "include",
      signal: controller.signal,
    });

    responseBody =
      response.status === 204
        ? ""
        : await response.text();
  } catch {
    if (timedOut) {
      throw new Error(
        "The request timed out. Check the provider status and try again.",
      );
    }

    if (callerSignal?.aborted) {
      throw new Error("The request was cancelled.");
    }

    throw new Error(
      "Cannot reach the backend. Verify that FastAPI is running and the configured API URL and dashboard origin are correct.",
    );
  } finally {
    clearTimeout(timeoutId);

    callerSignal?.removeEventListener(
      "abort",
      abortFromCaller,
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  let body: unknown;

  if (responseBody.trim()) {
    try {
      body = JSON.parse(responseBody);
    } catch {
      if (response.ok) {
        throw new Error(
          "The backend returned an invalid response. Please retry.",
        );
      }
    }
  }

  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/api/auth/login" &&
      path !== "/api/auth/me" &&
      typeof window !== "undefined"
    ) {
      window.dispatchEvent(
        new Event("r-matrix:unauthorized"),
      );
    }

    if (response.status === 503 && path === "/api/auth/login") {
      const detail =
        typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof body.detail === "string"
          ? body.detail
          : "Authentication is not configured.";

      throw new ApiError(detail, response.status);
    }

    if (response.status === 504) {
      throw new ApiError(
        "The backend request timed out. Check the provider status and try again.",
        response.status,
      );
    }

    if (response.status === 503) {
      const detail =
        typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof body.detail === "string"
          ? body.detail
          : "The service is temporarily unavailable. Check backend and provider configuration.";
      throw new ApiError(
        detail,
        response.status,
      );
    }

    if (response.status === 502) {
      throw new ApiError(
        "The AI provider could not complete the request. Check its status and try again.",
        response.status,
      );
    }

    if (response.status >= 500) {
      throw new ApiError(
        "The backend could not complete the request. Please try again.",
        response.status,
      );
    }

    const detail =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status}).`;

    throw new ApiError(detail, response.status);
  }

  if (!responseBody.trim()) {
    throw new Error(
      "The backend returned an empty response. Please retry.",
    );
  }

  return body as T;
}

/**
 * API client for remote-control operations.
 * Uses the same cookie, credentials and CSRF handling.
 */
export async function remoteApi<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  return api<T>(path, {
    ...init,
    credentials: "include",
  });
}

/**
 * Formats byte sizes.
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Formats dates.
 */
export function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
