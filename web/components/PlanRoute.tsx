"use client";

import type { NextRecommendation, PlanItem, ProgressStatus, Stage } from "@/lib/types";

const STAGE_LABEL: Record<Stage, string> = {
  orientation: "Get oriented",
  foundations: "Foundations",
  core: "Core reading",
  frontier: "The frontier",
};

const STAGE_HINT: Record<Stage, string> = {
  orientation: "surveys that map the field",
  foundations: "the work the rest builds on",
  core: "the main body of the goal",
  frontier: "recent work nothing here builds on yet",
};

const STATUSES: { value: ProgressStatus; label: string }[] = [
  { value: "reading", label: "Reading" },
  { value: "completed", label: "Done" },
  { value: "skipped", label: "Skip" },
];

interface Props {
  items: PlanItem[];
  next: NextRecommendation | null;
  building: boolean;
  onBuild: () => void;
  onStatus: (paperId: string, status: ProgressStatus) => void;
  onOpen: (paperId: string) => void;
}

export default function PlanRoute({ items, next, building, onBuild, onStatus, onOpen }: Props) {
  if (items.length === 0) {
    return (
      <div className="empty">
        <h2>No reading plan yet</h2>
        <p>Save a few papers to a collection for this goal, then build the plan. The order comes from which
          papers cite which, so it gets better with every paper you add.</p>
        <button className="btn" onClick={onBuild} disabled={building}>{building ? "Building plan" : "Build plan"}</button>
      </div>
    );
  }

  const positionOf = new Map(items.map((item) => [item.paper_id, item.position]));

  return (
    <>
      {next?.next && (
        <div className="next-up">
          <span className="next-up-label">Read next ({next.completed} of {next.total} done)</span>
          <button className="next-up-title stop-title" onClick={() => onOpen(next.next!.paper_id)}>
            #{next.next.position} {next.next.title}
          </button>
          <span className="next-up-reason">{next.reason}</span>
        </div>
      )}
      {next && !next.next && next.total > 0 && (
        <div className="next-up"><span className="next-up-label">Plan complete</span>
          <span className="next-up-reason">{next.reason}</span></div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-quiet btn-small" onClick={onBuild} disabled={building}>
          {building ? "Rebuilding" : "Rebuild plan"}
        </button>
      </div>

      <ol className="route" aria-label="Reading plan">
        {items.map((item, index) => {
          const status = item.status ?? "planned";
          const stageStarts = index === 0 || items[index - 1].stage !== item.stage;
          return (
            <li key={item.paper_id} className="stop" data-stage={item.stage} data-status={status}>
              <span className="stop-marker" aria-label={`Stop ${item.position}, ${status}`}>{item.position}</span>
              <div>
                {stageStarts && (
                  <div className="stage-start"><strong>{STAGE_LABEL[item.stage]}</strong>, {STAGE_HINT[item.stage]}</div>
                )}
                <button className="stop-title" onClick={() => onOpen(item.paper_id)}>{item.title}</button>
                <div className="stop-meta">
                  {item.publication_year ?? "Year unknown"}
                  {item.prerequisites.length > 0 &&
                    `, read after ${item.prerequisites.map((p) => `#${positionOf.get(p)}`).join(" and ")}`}
                </div>
                <p className="stop-rationale">{item.rationale}</p>
                <div className="stop-actions">
                  {STATUSES.map((s) => (
                    <button key={s.value} className="status-btn" aria-pressed={status === s.value}
                            onClick={() => onStatus(item.paper_id, status === s.value ? "planned" : s.value)}>
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </>
  );
}
