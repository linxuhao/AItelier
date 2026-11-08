#!/usr/bin/env bash
# Re-measurement arm for evidence/render-lock-r3-followups-20261108.
#
# Every pytest arm runs in a disposable container (network none, 2 cpu, 2 GiB,
# one pytest process), never on the host and never in a production container.
# Mutants are copies of the worktree under /tmp/rqmut/<name> with ONE edit to
# docker/godot/godot_harness.py (see mutate.py); the worktree is never mutated.
#
# usage: bash run_tests.sh            (paths below are absolute)
set -u
TREE=/home/linxuhao/.AItelier/dsh-drivers/dsh-c61eed4a13ba4aaaae96/work/c8-renderlock-r3-20261108
OUT=$TREE/evidence/render-lock-r3-followups-20261108/logs
MIRROR=/tmp/rqlogs
mkdir -p "$MIRROR"
IMAGE=aitelier:latest
PY=/usr/local/bin/python

arm() {   # arm <name> <tree-mounted> <pytest ids...>
  local name=$1 tree=$2; shift 2
  local log=$OUT/$name.txt
  docker rm -f "$name" >/dev/null 2>&1
  {
    echo "# arm: $name"
    echo "# tree: $tree"
    echo "# date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# host: $(hostname)  image: $IMAGE"
    echo "# command: docker run -d --name $name --network none --cpus 2 --memory 2g --init -u 1000:1000 \\"
    echo "#   -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -v $tree:/w -w /w -e PYTHONPATH=/w $IMAGE $PY -m pytest -q -p no:cacheprovider $*"
  } > "$log"
  docker run -d --name "$name" --network none --cpus 2 --memory 2g --init \
    -u 1000:1000 -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
    -v "$tree":/w -w /w -e PYTHONPATH=/w "$IMAGE" "$PY" -m pytest -q -p no:cacheprovider "$@" >> "$log" 2>&1
  local rc
  rc=$(docker wait "$name")
  docker logs "$name" >> "$log" 2>&1
  echo "# exit_code: $rc" >> "$log"
  echo "# ignition_lines: $(docker logs "$name" 2>&1 | grep -c '^IGNITION')" >> "$log"
  cp "$log" "$MIRROR/$name.txt" 2>/dev/null || true
  docker rm -f "$name" >/dev/null 2>&1
  echo "$name rc=$rc log=$log"
}

# 0. today's pristine tree: the render-lock + render-queue families
arm rq-baseline-families-20261108 "$TREE" \
  tests/unit/test_harness_render_lock.py tests/unit/test_harness_render_queue.py
# 1. today's constants the P5/P6 arithmetic uses
arm rq-caller-budget-20261108 "$TREE" tests/unit/test_playtest_caller_budget.py
# 2. V1: heartbeat keeps beating after two consecutive OperationalError failures
arm rq-mutant-v1-20261108 /tmp/rqmut/v1 \
  tests/unit/test_harness_render_queue.py::test_two_locked_beats_in_a_row_still_beat
# 3. V5: waiter retries a sqlite error whose text is not "locked"
arm rq-mutant-v5-20261108 /tmp/rqmut/v5 \
  tests/unit/test_harness_render_queue.py::test_a_non_locked_sqlite_error_propagates_from_the_waiting_acquire
# 4. V6: heartbeat keeps beating after its row is no longer active
arm rq-mutant-v6-20261108 /tmp/rqmut/v6 \
  tests/unit/test_harness_render_queue.py::test_the_heartbeat_stops_once_its_row_is_no_longer_active
# 5. P6: the post-ownership process-lock wait made unbounded again
arm rq-mutant-p6-20261108 /tmp/rqmut/p6 \
  tests/unit/test_harness_render_lock.py::test_a_held_process_render_lock_is_bounded_and_answers_not_rendered \
  tests/unit/test_harness_render_lock.py::test_a_held_render_effect_lock_is_bounded_and_answers_not_rendered
# 6. P7: non-positive limit charged after ownership again (the pre-4dd1711109 shape)
arm rq-mutant-p7-20261108 /tmp/rqmut/p7 \
  tests/unit/test_harness_render_lock.py::test_a_non_positive_limit_renders_on_a_free_render_lock \
  tests/unit/test_harness_render_lock.py::test_a_non_positive_limit_is_refused_at_once_and_says_it_may_not_queue
