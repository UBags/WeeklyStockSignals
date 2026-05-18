#!/usr/bin/env bash
#
# Pre-deletion audit for the trip_ema/transports/ dead island.
# Run from the repo root. Exits 0 if the deletion is safe; non-zero
# if any reference to the doomed directories is found anywhere in
# the codebase or tests.
#
# Usage: ./scripts/audit_dead_island.sh
#
# Output: a per-directory section showing every match (should be empty
# for each doomed dir), followed by a final SAFE / UNSAFE verdict.

set -u  # not -e — we want to keep going past empty greps

DOOMED_DIRS=(
    api cache config domain engine indicators notifications
    observability persistence registry scheduler
)

# These are full paths (under trip_ema/transports/) that we plan to
# delete in their entirety.
DOOMED_PATHS=(
    "trip_ema/transports/app.py"
    "trip_ema/transports/transports"   # the nested duplicate
)
for d in "${DOOMED_DIRS[@]}"; do
    DOOMED_PATHS+=("trip_ema/transports/$d")
done

# Search scope: all .py files in the repo, EXCLUDING the doomed
# paths themselves (their internal cross-references don't count —
# they're going away together).
EXCLUDES=()
for p in "${DOOMED_PATHS[@]}"; do
    EXCLUDES+=(--exclude-dir="$(basename "$p")")
done

found_any=0

echo "=========================================="
echo "Dead island audit — checking 13 doomed paths"
echo "=========================================="

# Build a list of import patterns to check for each doomed thing.
# We check for: absolute imports (trip_ema.transports.X), one-dot
# sibling-from-inside-transports (..X — but only from siblings, which
# we exclude), and three-or-more-dot imports referencing them.

for d in "${DOOMED_DIRS[@]}"; do
    echo ""
    echo "--- trip_ema.transports.$d ---"

    # Pattern 1: absolute imports
    matches=$(grep -rn \
        -e "from trip_ema\.transports\.$d" \
        -e "import trip_ema\.transports\.$d" \
        trip_ema/ tests/ \
        --include='*.py' 2>/dev/null \
        | grep -v __pycache__ \
        | grep -v "^trip_ema/transports/")

    # Pattern 2: relative imports that resolve into this directory.
    # `from .transports.X` from inside trip_ema/ root.
    # `from ..X` from a SIBLING under transports/ — but siblings are
    # also doomed, so we don't count those. The grep below intentionally
    # only flags external-to-transports/ relative imports.
    rel_matches=$(grep -rn \
        -e "from \.transports\.$d" \
        trip_ema/ \
        --include='*.py' 2>/dev/null \
        | grep -v __pycache__ \
        | grep -v "^trip_ema/transports/")

    combined="${matches}${rel_matches}"
    if [ -n "$combined" ]; then
        echo "$combined"
        found_any=1
    else
        echo "  (no external importers — safe to delete)"
    fi
done

echo ""
echo "--- trip_ema.transports.app (orphan entry point) ---"
matches=$(grep -rn \
    -e "trip_ema\.transports\.app" \
    -e "from \.transports import app" \
    -e "from \.transports\.app" \
    trip_ema/ tests/ \
    --include='*.py' 2>/dev/null \
    | grep -v __pycache__ \
    | grep -v "^trip_ema/transports/")
if [ -n "$matches" ]; then
    echo "$matches"
    found_any=1
else
    echo "  (no importers — safe to delete)"
fi

echo ""
echo "--- trip_ema/transports/transports/ (nested duplicate) ---"
matches=$(grep -rn \
    -e "trip_ema\.transports\.transports" \
    -e "from \.transports\.transports" \
    trip_ema/ tests/ \
    --include='*.py' 2>/dev/null \
    | grep -v __pycache__ \
    | grep -v "^trip_ema/transports/")
if [ -n "$matches" ]; then
    echo "$matches"
    found_any=1
else
    echo "  (no importers — safe to delete)"
fi

echo ""
echo "=========================================="
if [ $found_any -eq 0 ]; then
    echo "VERDICT: SAFE — no external references found."
    echo "Stage 3 deletion can proceed."
    exit 0
else
    echo "VERDICT: UNSAFE — references found above."
    echo "DO NOT proceed with deletion until each reference is"
    echo "explained or removed."
    exit 1
fi