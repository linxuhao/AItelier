#!/bin/bash
# usage: run_review_inprocess.sh battery <base_harness.py> <tree> <log> (candidate = harness/h_5fbe7c24.py)
#        run_review_inprocess.sh dispatch <harness.py> <tree> <log>
# Runs the r1 review's in-process script (copied verbatim into final/review_scripts/)
# on the host python with PYTHONPATH=<tree> (aitelier.strict_yaml, read_spec).
KIND="$1"; H="$2"; TREE="$3"; LOG="$4"; T=/home/linxuhao/.AItelier/worktrees-scratch/onereading2-tools
S=/home/linxuhao/.AItelier/worktrees-scratch/onereading1/final/review_scripts/$KIND.py
if [ "$KIND" = battery ]; then ARGS=("$H" "$T/harness/h_5fbe7c24.py"); else ARGS=("$H" "$TREE"); fi
{
  echo "CMD: PYTHONPATH=$TREE python3 $S ${ARGS[*]}"
  echo "SCRIPT_SHA1: $(sha1sum "$S" | cut -d" " -f1)  HARNESS_SHA1: $(sha1sum "$H" | cut -d" " -f1)  TREE: $(git -C "$TREE" rev-parse HEAD)"
  echo "DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$LOG"
PYTHONPATH="$TREE" PYTHONDONTWRITEBYTECODE=1 python3 "$S" "${ARGS[@]}" >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
exit $RC
