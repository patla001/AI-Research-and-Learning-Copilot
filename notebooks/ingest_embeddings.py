"""
Batch embedding job for the copilot's context index.

Finds every abstract, full text, note and goal with no chunks yet, chunks and
embeds them, and writes context_chunks. Imports and note/goal writes already
embed inline; this job is the catch-up path (after a bulk import, a model
change, or an inline embed that failed).

    python notebooks/ingest_embeddings.py                    # locally, off .env
    python notebooks/ingest_embeddings.py --source abstract  # one source type
    (or a Databricks job task, with --db-driver pg8000)

Preflights the tables and the vector width before any expensive work.
"""

from __future__ import annotations

import argparse
import os
import sys


def _repo_root() -> str:
    """Repo root, even where __file__ is undefined (Databricks serverless exec())."""
    if "--repo-root" in sys.argv:
        return sys.argv[sys.argv.index("--repo-root") + 1]
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        here = os.getcwd()
        while not os.path.isfile(os.path.join(here, "context_pipeline.py")):
            parent = os.path.dirname(here)
            if parent == here:
                return os.getcwd()
            here = parent
        return here


ROOT = _repo_root()
sys.path.insert(0, ROOT)

if "--db-driver" in sys.argv:
    # Must be set before lakebase is imported; it picks the driver at import.
    os.environ["COPILOT_DB_DRIVER"] = sys.argv[sys.argv.index("--db-driver") + 1]

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:  # pragma: no cover
    pass

import context_pipeline  # noqa: E402
import embeddings  # noqa: E402
import lakebase  # noqa: E402


def preflight() -> int:
    expected = embeddings.expected_dims()
    if expected is None:
        raise RuntimeError(f"Unknown model {embeddings.EMBED_MODEL!r}; add it to embeddings.MODEL_DIMS.")
    if not context_pipeline.chunks_table_exists():
        raise RuntimeError("context_chunks does not exist. Run sql/01_schema.sql and "
                           "sql/02_context_chunks.sql in the Lakebase SQL editor first.")
    rows = lakebase.run_query(
        """
        SELECT a.atttypmod AS dims FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'context_chunks' AND a.attname = 'embedding'
        """
    )
    actual = rows[0]["dims"] if rows else None
    if actual != expected:
        raise RuntimeError(f"Dimension mismatch: context_chunks.embedding is VECTOR({actual}) but "
                           f"{embeddings.EMBED_MODEL} produces {expected}.")
    return expected


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=context_pipeline.SOURCE_TYPES, action="append",
                        help="Only these source types (repeatable). Default: all.")
    parser.add_argument("--limit", type=int, default=None, help="Max sources per type this run.")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--db-driver", choices=["psycopg2", "pg8000"], default=None)
    args = parser.parse_args(argv)

    print(f"model:     {embeddings.EMBED_MODEL}")
    print(f"chunking:  size={embeddings.CHUNK_SIZE} overlap={embeddings.CHUNK_OVERLAP}")
    print(f"driver:    {lakebase.DRIVER}")
    dims = preflight()
    print(f"preflight: ok, VECTOR({dims})")
    print(f"before:    {context_pipeline.backlog()}")

    result = context_pipeline.embed_pending(
        limit=args.limit, source_types=tuple(args.source or context_pipeline.SOURCE_TYPES), log=print,
    )
    print(f"embedded:  {result['sources']} source(s) -> {result['chunks']} chunk(s), "
          f"{result['written']} written; by source {result['by_source']}")
    after = context_pipeline.backlog()
    print(f"after:     {after}")
    if any(after["pending"].values()) and args.limit is None:
        print("WARNING: sources still pending - check the log above for failed batches")
    return 0


if __name__ == "__main__":
    # Not `raise SystemExit(main())`: under Databricks serverless even
    # SystemExit(0) tears down the kernel and loses the job's logs.
    _rc = main()
    if _rc:
        raise SystemExit(_rc)
