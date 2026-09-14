export type GoalLevel = "beginner" | "intermediate" | "advanced";
export type ProgressStatus = "planned" | "reading" | "completed" | "skipped";
export type Stage = "orientation" | "foundations" | "core" | "frontier";

export interface Goal {
  id: number;
  title: string;
  description: string | null;
  level: GoalLevel;
  status: "active" | "completed" | "archived";
  planned_papers: number;
  completed_papers: number;
}

export interface SearchResult {
  id: string;
  title: string;
  publication_year: number | null;
  venue: string | null;
  work_type: string | null;
  cited_by_count: number;
  is_oa: boolean;
  has_content: boolean;
  doi: string | null;
  primary_topic: string | null;
  authors: string[];
  abstract_preview: string;
}

export interface PaperSummary {
  id: string;
  title: string;
  publication_year: number | null;
  venue: string | null;
  cited_by_count: number;
  is_oa: boolean;
  oa_url: string | null;
  doi: string | null;
  has_content: boolean;
  content_stored: boolean;
  authors: string[];
}

export interface PaperDetail extends PaperSummary {
  abstract: string | null;
  primary_topic: string | null;
  progress: { goal_id: number | null; goal_title: string | null; status: ProgressStatus; plan_position: number | null }[];
  notes: { id: number; body: string; created_at: string }[];
  collections: { id: number; name: string }[];
}

export interface Collection {
  id: number;
  name: string;
  description: string | null;
  goal_id: number | null;
  goal_title: string | null;
  paper_count: number;
}

export interface CollectionDetail extends Collection {
  papers: (PaperSummary & { added_by: "user" | "agent"; reason: string | null; status: ProgressStatus | null })[];
}

export interface PlanItem {
  paper_id: string;
  title: string;
  position: number;
  stage: Stage;
  rationale: string;
  prerequisites: string[];
  status?: ProgressStatus;
  publication_year?: number | null;
}

export interface Plan {
  goal_id: number | null;
  items: PlanItem[];
}

export interface NextRecommendation {
  next: (PlanItem & { status: ProgressStatus }) | null;
  reason: string;
  waiting_on: string[];
  completed: number;
  total: number;
}

export interface Evidence {
  citation_key: string;
  source_type: "abstract" | "content" | "note";
  paper_id: string | null;
  title: string | null;
  publication_year: number | null;
  chunk_text: string;
  similarity: number;
}

export interface Citation {
  key: string;
  kind: "paper" | "note";
  title: string | null;
  year?: number | null;
  doi?: string | null;
  url?: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  unverified_citations: string[];
  tool_calls: { tool: string; input: Record<string, unknown>; error: boolean }[];
  changes: { type: string }[];
  stop_reason: string;
}

export interface Stats {
  user: { active_goals: number; collections: number; saved_papers: number; completed: number; reading: number; notes: number };
  agent: { enabled: boolean; model: string; remaining_today: number | null };
  context: { pending?: Record<string, number>; chunks?: Record<string, number>; error?: string };
  openalex: { api_key_configured: boolean };
}
