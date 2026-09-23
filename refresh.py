"""One entry point for every ingest step.

  py -3 refresh.py --seed                 first run: full backfill, both modules
  py -3 refresh.py --daily                what the scheduler runs each evening
  py -3 refresh.py --papers               arXiv incremental + OpenAlex enrich
  py -3 refresh.py --repos                GitHub snapshot + metrics
  py -3 refresh.py --trending             today's trending boards + alerts
  py -3 refresh.py --classify             reapply themes.yaml after editing it

Each step shells out to the module that owns it, so a failure in one source
leaves the others intact and the log in `ingest_log` says which one broke.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ingest.common import connect, utf8_stdout  # noqa: E402

PY = [sys.executable]


def step(name: str, args: list[str]) -> bool:
    print(f"\n=== {name} " + "=" * (60 - len(name)), flush=True)
    started = time.time()
    r = subprocess.run(PY + args, cwd=ROOT)
    ok = r.returncode == 0
    print(f"=== {name}: {'ok' if ok else 'FAILED'} in {time.time() - started:.0f}s",
          flush=True)
    return ok


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Refresh the tracker")
    ap.add_argument("--seed", action="store_true", help="full first-time backfill")
    ap.add_argument("--daily", action="store_true", help="incremental daily refresh")
    ap.add_argument("--papers", action="store_true")
    ap.add_argument("--repos", action="store_true")
    ap.add_argument("--trending", action="store_true")
    ap.add_argument("--classify", action="store_true")
    ap.add_argument("--since", default="2018-01-01", help="backfill start for --seed")
    ap.add_argument("--max-per-theme", type=int, default=1200)
    args = ap.parse_args()

    if not any([args.seed, args.daily, args.papers, args.repos, args.trending,
                args.classify]):
        ap.print_help()
        return 2

    failures = []

    if args.seed:
        if not step("arxiv backfill",
                    ["ingest/arxiv.py", "--since", args.since, "--quiet"]):
            failures.append("arxiv")
        if not step("openalex discover",
                    ["ingest/openalex.py", "--discover", "--since", args.since,
                     "--max-per-theme", str(args.max_per_theme), "--quiet"]):
            failures.append("openalex discover")
        if not step("openalex enrich", ["ingest/openalex.py", "--enrich", "--quiet"]):
            failures.append("openalex enrich")
        if not step("github discover", ["ingest/github.py", "--discover"]):
            failures.append("github discover")
        if not step("github readme", ["ingest/github.py", "--readme"]):
            failures.append("github readme")
        if not step("github star history", ["ingest/github.py", "--backfill-stars"]):
            failures.append("github stars")
        if not step("github metrics", ["ingest/github.py", "--metrics"]):
            failures.append("github metrics")
        args.classify = True

    if args.papers or args.daily:
        if not step("arxiv incremental", ["ingest/arxiv.py", "--incremental", "--quiet"]):
            failures.append("arxiv")
        if not step("openalex enrich", ["ingest/openalex.py", "--enrich", "--quiet"]):
            failures.append("openalex enrich")
        args.classify = True

    if args.repos or args.daily:
        if not step("github discover", ["ingest/github.py", "--discover", "--pages", "1"]):
            failures.append("github discover")
        if not step("github daily", ["ingest/github.py", "--daily"]):
            failures.append("github daily")
        if not step("github readme", ["ingest/github.py", "--readme"]):
            failures.append("github readme")
        args.classify = True

    if args.trending or args.daily or args.seed:
        if not step("trending boards", ["ingest/trending.py", "--daily"]):
            failures.append("trending")
        # Alerts run even when the capture failed: a board fetched earlier today
        # can still have rules that were added since.
        if not step("trending alerts", ["ingest/notify.py", "--run"]):
            failures.append("alerts")

    if args.classify:
        if not step("classify", ["ingest/classify.py", "--all", "--quiet"]):
            failures.append("classify")

    con = connect()
    n_p = con.execute("SELECT count(*) FROM papers").fetchone()[0]
    n_r = con.execute("SELECT count(*) FROM repos").fetchone()[0]
    n_l = con.execute("SELECT count(*) FROM links").fetchone()[0]
    print(f"\ndatabase now holds {n_p} papers, {n_r} repos, {n_l} paper-repo links")
    if failures:
        print("failed steps: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
