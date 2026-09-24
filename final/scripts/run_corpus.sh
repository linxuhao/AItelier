#!/bin/bash
# usage: run_corpus.sh <log>
# Runs final/corpus_impact.py on the host python (no container, no engine)
# against the git-archive extraction of the game repo's master playtest/.
LOG="$1"
TREE=/home/linxuhao/.AItelier/worktrees-scratch/onereading1
T=/home/linxuhao/.AItelier/worktrees-scratch/onereading1-tools
{
  echo "CMD: PYTHONPATH=$TREE python3 $TREE/final/corpus_impact.py $T/harness_base.py $T/harness_cand.py $T/corpus"
  echo "TREE: $(git -C "$TREE" rev-parse HEAD)  PYTHON: $(python3 --version 2>&1)"
  echo "CORPUS: wuxia-commercial-batch-next master $(cat $T/corpus/MASTER_SHA), extracted with: git -C ~/.AItelier/projects/wuxia-commercial-batch-next archive master playtest | tar -x -C $T/corpus"
  echo "HARNESS_BASE_SHA1: $(sha1sum $T/harness_base.py | cut -d' ' -f1)  HARNESS_CAND_SHA1: $(sha1sum $T/harness_cand.py | cut -d' ' -f1)"
  echo "DATE: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$LOG"
PYTHONPATH="$TREE" PYTHONDONTWRITEBYTECODE=1 python3 "$TREE/final/corpus_impact.py" \
  "$T/harness_base.py" "$T/harness_cand.py" "$T/corpus" >> "$LOG" 2>&1
RC=$?
echo "BARE_RC=$RC" >> "$LOG"
exit $RC
