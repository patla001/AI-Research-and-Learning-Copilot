"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { PaperDetail } from "@/lib/types";

interface Props {
  paperId: string;
  goalId: number | null;
  onClose: () => void;
  onChanged: () => void;
}

export default function PaperSheet({ paperId, goalId, onClose, onChanged }: Props) {
  const [paper, setPaper] = useState<PaperDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  const load = () =>
    api.paper(paperId).then(setPaper).catch((err) => setError(err instanceof ApiError ? err.message : String(err)));

  useEffect(() => {
    setPaper(null);
    setError(null);
    void load();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paperId]);

  const saveNote = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!note.trim()) return;
    setSaving(true);
    try {
      await api.addNote(note.trim(), paperId, goalId ?? undefined);
      setNote("");
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const link = paper?.oa_url ?? paper?.doi ?? `https://openalex.org/${paperId}`;

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Paper details">
        <button className="link" onClick={onClose}>Close</button>
        {error && <div className="notice notice-error" role="alert">{error}</div>}
        {!paper && !error && <p className="thinking">Loading paper</p>}
        {paper && (
          <>
            <h2>{paper.title}</h2>
            <div className="paper-byline">
              {paper.authors.join(", ")}
              {paper.publication_year ? `, ${paper.publication_year}` : ""}
              {paper.venue ? `. ${paper.venue}` : ""}
            </div>
            <div className="stop-actions" style={{ marginTop: 12 }}>
              <a className="btn btn-small" href={link} target="_blank" rel="noreferrer">
                {paper.oa_url ? "Read the paper" : "View source"}
              </a>
              <span className="badge">{paper.cited_by_count.toLocaleString()} citations</span>
              {paper.content_stored && <span className="badge badge-oa">Full text in library</span>}
              <span className="badge">{paperId}</span>
            </div>

            <section>
              <h3>Abstract</h3>
              <p className="sheet-abstract">{paper.abstract ?? "OpenAlex has no abstract for this paper."}</p>
            </section>

            {paper.progress.length > 0 && (
              <section>
                <h3>In your plans</h3>
                {paper.progress.map((p, i) => (
                  <div key={i} className="paper-byline">
                    {p.goal_title ?? "No goal"}: {p.plan_position ? `stop #${p.plan_position}, ` : ""}{p.status}
                  </div>
                ))}
              </section>
            )}

            <section>
              <h3>Your notes</h3>
              {paper.notes.length === 0 && <p className="paper-byline">No notes yet. The copilot reads your notes when it answers.</p>}
              {paper.notes.map((n) => (
                <div key={n.id} className="note">
                  {n.body}
                  <time dateTime={n.created_at}>{new Date(n.created_at).toLocaleDateString()}</time>
                </div>
              ))}
              <form onSubmit={saveNote} style={{ display: "grid", gap: 8, marginTop: 10 }}>
                <textarea className="field" value={note} onChange={(e) => setNote(e.target.value)} maxLength={5000}
                          placeholder="What did you take away from this paper?" aria-label="New note" />
                <div><button className="btn btn-small" disabled={saving || !note.trim()}>{saving ? "Saving" : "Save note"}</button></div>
              </form>
            </section>
          </>
        )}
      </div>
    </>
  );
}
