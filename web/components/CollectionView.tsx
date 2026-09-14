"use client";

import type { CollectionDetail } from "@/lib/types";

interface Props {
  collection: CollectionDetail | null;
  onRemove: (paperId: string) => void;
  onOpen: (paperId: string) => void;
  onFind: () => void;
}

export default function CollectionView({ collection, onRemove, onOpen, onFind }: Props) {
  if (!collection || collection.papers.length === 0) {
    return (
      <div className="empty">
        <h2>Nothing saved for this goal yet</h2>
        <p>Papers you save from search, or that the copilot adds for you, collect here. The copilot answers questions
          from these papers and your notes on them.</p>
        <button className="btn" onClick={onFind}>Find papers</button>
      </div>
    );
  }

  return (
    <ul className="paper-list" aria-label={collection.name}>
      {collection.papers.map((paper) => (
        <li key={paper.id} className="paper-row">
          <div>
            <h3 className="paper-title"><button onClick={() => onOpen(paper.id)}>{paper.title}</button></h3>
            <div className="paper-byline">
              {paper.authors.slice(0, 3).join(", ")}{paper.authors.length > 3 ? " et al." : ""}
              {paper.publication_year ? `, ${paper.publication_year}` : ""}
            </div>
            {paper.reason && (
              <div className="paper-reason">
                {paper.added_by === "agent" ? "Added by the copilot: " : ""}{paper.reason}
              </div>
            )}
          </div>
          <div className="paper-side">
            {paper.status && <span className="badge">{paper.status === "completed" ? "Read" : paper.status}</span>}
            {paper.content_stored && <span className="badge badge-oa">Full text</span>}
            <button className="link" style={{ fontSize: "0.85rem" }} onClick={() => onRemove(paper.id)}>Remove</button>
          </div>
        </li>
      ))}
    </ul>
  );
}
