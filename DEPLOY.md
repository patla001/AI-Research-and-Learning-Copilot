# Deploying to Databricks Apps

Runbook for `dbc-7e085092-52e4.cloud.databricks.com`. The values below are what
was actually deployed.

**Step 1 starts billing** (the Lakebase instance). App compute in step 4 bills
separately. Stop both when you are not using them (see Teardown).

| | |
|---|---|
| Lakebase instance | `copilot-db`: CU_1, PG 16, pgvector 0.8.0, native login on |
| Instance host | `ep-old-rain-d1mk3m3q.database.us-west-2.cloud.databricks.com` |
| Postgres role | `copilot_app`, which owns all 10 tables (created by `scripts/bootstrap_lakebase.py`) |
| Secrets | `database/copilot-lakebase-url`, `database/anthropic-api-key`, `database/openalex-api-key`, each attached as an app resource of the same name |
| App | `research-copilot` (service principal READ on scope `database`) |
| App URL | `https://research-copilot-2808874854650870.aws.databricksapps.com` |
| Deployment | `01f1b058de2c16a7849a0a02e585523e`, SUCCEEDED ("App started successfully") |
| Verification | `test_deployment.py <url> --chat`: **48 passed, 0 failed**, including a real Claude request with verified citations and an open-access full-text import (60,000 chars, 40 content chunks) |
| OpenAlex budget | The free key's `X-RateLimit-Limit` measured **10,000 credits/day**: a search costs 10, a full-text download 100 |

> **Shortcut for steps 1b-3:** once the instance is AVAILABLE,
> `python scripts/bootstrap_lakebase.py --instance copilot-db --write-env` creates the
> role, enables pgvector, applies `sql/01`–`02` as the app role, stores the secret and
> writes `.env`. It connects as you with a short-lived OAuth credential and never prints
> the generated password.

---

## Step 1 - Lakebase instance (⚠️ starts billing)

```bash
export DATABRICKS_CONFIG_PROFILE=DEFAULT
databricks database create-database-instance copilot-db --capacity CU_1 --enable-pg-native-login
databricks database get-database-instance copilot-db      # wait for AVAILABLE
```

`--enable-pg-native-login` is required: `lakebase.py` uses one static-password DSN.

Create the role in **Catalog → Lakebase → copilot-db → Roles & Databases → Add role
→ Password**, name it `copilot_app`, and copy the connection URL.

> ⚠️ Paste the host exactly as shown. A doubled `.database.cloud.databricks.com`
> produces `password authentication failed`, which sends you rotating a password that
> was never wrong.

## Step 2 - Secrets

```bash
python setup_secrets.py                                   # database/copilot-lakebase-url (masked prompt)
databricks secrets put-secret database anthropic-api-key  # from console.anthropic.com
databricks secrets put-secret database openalex-api-key   # free, openalex.org/settings/api
databricks secrets list-secrets database                  # names only
```

## Step 3 - Schema

Use the **Lakebase instance's own query editor**, not a workspace SQL editor or a
`%sql` cell, which target Unity Catalog. Run it as your own identity, which is a
superuser:

1. `sql/00_grant_app_role.sql`, unless `copilot_app` is in `databricks_superuser`
2. `sql/01_schema.sql` (the app also applies this itself on startup)
3. `sql/02_context_chunks.sql`: `CREATE EXTENSION vector` and the HNSW index, which the app role cannot do

## Step 4 - Build, sync, deploy

```bash
cd web && npm install && npm run build && cd ..           # -> static/ (committed; the app has no Node)

APP=research-copilot
WS=/Workspace/Users/epatlan1742@sdsu.edu/$APP
databricks apps create $APP
databricks sync --full . $WS                              # respects .gitignore: no .env, no .venv
databricks apps update $APP --json '{
  "name": "research-copilot",
  "resources": [
    {"name": "copilot-lakebase-url", "secret": {"scope": "database", "key": "copilot-lakebase-url", "permission": "READ"}},
    {"name": "anthropic-api-key",    "secret": {"scope": "database", "key": "anthropic-api-key",    "permission": "READ"}},
    {"name": "openalex-api-key",     "secret": {"scope": "database", "key": "openalex-api-key",     "permission": "READ"}}
  ]}'
databricks apps deploy $APP --source-code-path $WS
```

The resource `name`s must match the `valueFrom` keys in `app.yaml`.

Grant the service principal READ on the scope so the SDK fallback in `lakebase.py`
works if a resource is ever detached:

```bash
SP=$(databricks apps get $APP -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["service_principal_client_id"])')
databricks secrets put-acl database "$SP" READ
```

## Step 5 - Verify

```bash
URL=$(databricks apps get $APP -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["url"])')
python test_deployment.py "$URL"            # data path
python test_deployment.py "$URL" --chat     # plus one real copilot request
```

The URL is behind Databricks OAuth, so the test attaches your CLI credentials.
In a browser, sign in to the workspace first. The user isolation checks run locally
only, because the platform sets `X-Forwarded-Email` and a client cannot spoof it.

## Teardown

```bash
databricks apps delete research-copilot
databricks database delete-database-instance copilot-db    # this is what stops billing
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/healthz` returns 200 with an empty body | Expected on Databricks Apps; the platform answers it. Use `/api` |
| Everything 500s | Secret resource not attached or DSN wrong. The logs say which source `lakebase.py` used, never the value |
| `permission denied for schema public` | `sql/00` not run as a superuser |
| `type "vector" does not exist` / 503 "context_chunks does not exist" | `sql/02` not run |
| `PRINCIPAL_DOES_NOT_EXIST` | SQL ran in a Unity Catalog editor instead of the Lakebase editor |
| Copilot says the API key is not set | `anthropic-api-key` resource missing; the rest of the app is unaffected |
| "Full text is off" in the console | No `openalex-api-key`; content downloads return 401 without one |
| OpenAlex 429 / "credit budget is used up" | Keyless is 1,000 credits/day (100 searches); add the key |
| Console says the session expired | The OAuth session lapsed; reload |
| A serverless job dies with "Python kernel is unresponsive" | psycopg2's bundled OpenSSL; run `notebooks/ingest_embeddings.py --db-driver pg8000` |
