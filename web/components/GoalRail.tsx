"use client";

import { useState } from "react";
import type { Goal, GoalLevel, Stats } from "@/lib/types";

interface Props {
  goals: Goal[];
  /** True once the first goals request has finished, so "no goals" is real. */
  ready: boolean;
  selectedId: number | null;
  stats: Stats | null;
  onSelect: (id: number) => void;
  onCreate: (goal: { title: string; description?: string; level: GoalLevel }) => Promise<void>;
}

export default function GoalRail({ goals, ready, selectedId, stats, onSelect, onCreate }: Props) {
  // Not useState(goals.length === 0): goals load after the first render, so
  // that initial value was always true and the form opened on every visit.
  const [adding, setAdding] = useState(false);
  const showForm = adding || (ready && goals.length === 0);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [level, setLevel] = useState<GoalLevel>("beginner");
  const [saving, setSaving] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    setSaving(true);
    try {
      await onCreate({ title: title.trim(), description: description.trim() || undefined, level });
      setTitle("");
      setDescription("");
      setAdding(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <aside className="rail" data-collapsed={collapsed}>
      <div className="brand">
        <span className="brand-mark" aria-hidden />
        <span className="brand-name">Reading Route</span>
        <button className="link" style={{ marginLeft: "auto", fontSize: "0.85rem" }}
                onClick={() => setCollapsed((c) => !c)} aria-expanded={!collapsed}>
          {collapsed ? "Show goals" : "Hide"}
        </button>
      </div>

      <div className="rail-body">
        <div className="rail-section">
          <div className="rail-heading">
            <span>Learning goals</span>
            {!showForm &&<button className="link" onClick={() => setAdding(true)}>New goal</button>}
          </div>

          {showForm &&(
            <form className="goal-form" onSubmit={submit}>
              <input className="field" placeholder="What do you want to learn?" value={title}
                     onChange={(e) => setTitle(e.target.value)} maxLength={200} autoFocus aria-label="Goal" />
              <textarea className="field" placeholder="Why, or what you already know (optional)" value={description}
                        onChange={(e) => setDescription(e.target.value)} aria-label="Goal details" />
              <select className="field" value={level} onChange={(e) => setLevel(e.target.value as GoalLevel)}
                      aria-label="Your level">
                <option value="beginner">I'm new to this</option>
                <option value="intermediate">I know the basics</option>
                <option value="advanced">I know the field</option>
              </select>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn" disabled={saving || !title.trim()}>{saving ? "Saving" : "Create goal"}</button>
                {goals.length > 0 && (
                  <button type="button" className="btn btn-quiet" onClick={() => setAdding(false)}>Cancel</button>
                )}
              </div>
            </form>
          )}
        </div>

        <ul className="goal-list rail-section" style={{ paddingTop: 0 }}>
          {goals.map((goal) => {
            const pct = goal.planned_papers ? Math.round((goal.completed_papers / goal.planned_papers) * 100) : 0;
            return (
              <li key={goal.id} className="goal-item" aria-current={goal.id === selectedId}>
                <button onClick={() => onSelect(goal.id)}>
                  <span className="goal-title">{goal.title}</span>
                  <span className="goal-meta">
                    <span>{goal.planned_papers ? `${goal.completed_papers} of ${goal.planned_papers} read` : "No plan yet"}</span>
                    {goal.planned_papers > 0 && (
                      <span className="progress-bar" aria-hidden><span style={{ width: `${pct}%` }} /></span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>

        {stats && (
          <div className="rail-footer">
            {stats.user.saved_papers} saved papers, {stats.user.notes} notes
            {!stats.openalex.api_key_configured && (
              <div style={{ marginTop: 4 }}>Full text is off until an OpenAlex key is added.</div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
