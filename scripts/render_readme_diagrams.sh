#!/usr/bin/env bash
#
# Render docs/diagrams/*.mmd -> docs/images/diagrams/*.svg for the README.
#
# Why pre-rendered images instead of ```mermaid blocks: GitHub renders each
# Mermaid block in its own iframe, and on a page with many diagrams some of
# them intermittently fail ("Unable to render rich display"). Static SVGs
# render everywhere, including the GitHub mobile app.
#
# Edit the .mmd source, then re-run this script and commit both.
#
# Requires Node.js. Uses @mermaid-js/mermaid-cli via npx and a local Chrome
# or Chromium (set PUPPETEER_EXECUTABLE_PATH if it isn't auto-detected).
#
# Usage (from repo root):
#   ./scripts/render_readme_diagrams.sh            # all diagrams
#   ./scripts/render_readme_diagrams.sh 06-live-cycle   # one diagram

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/docs/diagrams"
OUT="$ROOT/docs/images/diagrams"
mkdir -p "$OUT"

PUPPETEER_CFG="$(mktemp)"
trap 'rm -f "$PUPPETEER_CFG"' EXIT
CHROME="${PUPPETEER_EXECUTABLE_PATH:-$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)}"
if [[ -n "$CHROME" ]]; then
    printf '{"executablePath":"%s","args":["--no-sandbox"]}\n' "$CHROME" > "$PUPPETEER_CFG"
else
    printf '{"args":["--no-sandbox"]}\n' > "$PUPPETEER_CFG"
fi

shopt -s nullglob
if [[ $# -gt 0 ]]; then
    files=()
    for name in "$@"; do files+=("$SRC/${name%.mmd}.mmd"); done
else
    files=("$SRC"/*.mmd)
fi

for f in "${files[@]}"; do
    base="$(basename "$f" .mmd)"
    # White background so the diagrams stay legible in GitHub dark mode.
    npx --yes -p @mermaid-js/mermaid-cli@11 mmdc \
        -p "$PUPPETEER_CFG" -i "$f" -o "$OUT/$base.svg" -b white -q
    echo "rendered $base.svg"
done
