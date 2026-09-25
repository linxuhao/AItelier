#!/bin/bash
# usage (inside a throwaway container): mut_loop.sh <sha> <outdir> <mutant...>
# One `git archive` copy of <sha> per mutant under /tmp/mut, the mutant applied
# by mutate.py, then the gate suites run against that copy. Each log ends with
# BARE_RC (pytest's exit code) and IGNITION_BYTES (how often the mutated code ran).
SHA=$1; OUT=$2; shift 2
SRC=/home/linxuhao/.AItelier/worktrees-scratch/gatenotrun2
PROBES=/home/linxuhao/.AItelier/worktrees-scratch/gn5-probes
FILES="tests/unit/test_repo_gate_outcome_table.py tests/unit/test_repo_gate_identity_from_report_dir.py tests/unit/test_repo_gate_identity_is_stable.py tests/unit/test_repo_gate_red_beats_absence.py tests/unit/test_repo_gate_admission.py tests/unit/test_tree_level_accounting_witnesses.py tests/skillflow/test_coding_impl_absence_needs_nothing_else_red.py"
for n in "$@"; do
  d=/tmp/mut/$n; rm -rf "$d"; mkdir -p "$d"
  git -c safe.directory='*' -C "$SRC" archive "$SHA" | tar x -C "$d"
  log=$OUT/mut_$n.txt; ign=$OUT/mut_$n.ignition; : > "$ign"
  {
    echo "mutant: $n  sha: $SHA  copy: $d  date: $(date -u +%FT%TZ)"
    echo "files: $FILES"
    python "$PROBES/mutate.py" "$d" "$n"
  } > "$log" 2>&1
  if [ $? -ne 0 ]; then echo "BARE_RC=apply_failed" >> "$log"; continue; fi
  (cd "$d" && PYTHONPATH="$d" python -c "import aitelier.tools.run_tests.impl as a; print('IMPORT', a.__file__)") >> "$log" 2>&1
  (cd "$d" && GN5_IGN="$ign" PYTHONPATH="$d" python -m pytest -p no:cacheprovider -q -rf $FILES) >> "$log" 2>&1
  echo "BARE_RC=$?" >> "$log"
  echo "IGNITION_BYTES=$(stat -c %s "$ign")" >> "$log"
  echo "end: $(date -u +%FT%TZ)" >> "$log"
done
