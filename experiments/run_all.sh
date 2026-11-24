#!/usr/bin/env bash
# Rerun every experiment on one shared baseline (structure from evsim's
# run_c3.sh, BlokcChin@796b43c), so the report quotes one set of reference values.
#
#   bash experiments/run_all.sh [outdir]
#   QUICK=1 SEEDS=2 RUNS=3 bash experiments/run_all.sh    # smoke test
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-results/main}
SEEDS=${SEEDS:-20}
RUNS=${RUNS:-30}
C=(--seeds "$SEEDS" --runs "$RUNS")
run() { echo; echo "### $*"; local t=$SECONDS; python3 "$@"; echo "### took $((SECONDS - t)) s"; }

python3 -m unittest discover -s tests -q

# Step 1 check (docs/DESIGN.md section 7): the consensus against theory; stops the run if it fails.
mkdir -p "$OUT"
run experiments/check_consensus.py ${QUICK:+--quick} --out "$OUT/check_consensus"

# The six experiments of the design doc; a script is picked up as soon as it exists.
for exp in exp1_equivocation exp2_offline exp3_partition exp4_replay exp5_joint_overload exp6_common_cause; do
  if [[ -f "experiments/$exp.py" ]]; then
    mkdir -p "$OUT"
    run "experiments/$exp.py" "${C[@]}" --out "$OUT/$exp"
  else
    echo "skip $exp (not written yet)"
  fi
done
echo; echo "done: $OUT"
