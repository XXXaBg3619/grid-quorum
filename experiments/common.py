"""Shared pieces of the experiment scripts (skeleton from evsim's experiment
runners, BlokcChin@796b43c).

Every experiment script has the same shape:
  * argparse with --quick (smoke test), --seeds, --runs, --workers and --out;
  * a grid of jobs, one result row per cell, run in parallel with
    ProcessPoolExecutor (the job function must live at module level);
  * <out>.csv, one row per cell, plus <out>.meta.json with the settings, the
    git commit and the known issues, so every number in the report can be
    traced back to the run that produced it.

A machine that stops long commands can run the grid in pieces: --shard I/N
runs jobs I, I + N, I + 2N, ... and saves their raw results next to <out>;
the same command with --merge N instead puts the N pieces together and writes
the results, identical to one run without --shard (run_grid).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pickle
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
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="run only jobs I, I+N, ... and save their raw results; then the same command with --merge N")
    ap.add_argument("--merge", type=int, default=None, metavar="N", help="put N saved shards together instead of running")
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


def shard_path(out: str, i: int, n: int) -> str:
    return f"{out}.shard{i}of{n}.pkl"


def run_grid(fn: Callable, jobs: Iterable, a, out: str) -> Optional[List]:
    """run_jobs over all jobs; or with --shard I/N only jobs[I::N], saved for a later
    --merge N (returns None); or with --merge N the saved shards, checked against
    the jobs these arguments give. Results come back in job order either way."""
    jobs = list(jobs)
    if a.shard and a.merge:
        raise SystemExit("--shard and --merge exclude each other")
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        if not 0 <= i < n:
            raise SystemExit(f"--shard I/N needs 0 <= I < N, got {a.shard}")
        mine = jobs[i::n]
        part = {"jobs": mine, "results": run_jobs(fn, mine, a.workers)}
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(shard_path(out, i, n), "wb") as fh:
            pickle.dump(part, fh)
        print("wrote", shard_path(out, i, n), f"({len(mine)} of {len(jobs)} jobs)")
        return None
    if a.merge:
        results: List = [None] * len(jobs)
        for i in range(a.merge):
            with open(shard_path(out, i, a.merge), "rb") as fh:
                part = pickle.load(fh)
            if part["jobs"] != jobs[i::a.merge]:
                raise SystemExit(f"shard {i} of {a.merge} was run with other arguments")
            results[i::a.merge] = part["results"]
        return results
    return run_jobs(fn, jobs, a.workers)


def drop_shards(out: str, a) -> None:
    """After a --merge wrote the results: remove the shard files."""
    for i in range(a.merge or 0):
        os.remove(shard_path(out, i, a.merge))


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
