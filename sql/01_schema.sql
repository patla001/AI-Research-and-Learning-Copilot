-- RUN IN THE LAKEBASE SQL EDITOR (from the database instance page).
--
-- The nine application tables. Order matters: every table is created after the
-- tables its foreign keys reference. Every statement is IF NOT EXISTS, so the
-- file is safe to re-run.
--
-- Id strategy:
--   * Things the USER creates (users, goals, collections, notes, progress) get
--     BIGSERIAL ids - there is no natural key.
--   * Things that come from OPENALEX (papers, authors) keep the OpenAlex short
--     id ("W4389984066", "A5102001778") as the primary key. Those ids are stable
--     and global, so re-importing a paper upserts instead of duplicating, and the
--     agent can cite a paper by the same id the API uses.

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
-- On Databricks Apps the identity comes from the X-Forwarded-Email header the
-- platform injects; rows are created on first request.
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- learning_goals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_goals (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    description  TEXT,
    -- Drives plan ordering: beginners get surveys first, advanced learners go
    -- straight to the primary literature.
    level        TEXT NOT NULL DEFAULT 'intermediate'
                 CHECK (level IN ('beginner', 'intermediate', 'advanced')),
    target_date  DATE,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'completed', 'archived')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_learning_goals_user ON learning_goals (user_id, status);

-- ---------------------------------------------------------------------------
-- papers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS papers (
    id                TEXT PRIMARY KEY,          -- OpenAlex short id, "W4389984066"
    doi               TEXT,
    title             TEXT NOT NULL,
    -- Rebuilt from OpenAlex's abstract_inverted_index; NULL when OpenAlex has none.
    abstract          TEXT,
    publication_year  INT,
    publication_date  DATE,
    venue             TEXT,
    work_type         TEXT,                      -- article | preprint | review | ...
    language          TEXT,
    cited_by_count    INT NOT NULL DEFAULT 0,
    is_oa             BOOLEAN NOT NULL DEFAULT false,
    oa_url            TEXT,
    primary_topic     TEXT,
    topics            JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- OpenAlex ids this paper cites. Used for plan ordering: when two papers in
    -- a collection cite each other, the cited one is read first.
    referenced_works  TEXT[] NOT NULL DEFAULT '{}',
    related_works     TEXT[] NOT NULL DEFAULT '{}',
    -- Open-access full text (GROBID TEI -> plain text) when OpenAlex has it and
    -- a key is configured. NULL means "abstract only".
    has_content       BOOLEAN NOT NULL DEFAULT false,
    content_text      TEXT,
    content_fetched_at TIMESTAMPTZ,
    -- sha256 of abstract / content_text. A change invalidates that source's
    -- chunks so retrieval never serves vectors of text that is gone.
    abstract_hash     TEXT,
    content_hash      TEXT,
    payload           JSONB NOT NULL,            -- raw OpenAlex work, for provenance
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers (publication_year);
CREATE INDEX IF NOT EXISTS idx_papers_referenced ON papers USING gin (referenced_works);

-- ---------------------------------------------------------------------------
-- authors + paper_authors
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS authors (
    id            TEXT PRIMARY KEY,              -- OpenAlex short id, "A5102001778"
    display_name  TEXT NOT NULL,
    orcid         TEXT,
    -- Institutions as seen on this author's most recently imported authorship.
    institutions  JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id          TEXT NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    author_id         TEXT NOT NULL REFERENCES authors (id) ON DELETE CASCADE,
    author_position   TEXT,                      -- first | middle | last
    position_index    INT NOT NULL,              -- byline order, 0-based
    is_corresponding  BOOLEAN NOT NULL DEFAULT false,
    -- Affiliation is a property of the authorship, not the author: people move.
    institutions      JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (paper_id, author_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_authors_author ON paper_authors (author_id);

-- ---------------------------------------------------------------------------
-- collections + collection_papers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- A collection usually serves one goal; deleting the goal keeps the papers.
    goal_id      BIGINT REFERENCES learning_goals (id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS collection_papers (
    collection_id  BIGINT NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    paper_id       TEXT   NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    added_by       TEXT   NOT NULL DEFAULT 'user' CHECK (added_by IN ('user', 'agent')),
    -- Why it was added. The agent must give one, so the learner can audit it.
    reason         TEXT,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_papers_paper ON collection_papers (paper_id);

-- ---------------------------------------------------------------------------
-- reading_progress
-- ---------------------------------------------------------------------------
-- One row per (user, paper, goal). It doubles as the persisted reading plan:
-- plan_position is the sequence, rationale says why the paper sits there.
CREATE TABLE IF NOT EXISTS reading_progress (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    paper_id       TEXT   NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    goal_id        BIGINT REFERENCES learning_goals (id) ON DELETE CASCADE,
    status         TEXT   NOT NULL DEFAULT 'planned'
                   CHECK (status IN ('planned', 'reading', 'completed', 'skipped')),
    plan_position  INT,
    plan_stage     TEXT,                        -- orientation | foundations | core | frontier
    rationale      TEXT,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT (PG15+) so progress with no goal is still one row per
    -- paper; a plain UNIQUE would let NULL goal_ids duplicate freely.
    UNIQUE NULLS NOT DISTINCT (user_id, paper_id, goal_id)
);
CREATE INDEX IF NOT EXISTS idx_reading_progress_plan
    ON reading_progress (user_id, goal_id, plan_position);

-- ---------------------------------------------------------------------------
-- notes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    paper_id    TEXT   REFERENCES papers (id) ON DELETE CASCADE,
    goal_id     BIGINT REFERENCES learning_goals (id) ON DELETE SET NULL,
    body        TEXT   NOT NULL,
    body_hash   TEXT   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notes_user_paper ON notes (user_id, paper_id);

SELECT table_name, COUNT(*) AS columns
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('users', 'learning_goals', 'papers', 'authors', 'paper_authors',
                     'collections', 'collection_papers', 'reading_progress', 'notes')
GROUP BY table_name
ORDER BY table_name;
