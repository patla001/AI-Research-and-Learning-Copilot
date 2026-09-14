#!/usr/bin/env bash
# Build a slim zip for the assignment upload: the source, without generated or
# binary files that blow through the submission token limit.
#
#   scripts/make_submission.sh                 # -> ../AI-Research-and-Learning-Copilot-submission.zip
#   scripts/make_submission.sh /path/out.zip
#
# Left out (all still in the GitHub repo):
#   static/                  the built Next.js console - minified JS/CSS/fonts, ~168K tokens.
#                            Rebuild from web/ with `cd web && npm install && npm run build`.
#   docs/demo.gif            1.4 MB binary; the README shows it from GitHub instead.
#   web/package-lock.json    npm lockfile.
#   tests/fixtures/          saved OpenAlex response; its one test skips when absent.
#
# Copies tracked files only (git ls-files), so .env, .venv and node_modules can
# never end up in the upload. Uses the working tree, so uncommitted edits count.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="${1:-$ROOT/../AI-Research-and-Learning-Copilot-submission.zip}"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
EXCLUDE='^(static/|docs/demo\.gif$|web/package-lock\.json$|tests/fixtures/)'

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/AI-Research-and-Learning-Copilot"
mkdir -p "$STAGE"

git ls-files | grep -Ev "$EXCLUDE" | while IFS= read -r f; do
  [ -f "$f" ] || continue
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$f" "$STAGE/$f"
done

rm -f "$OUT"
(cd "$WORK" && zip -qr "$OUT" "$(basename "$STAGE")")

files=$(find "$STAGE" -type f | wc -l | tr -d ' ')
bytes=$(find "$STAGE" -type f -exec cat {} + | wc -c | tr -d ' ')
echo "wrote $OUT"
echo "  $files files, $((bytes / 1024)) KB of text, ~$((bytes / 4)) tokens (4 chars/token estimate)"
echo "  left out: static/, docs/demo.gif, web/package-lock.json, tests/fixtures/"
