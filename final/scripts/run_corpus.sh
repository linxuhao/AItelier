#!/bin/bash
# usage: run_corpus.sh <base_harness.py> <log>
# Runs final/corpus_impact.py on the host python (no container, no engine)
# against a git-archive extraction of the game repo's playtest/ at 43aff480.
# The candidate harness and the contract reader come from the candidate tree.
BASE="$1"; LOG="$2"
TREE=/home/linxuhao/.AItelier/worktrees-scratch/onereading1
T=/home/linxuhao/.AItelier/worktrees-scratch/onereading2-tools
CAND="$T/harness/h_5fbe7c24.py"
{
  echo "CMD: PYTHONPATH=$TREE python3 $TREE/final/corpus_impact.py $BASE $CAND $T/corpus"
  echo "TREE: $(git -C "$TREE" rev-parse HEAD)  PYTHON: $(python3 --version 2>&1)"
  echo "CORPUS: wuxia-commercial-batch-next $(cat $T/corpus/SHA), extracted with: git -C ~/.AItelier/projects/wuxia-commercial-batch-next archive $(cat $T/corpus/SHA) playtest tools | tar -x -C $T/corpus"
  echo "HARNESS_BASE_SHA1: $(sha1sum "$BASE" | cut -d' ' -f1)  HARNESS_CAND_SHA1: $(sha1sum "$CAND" | cut -d' ' -f1)"
  echo "DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$LOG"
PYTHONPATH="$TREE" PYTHONDONTWRITEBYTECODE=1 python3 "$TREE/final/corpus_impact.py" \
  "$BASE" "$CAND" "$T/corpus" >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
exit $RC
