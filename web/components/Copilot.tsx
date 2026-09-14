"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { ChatResponse, ChatTurn, Citation, Goal } from "@/lib/types";

interface Message {
  role: "user" | "assistant";
  content: string;
  response?: ChatResponse;
}

interface Props {
  goal: Goal | null;
  enabled: boolean;
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
  onOpenPaper: (paperId: string) => void;
}

const TOOL_LABEL: Record<string, string> = {
  find_papers: "searched OpenAlex",
  import_papers: "saved papers",
  retrieve_evidence: "read passages",
  get_paper: "opened a paper",
  add_to_collection: "updated a collection",
  create_collection: "created a collection",
  generate_reading_plan: "built a plan",
  update_progress: "updated progress",
  recommend_next_paper: "checked your plan",
  add_note: "saved a note",
  list_learning_context: "looked up your goals",
};

export default function Copilot({ goal, enabled, open, onClose, onChanged, onOpenPaper }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, pending]);

  const prompts = goal
    ? [
        "Find five papers for this goal and save the best ones",
        "Build my reading plan and tell me where to start",
        "Compare the papers in my collection",
        "What should I read next?",
      ]
    : ["Help me set up a learning goal"];

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || pending) return;
    const history: ChatTurn[] = messages.map(({ role, content }) => ({ role, content }));
    setMessages((m) => [...m, { role: "user", content: message }]);
    setDraft("");
    setPending(true);
    setError(null);
    try {
      const response = await api.chat(message, history, goal?.id);
      setMessages((m) => [...m, { role: "assistant", content: response.answer, response }]);
      if (response.changes.length > 0) onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <aside className="copilot" data-open={open} aria-label="Copilot">
      <div className="copilot-head">
        <div>
          <h2>Copilot</h2>
          <p>Answers come from your saved papers and notes, with sources.</p>
        </div>
        <button className="link copilot-close" onClick={onClose}>Close</button>
      </div>

      <div className="thread" ref={threadRef}>
        {!enabled && (
          <div className="notice">The copilot needs an Anthropic API key on the server. Everything else works without it.</div>
        )}
        {enabled && messages.length === 0 && (
          <div className="empty" style={{ padding: 0 }}>
            <p>Ask for papers, a plan, a summary, or a comparison. When the copilot saves a paper or updates your plan,
              the workspace updates too.</p>
          </div>
        )}
        {messages.map((message, index) =>
          message.role === "user" ? (
            <div key={index} className="turn-user">{message.content}</div>
          ) : (
            <Answer key={index} message={message} onOpenPaper={onOpenPaper} />
          ),
        )}
        {pending && <div className="thinking">Working through your library</div>}
        {error && <div className="notice notice-error" role="alert">{error}</div>}
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); void send(draft); }}>
        {messages.length === 0 && enabled && (
          <div className="prompts">
            {prompts.map((p) => (
              <button key={p} type="button" className="prompt-chip" onClick={() => void send(p)}>{p}</button>
            ))}
          </div>
        )}
        <div className="composer-row">
          <textarea className="field" rows={2} value={draft} maxLength={2000} disabled={!enabled}
                    placeholder={goal ? `Ask about "${goal.title}"` : "Ask the copilot"} aria-label="Message"
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(draft); } }} />
          <button className="btn" disabled={!enabled || pending || !draft.trim()}>Send</button>
        </div>
      </form>
    </aside>
  );
}

function Answer({ message, onOpenPaper }: { message: Message; onOpenPaper: (id: string) => void }) {
  const [active, setActive] = useState<string | null>(null);
  const response = message.response;
  const verified = new Map<string, Citation>((response?.citations ?? []).map((c) => [c.key, c]));
  const unverified = new Set(response?.unverified_citations ?? []);
  const tools = [...new Set((response?.tool_calls ?? []).map((t) => TOOL_LABEL[t.tool] ?? t.tool))];

  const renderInline = (text: string) =>
    text.split(/(\[(?:W|N)\d+\])/g).map((part, i) => {
      const match = part.match(/^\[((?:W|N)\d+)\]$/);
      if (!match) return <Fragment key={i}>{part}</Fragment>;
      const key = match[1];
      const citation = verified.get(key);
      return (
        <button key={i} type="button" className={`cite${unverified.has(key) ? " cite-unverified" : ""}`}
                title={citation?.title ?? "This source was not found in what the copilot retrieved"}
                aria-expanded={active === key}
                onClick={() => (citation?.kind === "paper" ? onOpenPaper(key) : setActive(active === key ? null : key))}
                onMouseEnter={() => setActive(key)} onMouseLeave={() => setActive(null)}>
          {key}
        </button>
      );
    });

  const blocks = message.content.split(/\n{2,}/);
  return (
    <div className="turn-assistant">
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*([-*]|\d+\.)\s+/.test(l))) {
          const ordered = /^\s*\d+\./.test(lines[0]);
          const items = lines.map((l, j) => <li key={j}>{renderInline(l.replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>);
          return ordered ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>;
        }
        return <p key={i}>{renderInline(block.replace(/\*\*(.+?)\*\*/g, "$1"))}</p>;
      })}

      {(verified.size > 0 || unverified.size > 0) && (
        <div className="sources">
          {[...verified.values()].map((c) => (
            <div key={c.key} className="source" data-active={active === c.key}>
              <span className="source-key">{c.key}</span>
              <span>{c.kind === "note" ? `Your note${c.title ? ` on ${c.title}` : ""}` : `${c.title ?? c.key}${c.year ? ` (${c.year})` : ""}`}</span>
            </div>
          ))}
          {[...unverified].map((key) => (
            <div key={key} className="source source-warning">
              <span className="source-key">{key}</span>
              <span>Not found in anything the copilot retrieved. Treat this claim with care.</span>
            </div>
          ))}
        </div>
      )}
      {tools.length > 0 && <div className="turn-trace">The copilot {tools.join(", ")}.</div>}
    </div>
  );
}
