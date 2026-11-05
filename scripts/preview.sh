#!/usr/bin/env bash
# Local preview with the inline-suggestion layer wired in.
# PORT (default 1313) and SUGGEST_REPO (default levduc/suggest-lab) override.
set -euo pipefail
cd "$(dirname "$0")/.."
port="${PORT:-1313}"
repo="${SUGGEST_REPO:-levduc/suggest-lab}"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
# Posts read the repo from a Hugo param, static pages from the injector: set both.
HUGO_PARAMS_SUGGESTREPO="$repo" hugo --quiet -d "$out" --baseURL "http://localhost:${port}/"
python3 -I .github/scripts/inject_suggest.py --public "$out" --static static \
  --repo "$repo" --base-url "http://localhost:${port}/"
python3 -m http.server "$port" --bind 127.0.0.1 -d "$out"
