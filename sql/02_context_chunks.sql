-- RUN IN THE LAKEBASE SQL EDITOR, as a superuser role. Run 01_schema.sql first.
--
-- The retrieval index for context engineering. Abstracts, open-access paper
-- content, notes and learning goals are all chunked and embedded into this one
-- table, so a single ORDER BY embedding <=> query can pull evidence from many
-- papers and the learner's own notes at once - instead of sending a whole
-- collection to the model.
--
-- The app does NOT create this table itself: CREATE EXTENSION and the HNSW
-- build are privileged one-time DDL.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS context_chunks (
    -- sha256("<source_type>:<source_id>:<chunk_index>")[:32]. Derived from
    -- position, so re-running the embed job collides instead of duplicating.
    id           TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL CHECK (source_type IN ('abstract', 'content', 'note', 'goal')),

    -- Exactly one owner, each with ON DELETE CASCADE, so deleting a paper, note
    -- or goal takes its vectors with it. A single polymorphic "source_id TEXT"
    -- column could not carry a foreign key, and orphans would pile up.
    paper_id     TEXT   REFERENCES papers (id)         ON DELETE CASCADE,
    note_id      BIGINT REFERENCES notes (id)          ON DELETE CASCADE,
    goal_id      BIGINT REFERENCES learning_goals (id) ON DELETE CASCADE,

    -- Notes and goals are private. Denormalized here so retrieval can filter by
    -- owner without joining; NULL for paper text, which every user can see.
    user_id      BIGINT REFERENCES users (id) ON DELETE CASCADE,

    chunk_index  INT  NOT NULL,
    chunk_text   TEXT NOT NULL,
    -- VECTOR(384) matches BAAI/bge-small-en-v1.5 (and all-MiniLM-L6-v2). Change
    -- both together; notebooks/ingest_embeddings.py preflights the width.
    embedding    VECTOR(384) NOT NULL,
    model_name   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT context_chunks_owner CHECK (
        (source_type IN ('abstract', 'content') AND paper_id IS NOT NULL AND note_id IS NULL AND goal_id IS NULL)
     OR (source_type = 'note' AND note_id IS NOT NULL AND user_id IS NOT NULL AND goal_id IS NULL)
     OR (source_type = 'goal' AND goal_id IS NOT NULL AND user_id IS NOT NULL AND note_id IS NULL AND paper_id IS NULL)
    )
);

-- Cosine ops, matching the <=> operator in retrieval.py. A mismatched operator
-- class makes the planner silently ignore the index.
CREATE INDEX IF NOT EXISTS idx_context_chunks_embedding
    ON context_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_context_chunks_paper ON context_chunks (paper_id, source_type);
CREATE INDEX IF NOT EXISTS idx_context_chunks_note  ON context_chunks (note_id);
CREATE INDEX IF NOT EXISTS idx_context_chunks_goal  ON context_chunks (goal_id);
CREATE INDEX IF NOT EXISTS idx_context_chunks_user  ON context_chunks (user_id, source_type);

SELECT column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_name = 'context_chunks'
ORDER BY ordinal_position;
