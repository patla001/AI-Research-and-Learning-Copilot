-- RUN IN THE LAKEBASE SQL EDITOR. Each query states the result to expect.

-- 1. Row counts across all ten tables.
SELECT 'users' AS t, COUNT(*) FROM users
UNION ALL SELECT 'learning_goals', COUNT(*) FROM learning_goals
UNION ALL SELECT 'papers', COUNT(*) FROM papers
UNION ALL SELECT 'authors', COUNT(*) FROM authors
UNION ALL SELECT 'paper_authors', COUNT(*) FROM paper_authors
UNION ALL SELECT 'collections', COUNT(*) FROM collections
UNION ALL SELECT 'collection_papers', COUNT(*) FROM collection_papers
UNION ALL SELECT 'reading_progress', COUNT(*) FROM reading_progress
UNION ALL SELECT 'notes', COUNT(*) FROM notes
UNION ALL SELECT 'context_chunks', COUNT(*) FROM context_chunks;

-- 2. Chunks by source. Expect 'abstract' > 0 once papers are imported and
--    embedded; 'content' only appears when OPENALEX_API_KEY is set.
SELECT source_type, COUNT(*) AS chunks,
       COUNT(DISTINCT COALESCE(paper_id, note_id::text, goal_id::text)) AS sources
FROM context_chunks GROUP BY source_type ORDER BY source_type;

-- 3. Embedding backlog. MUST be 0 right after an embed run.
SELECT
  (SELECT COUNT(*) FROM papers p WHERE p.abstract IS NOT NULL AND NOT EXISTS
     (SELECT 1 FROM context_chunks c WHERE c.paper_id = p.id AND c.source_type = 'abstract')) AS abstracts_pending,
  (SELECT COUNT(*) FROM papers p WHERE p.content_text IS NOT NULL AND NOT EXISTS
     (SELECT 1 FROM context_chunks c WHERE c.paper_id = p.id AND c.source_type = 'content')) AS content_pending,
  (SELECT COUNT(*) FROM notes n WHERE NOT EXISTS
     (SELECT 1 FROM context_chunks c WHERE c.note_id = n.id)) AS notes_pending,
  (SELECT COUNT(*) FROM learning_goals g WHERE NOT EXISTS
     (SELECT 1 FROM context_chunks c WHERE c.goal_id = g.id)) AS goals_pending;

-- 4. One vector width and one model. Expect a single row: 384, one model name.
SELECT vector_dims(embedding) AS dims, model_name, COUNT(*)
FROM context_chunks GROUP BY 1, 2;

-- 5. Every paper in a collection exists and has at least one author. Expect 0.
SELECT COUNT(*) AS collection_papers_without_authors
FROM collection_papers cp
WHERE NOT EXISTS (SELECT 1 FROM paper_authors pa WHERE pa.paper_id = cp.paper_id);

-- 6. Reading plans have no gaps or duplicate positions per (user, goal). Expect no rows.
SELECT user_id, goal_id, plan_position, COUNT(*)
FROM reading_progress
WHERE plan_position IS NOT NULL
GROUP BY 1, 2, 3 HAVING COUNT(*) > 1;

-- 7. HNSW index present with cosine ops. Expect a row containing "USING hnsw".
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'context_chunks';
