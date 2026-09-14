"""
Clear one learner's own records, e.g. before recording a demo.

    python scripts/reset_user_data.py --email you@example.edu          # dry run: counts only
    python scripts/reset_user_data.py --email you@example.edu --yes    # delete

Deletes that user's learning goals, collections (and their collection_papers),
reading progress and notes - and, through the ON DELETE CASCADE foreign keys,
the context_chunks embedded from those notes and goals.

Keeps the users row and everything shared: papers, authors, paper_authors and
the abstract/full-text chunks. Those are public OpenAlex records other learners
may have saved, and re-importing them would cost OpenAlex credits.

Connects through lakebase.py, so it targets whatever LAKEBASE_URL (.env) points
at - the host is printed first so you can check before passing --yes.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import lakebase  # noqa: E402

COUNTS = """
    SELECT
      (SELECT COUNT(*) FROM learning_goals   WHERE user_id = %(u)s) AS learning_goals,
      (SELECT COUNT(*) FROM collections      WHERE user_id = %(u)s) AS collections,
      (SELECT COUNT(*) FROM collection_papers cp JOIN collections c ON c.id = cp.collection_id
                                             WHERE c.user_id = %(u)s) AS collection_papers,
      (SELECT COUNT(*) FROM reading_progress WHERE user_id = %(u)s) AS reading_progress,
      (SELECT COUNT(*) FROM notes            WHERE user_id = %(u)s) AS notes,
      (SELECT COUNT(*) FROM context_chunks   WHERE user_id = %(u)s) AS note_and_goal_chunks
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = parser.parse_args()

    url = os.environ.get("LAKEBASE_URL") or ""
    print(f"database host: {urlparse(url).hostname or '(from secret scope)'}")

    rows = lakebase.run_query("SELECT id FROM users WHERE email = %s", (args.email.strip().lower(),))
    if not rows:
        print(f"no user {args.email!r} - nothing to do")
        return 0
    user_id = rows[0]["id"]
    before = lakebase.run_query(COUNTS, {"u": user_id})[0]
    print(f"user {args.email} (id {user_id}): {dict(before)}")

    if not args.yes:
        print("dry run - re-run with --yes to delete these rows")
        return 0

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            # Order matters only for readability; every child row cascades.
            for table in ("notes", "reading_progress", "collections", "learning_goals"):
                cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
                print(f"  deleted {cur.rowcount} from {table}")
        conn.commit()

    after = lakebase.run_query(COUNTS, {"u": user_id})[0]
    print(f"after: {dict(after)}")
    return 0 if not any(after.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
