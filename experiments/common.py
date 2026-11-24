"""Shared pieces of the experiment scripts (skeleton from evsim's experiment
runners, BlokcChin@796b43c).

Every experiment script has the same shape:
  * argparse with --quick (smoke test), --seeds, --runs, --workers and --out;
  * a grid of jobs, one result row per cell, run in parallel with
    ProcessPoolExecutor (the job function must live at module level);
  * <out>.csv, one row per cell, plus <out>.meta.json with the settings, the
    git commit and the known issues, so every number in the report can be
    traced back to the run that produced it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Iterable, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def common_args(ap: Optional[argparse.ArgumentParser] = None, seeds: int = 10,
                runs: int = 30) -> argparse.ArgumentParser:
    ap = ap or argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="smoke test: 2 seeds x 3 runs")
    ap.add_argument("--seeds", type=int, default=seeds)
    ap.add_argument("--runs", type=int, default=runs, help="island events per seed")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=None, help="output path without extension")
    return ap


def sizes(a) -> Tuple[int, int]:
    """(seeds, runs), shrunk by --quick."""
    return (2, 3) if a.quick else (a.seeds, a.runs)


def run_jobs(fn: Callable, jobs: Iterable, workers: int = 1) -> List:
    """fn over jobs, in parallel when workers > 1; results in job order."""
    jobs = list(jobs)
    if workers <= 1 or len(jobs) <= 1:
        return [fn(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, jobs))


def git_commit() -> str:
    """Short commit hash of the code that produced a result, '-dirty' if uncommitted changes exist."""
    try:
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (head or "unknown") + ("-dirty" if head and dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_results(out: str, rows: List[dict], meta: dict) -> None:
    """Write <out>.csv (union of all row keys, first-seen order) and <out>.meta.json."""
    if not rows:
        raise ValueError("no rows to write")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fields: List[str] = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with open(out + ".csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(out + ".meta.json", "w", encoding="utf-8") as fh:
        json.dump({"git_commit": git_commit(), **meta}, fh, indent=2, ensure_ascii=False, default=str)
    print("wrote", out + ".csv", "and", out + ".meta.json")
