"use client";

import { useCallback, useEffect, useState } from "react";
import CollectionView from "@/components/CollectionView";
import Copilot from "@/components/Copilot";
import Discover from "@/components/Discover";
import GoalRail from "@/components/GoalRail";
import PaperSheet from "@/components/PaperSheet";
import PlanRoute from "@/components/PlanRoute";
import { api, ApiError } from "@/lib/api";
import type {
  Collection, CollectionDetail, Goal, GoalLevel, NextRecommendation, PlanItem, ProgressStatus, Stats,
} from "@/lib/types";

type Tab = "plan" | "discover" | "saved";

const LEVEL_LABEL: Record<GoalLevel, string> = {
  beginner: "New to this",
  intermediate: "Knows the basics",
  advanced: "Knows the field",
};

export default function Page() {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [goalId, setGoalId] = useState<number | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collection, setCollection] = useState<CollectionDetail | null>(null);
  const [plan, setPlan] = useState<PlanItem[]>([]);
  const [next, setNext] = useState<NextRecommendation | null>(null);
  const [tab, setTab] = useState<Tab>("plan");
  const [openPaper, setOpenPaper] = useState<string | null>(null);
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [building, setBuilding] = useState(false);
  const [booting, setBooting] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const goal = goals.find((g) => g.id === goalId) ?? null;

  const fail = (err: unknown) => setError(err instanceof ApiError ? err.message : String(err));

  const loadGoals = useCallback(async () => {
    const [goalList, statsResponse] = await Promise.all([api.goals(), api.stats()]);
    setGoals(goalList);
    setStats(statsResponse);
    return goalList;
  }, []);

  const loadGoalData = useCallback(async (id: number) => {
    const [collectionList, planResponse, nextResponse] = await Promise.all([
      api.collections(id), api.plan(id), api.next(id),
    ]);
    setCollections(collectionList);
    setPlan(planResponse.items);
    setNext(nextResponse);
    setCollection(collectionList.length ? await api.collection(collectionList[0].id) : null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      await loadGoals();
      if (goalId !== null) await loadGoalData(goalId);
      setError(null);
    } catch (err) {
      fail(err);
    }
  }, [goalId, loadGoals, loadGoalData]);

  useEffect(() => {
    loadGoals()
      .then((list) => {
        const first = list.find((g) => g.status === "active") ?? list[0];
        if (first) setGoalId(first.id);
      })
      .catch(fail)
      .finally(() => setBooting(false));
  }, [loadGoals]);

  useEffect(() => {
    if (goalId !== null) loadGoalData(goalId).catch(fail);
  }, [goalId, loadGoalData]);

  const createGoal = async (input: { title: string; description?: string; level: GoalLevel }) => {
    try {
      const created = await api.createGoal(input);
      await loadGoals();
      setGoalId(created.id);
      setTab("discover");
    } catch (err) {
      fail(err);
      throw err;
    }
  };

  const buildPlan = async () => {
    if (goalId === null) return;
    setBuilding(true);
    try {
      await api.buildPlan(goalId);
      await refresh();
      setTab("plan");
    } catch (err) {
      fail(err);
    } finally {
      setBuilding(false);
    }
  };

  const setStatus = async (paperId: string, status: ProgressStatus) => {
    // Optimistic: the route marker changes on click, the server confirms after.
    setPlan((items) => items.map((i) => (i.paper_id === paperId ? { ...i, status } : i)));
    try {
      await api.setProgress(paperId, status, goalId ?? undefined);
      await refresh();
    } catch (err) {
      fail(err);
      await refresh();
    }
  };

  const removePaper = async (paperId: string) => {
    if (!collection) return;
    try {
      await api.removeFromCollection(collection.id, paperId);
      await refresh();
    } catch (err) {
      fail(err);
    }
  };

  return (
    <div className="shell">
      <GoalRail goals={goals} ready={!booting} selectedId={goalId} stats={stats} onSelect={(id) => { setGoalId(id); setTab("plan"); }}
                onCreate={createGoal} />

      <main className="workspace">
        <div className="workspace-inner">
          {error && (
            <div className="notice notice-error" role="alert">
              {error} <button className="link" onClick={() => setError(null)}>Dismiss</button>
            </div>
          )}

          {!booting && !goal && (
            <div className="empty">
              <h2>Start with what you want to learn</h2>
              <p>Name a learning goal on the left, like &ldquo;Understand retrieval augmented generation&rdquo;. You&rsquo;ll
                find papers for it, save the useful ones, and get a reading plan that puts them in order.</p>
            </div>
          )}

          {goal && (
            <>
              <header className="goal-header">
                <h1>{goal.title}</h1>
                {goal.description && <p>{goal.description}</p>}
                <span className="level">{LEVEL_LABEL[goal.level]}</span>
              </header>

              <div className="tabs" role="tablist" style={{ marginTop: 20 }}>
                {([
                  ["plan", "Reading plan", plan.length],
                  ["discover", "Find papers", null],
                  ["saved", "Saved", collection?.papers.length ?? 0],
                ] as [Tab, string, number | null][]).map(([key, label, count]) => (
                  <button key={key} role="tab" className="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
                    {label}{count !== null && <span className="tab-count">{count}</span>}
                  </button>
                ))}
              </div>

              {tab === "plan" && (
                <PlanRoute items={plan} next={next} building={building} onBuild={buildPlan}
                           onStatus={setStatus} onOpen={setOpenPaper} />
              )}
              {tab === "discover" && (
                <Discover goal={goal} collections={collections} onSaved={refresh} onOpen={setOpenPaper} />
              )}
              {tab === "saved" && (
                <CollectionView collection={collection} onRemove={removePaper} onOpen={setOpenPaper}
                                onFind={() => setTab("discover")} />
              )}
            </>
          )}
        </div>
      </main>

      <Copilot goal={goal} enabled={Boolean(stats?.agent.enabled)} open={copilotOpen}
               onClose={() => setCopilotOpen(false)} onChanged={refresh} onOpenPaper={setOpenPaper} />
      <button className="btn copilot-toggle" onClick={() => setCopilotOpen(true)}>Ask the copilot</button>

      {openPaper && (
        <PaperSheet paperId={openPaper} goalId={goalId} onClose={() => setOpenPaper(null)} onChanged={refresh} />
      )}
    </div>
  );
}
