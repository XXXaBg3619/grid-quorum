#!/usr/bin/env bash
# Rerun every experiment on one shared baseline (structure from evsim's
# run_c3.sh, BlokcChin@796b43c), so the report quotes one set of reference values.
#
#   bash experiments/run_all.sh [outdir]           # every script's own seeds and runs, as in results/main
#   QUICK=1 bash experiments/run_all.sh /tmp/smoke  # smoke test
#   SEEDS=10 RUNS=8 bash experiments/run_all.sh     # the same seeds and runs for every experiment
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-results/main}
C=()
if [[ -n "${QUICK:-}" ]]; then C+=(--quick); fi
if [[ -n "${SEEDS:-}" ]]; then C+=(--seeds "$SEEDS"); fi
if [[ -n "${RUNS:-}" ]]; then C+=(--runs "$RUNS"); fi
run() { echo; echo "### $*"; local t=$SECONDS; python3 "$@"; echo "### took $((SECONDS - t)) s"; }

python3 -m unittest discover -s tests -q

# Step 1 check (docs/DESIGN.md section 7): the consensus against theory; stops the run if it fails.
mkdir -p "$OUT"
run experiments/check_consensus.py ${QUICK:+--quick} --out "$OUT/check_consensus"

# Step 3 check: the four architectures on one island with the power model (question 22); stops the run if it fails.
run experiments/check_island.py ${QUICK:+--quick} --out "$OUT/check_island"

# The six experiments of the design doc; a script is picked up as soon as it exists.
for exp in exp1_equivocation exp2_offline exp3_partition exp4_replay exp5_joint_overload exp6_common_cause; do
  if [[ -f "experiments/$exp.py" ]]; then
    mkdir -p "$OUT"
    run "experiments/$exp.py" ${C[@]+"${C[@]}"} --out "$OUT/$exp"
  else
    echo "skip $exp (not written yet)"
  fi
done
echo; echo "done: $OUT"
