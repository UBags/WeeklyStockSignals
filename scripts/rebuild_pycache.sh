#!/usr/bin/env bash
# Invalidate every stale .pyc and rebuild from current source.
#
# The grep hits on __pycache__/*.pyc are compiled bytecode from BEFORE the
# fixes — they are not a code problem, but they are noise in every grep and
# (rarely, if a source mtime is ever clobbered) can be imported instead of the
# real thing. Blow them away and recompile so the cache matches the tree.
set -euo pipefail
cd "$(dirname "$0")/../../.." 2>/dev/null || cd ~/PycharmProjects/WeeklyStockSignals

echo "== removing __pycache__ =="
find . -path ./.venv -prune -o -type d -name __pycache__ -print0 | xargs -0 rm -rf
find . -path ./.venv -prune -o -type f -name '*.pyc' -print0 | xargs -0 rm -f
echo "   done"

echo "== rebuilding bytecode =="
python -m compileall -q trip_ema/ && echo "   trip_ema/ compiled clean"

echo "== confirming no live code references the dropped tables =="
# SELF-MATCH guard: build the pattern from parts, so this
# script's own source cannot satisfy the grep it runs.
DROPPED="indicator_""state|signal_""events"
if grep -rnE "FROM (${DROPPED})|JOIN (${DROPPED})" --include='*.py' trip_ema/ ; then
  echo "   ^^ STILL PRESENT — investigate"
  exit 1
else
  echo "   clean: no live SQL touches indicator_state / signal_events"
fi
