#!/bin/bash
T=/home/linxuhao/.AItelier/worktrees-scratch/onereading2-tools
H=$T/harness; L=$T/logs
run() { "$T/run_engine.sh" "$@"; echo "$5 BARE_RC=$?"; }
run $H/h_8b084c20.py $T/engine_mine "" $L/engine_path_8b084c20.txt base8b
run $H/h_290d7908.py $T/engine_mine "" $L/engine_path_290d7908.txt base29
run $H/h_5fbe7c24.py $T/engine_mine "" $L/engine_path_cand.txt cand
run $H/h_8b084c20.py $T/engine_review crit $L/engine_crit_8b084c20.txt crit8b
run $H/h_5fbe7c24.py $T/engine_review crit $L/engine_crit_cand.txt critcand
run $H/h_290d7908.py $T/engine_review battery $L/engine_battery_290d7908.txt bat29
run $H/h_5fbe7c24.py $T/engine_review battery $L/engine_battery_cand.txt batcand
run $H/h_5fbe7c24_G1.py $T/engine_mine "" $L/engine_path_cand_G1.txt g1
run $H/h_5fbe7c24_G2.py $T/engine_review battery $L/engine_battery_cand_G2.txt g2
