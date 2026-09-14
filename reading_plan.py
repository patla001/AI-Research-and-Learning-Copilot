"""
Reading-plan sequencing: a pure function over paper metadata.

The agent explains a plan; it does not invent the order. The order comes from
here, deterministically, from facts that can be checked:

  * Prerequisites. If paper A cites paper B and both are in the plan, B comes
    first. This is a topological sort over the in-plan citation graph.
  * Stage. Every paper gets one of four stages, and stages never interleave:
        orientation  surveys/tutorials (beginner and intermediate goals only)
        foundations  cited by other papers in the plan, and builds on none
        core         everything in between
        frontier     recent work nothing else in the plan builds on yet
  * Within a stage, ties break on a priority score: citations inside the plan,
    global citation count, relevance to the goal, and publication year.

Why stages cannot conflict with prerequisites: a foundations paper has no
in-plan prerequisites by definition, and a frontier paper is cited by nothing
in the plan, so no paper can depend on it. A survey does cite the originals -
that edge is deliberately ignored for orientation, because the point of a
survey is to be read before them.

No database access, so tests/test_reading_plan.py exercises it directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from openalex_client import looks_like_survey

STAGES = ("orientation", "foundations", "core", "frontier")
VALID_LEVELS = ("beginner", "intermediate", "advanced")


@dataclass
class PlanItem:
    paper_id: str
    title: str
    position: int
    stage: str
    rationale: str
    prerequisites: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "position": self.position,
            "stage": self.stage,
            "rationale": self.rationale,
            "prerequisites": self.prerequisites,
        }


def build_plan(papers: list[dict], level: str = "intermediate") -> list[PlanItem]:
    """Order papers into a reading plan.

    Each paper dict needs: id, title; optional: publication_year,
    cited_by_count, work_type, referenced_works, relevance (0..1, similarity of
    the paper to the learning goal).
    """
    if level not in VALID_LEVELS:
        raise ValueError(f"level must be one of {VALID_LEVELS}, got {level!r}")

    by_id: dict[str, dict] = {}
    for paper in papers:
        by_id.setdefault(paper["id"], paper)
    if not by_id:
        return []
    ids = set(by_id)

    survey_first = level in ("beginner", "intermediate")
    is_survey = {
        pid: looks_like_survey(p.get("title"), p.get("work_type")) for pid, p in by_id.items()
    }

    # In-plan citation graph. prereqs[A] = papers in the plan that A cites.
    prereqs: dict[str, set[str]] = {}
    cited_in_plan: dict[str, int] = {pid: 0 for pid in ids}
    for pid, paper in by_id.items():
        refs = {r for r in paper.get("referenced_works") or [] if r in ids and r != pid}
        for ref in refs:
            cited_in_plan[ref] += 1
        # Orientation readings are read before the work they summarize.
        prereqs[pid] = set() if (survey_first and is_survey[pid]) else refs

    years = [p.get("publication_year") for p in by_id.values() if p.get("publication_year")]
    newest = max(years) if years else None
    oldest = min(years) if years else None
    # "Recent" only means something when the plan spans more than one year;
    # otherwise every uncited paper would be labelled frontier.
    recent_cutoff = (newest - max(1, (newest - oldest) // 3)
                     if years and newest > oldest else None)

    stage_of: dict[str, str] = {}
    for pid, paper in by_id.items():
        year = paper.get("publication_year")
        if survey_first and is_survey[pid]:
            stage_of[pid] = "orientation"
        elif cited_in_plan[pid] > 0 and not prereqs[pid]:
            stage_of[pid] = "foundations"
        elif cited_in_plan[pid] == 0 and year and recent_cutoff and year > recent_cutoff:
            stage_of[pid] = "frontier"
        else:
            stage_of[pid] = "core"

    def priority(pid: str) -> float:
        paper = by_id[pid]
        score = (
            cited_in_plan[pid] * 1.0
            + math.log10(1 + (paper.get("cited_by_count") or 0)) * 0.5
            + float(paper.get("relevance") or 0.0) * 2.0
        )
        year = paper.get("publication_year")
        if year and newest and oldest and newest > oldest and level != "advanced":
            # Newcomers read the literature roughly in the order it was written.
            score -= (year - oldest) / (newest - oldest)
        return -score  # lower sorts first

    placed: list[str] = []
    placed_set: set[str] = set()
    cycle_broken: set[str] = set()
    remaining = set(ids)
    while remaining:
        ready = [pid for pid in remaining if prereqs[pid] <= placed_set]
        if not ready:
            # A citation cycle (it happens: preprints cite each other's later
            # versions). Break it at the highest-priority paper and say so.
            ready = list(remaining)
            choice = min(ready, key=lambda p: (STAGES.index(stage_of[p]), priority(p), p))
            cycle_broken.add(choice)
        else:
            choice = min(ready, key=lambda p: (STAGES.index(stage_of[p]), priority(p), p))
        placed.append(choice)
        placed_set.add(choice)
        remaining.discard(choice)

    position_of = {pid: i + 1 for i, pid in enumerate(placed)}
    items = []
    for pid in placed:
        paper = by_id[pid]
        before = sorted((r for r in prereqs[pid] if r in placed_set and position_of[r] < position_of[pid]),
                        key=position_of.get)
        items.append(PlanItem(
            paper_id=pid,
            title=paper.get("title") or pid,
            position=position_of[pid],
            stage=stage_of[pid],
            rationale=_rationale(pid, paper, stage_of[pid], cited_in_plan[pid],
                                 [position_of[r] for r in before], pid in cycle_broken),
            prerequisites=before,
        ))
    return items


def _rationale(pid, paper, stage, cited_count, before_positions, cycle) -> str:
    parts = []
    if stage == "orientation":
        parts.append("A survey or tutorial: read it first to get a map of the field")
    elif stage == "foundations":
        parts.append(f"Foundational here: {cited_count} other paper(s) in this plan build on it")
    elif stage == "frontier":
        year = paper.get("publication_year")
        parts.append(f"Recent work{f' ({year})' if year else ''} that nothing else in this plan builds on yet")
    else:
        parts.append("Core reading for this goal")
    if before_positions:
        parts.append("builds on #" + ", #".join(str(p) for p in before_positions))
    if cycle:
        parts.append("placed here to break a citation cycle")
    return "; ".join(parts) + "."


def recommend_next(plan: list[dict]) -> dict:
    """Pick the next paper from a plan with statuses.

    `plan` rows need: paper_id, position, status, and optional prerequisites.
    Order of preference: finish what is in progress; otherwise the first planned
    paper whose prerequisites are all done; otherwise the first planned paper
    (and name what it is waiting on).
    """
    ordered = sorted(plan, key=lambda row: (row.get("position") is None, row.get("position") or 0))
    done = {row["paper_id"] for row in ordered if row.get("status") in ("completed", "skipped")}
    total = len(ordered)
    completed = sum(1 for row in ordered if row.get("status") == "completed")

    def result(row, reason, waiting_on=None):
        return {"next": row, "reason": reason, "waiting_on": waiting_on or [],
                "completed": completed, "total": total}

    for row in ordered:
        if row.get("status") == "reading":
            return result(row, "You already started this one - finishing it keeps the sequence intact.")

    planned = [row for row in ordered if row.get("status") == "planned"]
    for row in planned:
        missing = [p for p in row.get("prerequisites") or [] if p not in done]
        if not missing:
            reason = ("It is the earliest unread paper in your plan"
                      + (" and everything it builds on is done." if row.get("prerequisites") else "."))
            return result(row, reason)

    if planned:
        row = planned[0]
        missing = [p for p in row.get("prerequisites") or [] if p not in done]
        return result(row, "Every remaining paper builds on something unread; this is the earliest.", missing)

    return {"next": None, "reason": "Every paper in this plan is completed or skipped.",
            "waiting_on": [], "completed": completed, "total": total}
