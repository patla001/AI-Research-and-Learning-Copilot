/**
 * Flask API client.
 *
 * Every path is relative: the console is served by the same Flask process, so
 * requests inherit the origin and, on Databricks Apps, the OAuth session.
 * NEXT_PUBLIC_API_BASE exists only for `next dev` on :3000 against Flask on :8000.
 */
import type {
  ChatResponse, ChatTurn, Collection, CollectionDetail, Evidence, Goal, GoalLevel,
  NextRecommendation, Plan, PaperDetail, ProgressStatus, SearchResult, Stats,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("Can't reach the server. Check that the app is running.", 0);
  }
  // A Databricks App answers an expired session with its sign-in page as HTML
  // and HTTP 200, so the content type is checked before the status.
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new ApiError(
      response.status === 200
        ? "Your session expired. Reload the page to sign in again."
        : `${path} failed with HTTP ${response.status}.`,
      response.status,
    );
  }
  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(body?.error ?? `${path} failed with HTTP ${response.status}.`, response.status);
  }
  return body as T;
}

const send = <T>(method: string, path: string, body: unknown) =>
  request<T>(path, { method, body: JSON.stringify(body) });

export const api = {
  stats: () => request<Stats>("/copilot/stats"),

  goals: () => request<{ goals: Goal[] }>("/goals").then((r) => r.goals),
  createGoal: (goal: { title: string; description?: string; level: GoalLevel }) =>
    send<{ goal: Goal }>("POST", "/goals", goal).then((r) => r.goal),

  search: (opts: { q?: string; goalId?: number; sort?: string; openAccess?: boolean; fromYear?: number }) => {
    const params = new URLSearchParams({ limit: "12" });
    if (opts.q) params.set("q", opts.q);
    if (opts.goalId) params.set("goal_id", String(opts.goalId));
    if (opts.sort) params.set("sort", opts.sort);
    if (opts.openAccess) params.set("open_access", "true");
    if (opts.fromYear) params.set("from_year", String(opts.fromYear));
    return request<{ query: string; results: SearchResult[] }>(`/papers/search?${params}`);
  },

  importPapers: (paperIds: string[]) =>
    send<{ imported: string[]; errors: { id: string; error: string }[] }>(
      "POST", "/papers/import", { paper_ids: paperIds }),

  paper: (id: string) => request<PaperDetail>(`/papers/${encodeURIComponent(id)}`),

  collections: (goalId?: number) =>
    request<{ collections: Collection[] }>(`/collections${goalId ? `?goal_id=${goalId}` : ""}`)
      .then((r) => r.collections),
  collection: (id: number) => request<CollectionDetail>(`/collections/${id}`),
  createCollection: (name: string, goalId?: number) =>
    send<{ collection: Collection }>("POST", "/collections", { name, goal_id: goalId }).then((r) => r.collection),
  addToCollection: (collectionId: number, paperIds: string[], reason?: string) =>
    send<{ added: string[]; not_in_library: string[] }>(
      "POST", `/collections/${collectionId}/papers`, { paper_ids: paperIds, reason }),
  removeFromCollection: (collectionId: number, paperId: string) =>
    send<{ removed: number }>("DELETE", `/collections/${collectionId}/papers/${paperId}`, {}),

  plan: (goalId: number) => request<Plan>(`/plans?goal_id=${goalId}`),
  buildPlan: (goalId: number) => send<Plan>("POST", "/plans", { goal_id: goalId }),
  next: (goalId: number) => request<NextRecommendation>(`/plans/next?goal_id=${goalId}`),
  setProgress: (paperId: string, status: ProgressStatus, goalId?: number) =>
    send("PUT", "/progress", { paper_id: paperId, status, goal_id: goalId }),

  addNote: (body: string, paperId?: string, goalId?: number) =>
    send("POST", "/notes", { body, paper_id: paperId, goal_id: goalId }),

  retrieve: (query: string, goalId?: number) =>
    send<{ evidence: Evidence[] }>("POST", "/retrieve", { query, goal_id: goalId }),

  chat: (message: string, history: ChatTurn[], goalId?: number) =>
    send<ChatResponse>("POST", "/copilot/chat", { message, history, goal_id: goalId }),
};
