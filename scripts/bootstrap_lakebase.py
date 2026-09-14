"""
One-command Lakebase bootstrap for the copilot.

Replaces DEPLOY.md steps 1b-3 (role in the UI, secret, schema in the SQL
editor) with something repeatable:

    python scripts/bootstrap_lakebase.py --instance copilot-db --write-env

  1. Connects to the instance AS YOU, with a short-lived OAuth credential from
     `generate_database_credential` - your identity is in databricks_superuser.
  2. Creates the `copilot_app` password role (random 32-byte password), grants
     it CREATE on schema public, and creates the pgvector extension - the one
     step that genuinely needs a superuser.
  3. Reconnects AS copilot_app and applies sql/01 and sql/02, so the app role
     OWNS every table. No per-table GRANTs to keep in sync, and the app's own
     startup schema check (app.ensure_schema) runs as the owner.
  4. Stores the DSN as the `database/copilot-lakebase-url` secret, and with
     --write-env also into the gitignored .env for local runs.

The password is generated here and never printed. If the role already exists
the script refuses to guess its password: pass --rotate-password to set a new
one (anything using the old DSN must then be updated - the secret is).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import uuid
from urllib.parse import quote

import psycopg2
from psycopg2 import sql as pgsql

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE = "databricks_postgres"


def statements(path: str) -> list[str]:
    """Split a schema file into statements. Comments go first: one contains a ';'."""
    with open(path) as handle:
        text = "\n".join(line.split("--", 1)[0] for line in handle)
    return [s.strip() for s in text.split(";") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", default="copilot-db")
    parser.add_argument("--role", default="copilot_app")
    parser.add_argument("--scope", default="database")
    parser.add_argument("--key", default="copilot-lakebase-url")
    parser.add_argument("--rotate-password", action="store_true")
    parser.add_argument("--write-env", action="store_true", help="also write LAKEBASE_URL into ./.env")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    instance = w.database.get_database_instance(args.instance)
    host = instance.read_write_dns
    print(f"instance:  {args.instance} ({instance.state}), host {host}")
    print(f"as user:   {me}")
    if not host:
        print("The instance has no read/write DNS yet - wait for AVAILABLE and re-run.")
        return 1

    credential = w.database.generate_database_credential(
        instance_names=[args.instance], request_id=str(uuid.uuid4())
    )

    # -- 1-2: superuser work ------------------------------------------------
    admin = psycopg2.connect(host=host, port=5432, dbname=DATABASE, user=me,
                             password=credential.token, sslmode="require")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (args.role,))
        exists = cur.fetchone() is not None
        if exists and not args.rotate_password:
            print(f"role {args.role!r} already exists and its password is not recoverable.\n"
                  "Re-run with --rotate-password to set a new one and update the secret.")
            return 1

        password = secrets.token_urlsafe(32)
        verb = "ALTER ROLE {} WITH LOGIN PASSWORD %s" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD %s"
        cur.execute(pgsql.SQL(verb).format(pgsql.Identifier(args.role)), (password,))
        print(f"role:      {args.role} {'password rotated' if exists else 'created'}")

        cur.execute(pgsql.SQL("GRANT CREATE, USAGE ON SCHEMA public TO {}").format(pgsql.Identifier(args.role)))
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        print(f"pgvector:  {cur.fetchone()[0]}")
    admin.close()

    dsn = f"postgresql://{args.role}:{quote(password, safe='')}@{host}:5432/{DATABASE}?sslmode=require"

    # -- 3: schema, owned by the app role ------------------------------------
    app = psycopg2.connect(dsn)
    with app.cursor() as cur:
        for path in ("sql/01_schema.sql", "sql/02_context_chunks.sql"):
            applied = 0
            for statement in statements(os.path.join(ROOT, path)):
                upper = statement.upper()
                # SELECTs are the files' human-readable verification output;
                # the extension already exists and needs a superuser anyway.
                if upper.startswith("SELECT") or upper.startswith("CREATE EXTENSION"):
                    continue
                cur.execute(statement)
                applied += 1
            print(f"schema:    {path} ({applied} statements)")
        cur.execute("""
            SELECT COUNT(*) FROM pg_tables
            WHERE schemaname = 'public' AND tableowner = %s""", (args.role,))
        tables = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_context_chunks_embedding'")
        hnsw = cur.fetchone()[0]
    app.commit()
    app.close()
    print(f"verified:  {tables} tables owned by {args.role}, HNSW index {'present' if hnsw else 'MISSING'}")

    # -- 4: secret (+ optional .env) -------------------------------------------
    if args.scope not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(scope=args.scope)
    w.secrets.put_secret(scope=args.scope, key=args.key, string_value=dsn)
    print(f"secret:    {args.scope}/{args.key} stored")

    if args.write_env:
        env_path = os.path.join(ROOT, ".env")
        lines = []
        if os.path.exists(env_path):
            with open(env_path) as handle:
                lines = [l for l in handle.read().splitlines() if not l.startswith("LAKEBASE_URL=")]
        lines.insert(0, f"LAKEBASE_URL={dsn}")
        with open(env_path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(env_path, 0o600)
        print("env:       LAKEBASE_URL written to .env (gitignored, mode 600)")

    return 0 if tables >= 10 and hnsw else 1


if __name__ == "__main__":
    sys.exit(main())
