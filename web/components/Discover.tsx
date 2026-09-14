"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Collection, Goal, SearchResult } from "@/lib/types";

interface Props {
  goal: Goal;
  collections: Collection[];
  onSaved: () => void;
  onOpen: (paperId: string) => void;
}

export default function Discover({ goal, collections, onSaved, onOpen }: Props) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("relevance");
  const [openAccess, setOpenAccess] = useState(false);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [searchedFor, setSearchedFor] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<Set<string>>(new Set());

  const goalCollection = collections.find((c) => c.goal_id === goal.id);

  const search = async (event?: React.FormEvent) => {
    event?.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await api.search({ q: query.trim() || undefined, goalId: query.trim() ? undefined : goal.id,
                                          sort, openAccess });
      setResults(response.results);
      setSearchedFor(response.query);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  // Saving = import (metadata, authors, full text, vectors) + add to the goal's
  // collection, created on first save so the learner never has to set one up.
  const save = async (paper: SearchResult) => {
    setSaving((s) => new Set(s).add(paper.id));
    setError(null);
    try {
      await api.importPapers([paper.id]);
      const collection = goalCollection ?? (await api.createCollection(`${goal.title}`, goal.id));
      await api.addToCollection(collection.id, [paper.id], "Saved from search");
      setSaved((s) => new Set(s).add(paper.id));
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving((s) => { const next = new Set(s); next.delete(paper.id); return next; });
    }
  };

  return (
    <section aria-label="Find papers">
      <form className="search-bar" onSubmit={search}>
        <input className="field" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={300}
               placeholder={`Search OpenAlex, or leave empty to search for "${goal.title}"`} aria-label="Search papers" />
        <button className="btn" disabled={loading}>{loading ? "Searching" : "Search"}</button>
      </form>
      <div className="search-options">
        <label>Order by
          <select className="field" style={{ width: "auto", padding: "2px 6px" }} value={sort}
                  onChange={(e) => setSort(e.target.value)}>
            <option value="relevance">Best match</option>
            <option value="citations">Most cited</option>
            <option value="recent">Newest</option>
          </select>
        </label>
        <label><input type="checkbox" checked={openAccess} onChange={(e) => setOpenAccess(e.target.checked)} />
          Open access only</label>
      </div>

      {error && <div className="notice notice-error" role="alert">{error}</div>}

      {results === null && !loading && (
        <div className="empty">
          <h2>Find papers for this goal</h2>
          <p>Search runs against OpenAlex, which indexes over 250 million scholarly works. Nothing is saved until you
            save it, and saved papers are what the copilot reads.</p>
        </div>
      )}
      {results && results.length === 0 && (
        <div className="notice">No papers matched &ldquo;{searchedFor}&rdquo;. Try fewer or broader words.</div>
      )}

      {results && results.length > 0 && (
        <ul className="paper-list">
          {results.map((paper) => (
            <li key={paper.id} className="paper-row">
              <div>
                <h3 className="paper-title">{paper.title}</h3>
                <div className="paper-byline">
                  {paper.authors.slice(0, 3).join(", ")}{paper.authors.length > 3 ? " et al." : ""}
                  {paper.publication_year ? `, ${paper.publication_year}` : ""}
                  {paper.venue ? `, ${paper.venue}` : ""}
                </div>
                {paper.abstract_preview && <p className="paper-abstract">{paper.abstract_preview}</p>}
              </div>
              <div className="paper-side">
                {saved.has(paper.id) ? (
                  <button className="btn btn-quiet btn-small" onClick={() => onOpen(paper.id)}>Saved</button>
                ) : (
                  <button className="btn btn-small" onClick={() => save(paper)} disabled={saving.has(paper.id)}>
                    {saving.has(paper.id) ? "Saving" : "Save"}
                  </button>
                )}
                <span className="badge">{paper.cited_by_count.toLocaleString()} citations</span>
                {paper.is_oa && <span className="badge badge-oa">{paper.has_content ? "Full text" : "Open access"}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
