"""GitHub ingest: discovery, README fetch, daily snapshots, growth metrics.

Authentication comes from the `gh` CLI keyring via `gh auth token`, so no token
is ever written to a file in this project. The token is held in memory for the
run and never printed.

  py -3 ingest/github.py --discover          build the watchlist from themes.yaml
  py -3 ingest/github.py --readme            fetch README excerpts + paper links
  py -3 ingest/github.py --backfill-stars    one-off star history for growth
  py -3 ingest/github.py --snapshot          record today's stars
  py -3 ingest/github.py --metrics           recompute 7d/30d growth and spikes
  py -3 ingest/github.py --daily             snapshot + metrics, for the scheduler

Rate limits: 5000 core requests an hour and 30 searches a minute, both of which
the client tracks and waits out rather than failing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import (  # noqa: E402
    IngestRun,
    clean_text,
    connect,
    load_config,
    today,
    utf8_stdout,
)

API = "https://api.github.com"
SEARCH_PAGES = 3          # 100 results a page, ordered by stars
PER_PAGE = 100
STAR_SAMPLES = 24         # pages sampled when reconstructing a star curve
README_CHARS = 1200

_ARXIV_IN_TEXT = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})|arxiv[:\s]+(\d{4}\.\d{4,5})", re.I)
_MD_NOISE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|<[^>]+>")


# ------------------------------------------------------------------ client ---
def gh_token() -> str:
    """Read the token from the gh CLI keyring. Never logged, never persisted."""
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                             shell=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"could not run `gh auth token`: {e}")
    tok = out.stdout.strip()
    if not tok:
        raise RuntimeError("gh returned no token; run `gh auth login` first")
    return tok


class GitHub:
    def __init__(self):
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {gh_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bellwether/0.2",
            },
            timeout=60, follow_redirects=True,
        )

    def close(self):
        self.client.close()

    def get(self, path: str, params: dict | None = None, accept: str | None = None,
            allow_404: bool = False):
        headers = {"Accept": accept} if accept else None
        for attempt in range(5):
            r = self.client.get(f"{API}{path}", params=params, headers=headers)
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code in (403, 429):
                # Secondary rate limit or quota exhausted: both tell us when to
                # come back, so wait rather than hammering.
                wait = int(r.headers.get("retry-after", 0))
                if not wait:
                    reset = int(r.headers.get("x-ratelimit-reset", 0))
                    wait = max(2, reset - int(time.time()) + 2) if reset else 30
                wait = min(wait, 300)
                print(f"    rate limited, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"GitHub request failed repeatedly: {path}")

    def search_repos(self, query: str, pages: int = SEARCH_PAGES):
        for page in range(1, pages + 1):
            r = self.get("/search/repositories", {
                "q": query, "sort": "stars", "order": "desc",
                "per_page": PER_PAGE, "page": page})
            if r is None:
                return
            data = r.json()
            items = data.get("items", [])
            yield from items
            if len(items) < PER_PAGE or page * PER_PAGE >= min(data.get("total_count", 0), 1000):
                return
            time.sleep(2.2)   # 30 searches a minute


# -------------------------------------------------------------------- gate ---
def _rx(pattern: str | None):
    """Compile a folded YAML regex. `>-` joins lines with a space, which would
    otherwise leave alternatives such as `| stochastic volatility` unable to
    match at the start of a string."""
    if not pattern:
        return None
    flat = re.sub(r"\s*\|\s*", "|", re.sub(r"\s+", " ", pattern)).strip()
    return re.compile(flat, re.I) if flat else None


def anchor(cfg: dict):
    """Is this repository actually about finance?

    Generic topics collide across domains: `topic:options` returns select-box
    widgets, `topic:volatility` returns memory forensics, `topic:arch` returns
    Linux packaging. One unambiguous topic settles it; otherwise the name,
    description or topic list has to carry a finance term, and a veto pattern
    throws out the collisions that still slip through.
    """
    cfgs = cfg.get("repo_anchor") or {}
    clear_topics = {t.lower() for t in (cfgs.get("topics") or [])}
    require = _rx(cfgs.get("require"))
    veto = _rx(cfgs.get("veto"))

    def is_finance(repo: dict) -> bool:
        topics = {t.lower() for t in (repo.get("topics") or [])}
        text = " ".join(filter(None, [
            repo.get("full_name", "").replace("/", " ").replace("-", " "),
            repo.get("description") or "",
            " ".join(topics).replace("-", " "),
        ]))
        if veto and veto.search(text):
            return False
        if topics & clear_topics:
            return True
        return bool(require and require.search(text))

    return is_finance


def gate(cfg: dict):
    g = cfg.get("repo_gate") or {}
    blocked = [re.compile(p, re.I) for p in (cfg.get("repo_blocklist") or [])]
    is_finance = anchor(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * int(g.get("max_months_since_push", 24)))

    def ok(repo: dict) -> bool:
        if not is_finance(repo):
            return False
        if repo.get("stargazers_count", 0) < int(g.get("min_stars", 0)):
            return False
        if repo.get("fork") and not g.get("allow_forks", False):
            return False
        if repo.get("archived") and not g.get("allow_archived", False):
            return False
        pushed = repo.get("pushed_at")
        if pushed:
            try:
                if datetime.fromisoformat(pushed.replace("Z", "+00:00")) < cutoff:
                    return False
            except ValueError:
                pass
        name = repo.get("full_name", "")
        return not any(b.search(name) for b in blocked)

    return ok


# ------------------------------------------------------------------ upsert ---
def upsert_repo(con, repo: dict) -> str:
    existing = con.execute("SELECT id FROM repos WHERE id=?", (repo["id"],)).fetchone()
    row = (
        repo["id"], repo["full_name"], repo["owner"]["login"], repo["name"],
        clean_text(repo.get("description")), repo.get("homepage") or None,
        repo["html_url"], repo.get("language"),
        repo.get("stargazers_count", 0), repo.get("forks_count", 0),
        repo.get("open_issues_count", 0), repo.get("created_at"), repo.get("pushed_at"),
        json.dumps(repo.get("topics") or []),
        (repo.get("license") or {}).get("spdx_id") if repo.get("license") else None,
        int(bool(repo.get("fork"))), int(bool(repo.get("archived"))),
    )
    con.execute(
        """INSERT INTO repos(id, full_name, owner, name, description, homepage, url,
                             language, stars, forks, open_issues, created_at, pushed_at,
                             topics_json, license, is_fork, archived)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             full_name=excluded.full_name, description=excluded.description,
             homepage=excluded.homepage, language=excluded.language,
             stars=excluded.stars, forks=excluded.forks,
             open_issues=excluded.open_issues, pushed_at=excluded.pushed_at,
             topics_json=excluded.topics_json, license=excluded.license,
             archived=excluded.archived, last_seen_at=datetime('now')""",
        row)
    con.execute(
        "INSERT OR REPLACE INTO repo_snapshots(repo_id, day, stars, forks) VALUES(?,?,?,?)",
        (repo["id"], today(), repo.get("stargazers_count", 0), repo.get("forks_count", 0)))
    index_repo(con, repo["id"])
    return "merge" if existing else "insert"


def index_repo(con, repo_id: int) -> None:
    r = con.execute(
        "SELECT full_name, description, topics_json, readme_excerpt FROM repos WHERE id=?",
        (repo_id,)).fetchone()
    if not r:
        return
    con.execute("DELETE FROM repos_fts WHERE repo_id=?", (repo_id,))
    con.execute(
        "INSERT INTO repos_fts(full_name, description, topics, readme, repo_id) "
        "VALUES(?,?,?,?,?)",
        (r["full_name"].replace("/", " ").replace("-", " "), r["description"] or "",
         " ".join(json.loads(r["topics_json"] or "[]")), r["readme_excerpt"] or "",
         repo_id))


# --------------------------------------------------------------- discovery ---
def build_search_queries(cfg: dict) -> list[str]:
    """One query per topic, plus one per multi-word theme phrase. Topics are
    deduplicated across themes, since most themes share several."""
    g = cfg.get("repo_gate") or {}
    min_stars = int(g.get("min_stars", 15))
    months = int(g.get("max_months_since_push", 24))
    pushed_since = (datetime.now(timezone.utc) - timedelta(days=30 * months)).date()
    tail = f"stars:>={min_stars} pushed:>={pushed_since}"

    topics, phrases = [], []
    for theme in cfg["themes"]:
        for t in theme.get("gh_topics") or []:
            if t not in topics:
                topics.append(t)
        for p in (theme.get("search_extra") or [])[:2]:
            if p not in phrases:
                phrases.append(p)

    queries = [f"topic:{t} {tail}" for t in topics]
    queries += [f'"{p}" in:name,description,readme {tail}' for p in phrases]
    return queries


def discover(con, gh: GitHub, cfg: dict, pages: int, limit: int | None,
             verbose: bool = True) -> dict:
    ok = gate(cfg)
    stats = {"fetched": 0, "inserted": 0, "merged": 0, "rejected": 0}
    seen: set[int] = set()
    queries = build_search_queries(cfg)
    for i, q in enumerate(queries, 1):
        if verbose:
            print(f"  [{i}/{len(queries)}] {q[:70]}", flush=True)
        for repo in gh.search_repos(q, pages):
            if repo["id"] in seen:
                continue          # topics overlap heavily across themes
            seen.add(repo["id"])
            stats["fetched"] += 1
            if not ok(repo):
                stats["rejected"] += 1
                continue
            action = upsert_repo(con, repo)
            stats["inserted" if action == "insert" else "merged"] += 1
            if limit and stats["inserted"] >= limit:
                con.commit()
                return stats
        con.commit()
    return stats


# ------------------------------------------------------------------ readme ---
def readme_excerpt(text: str) -> str:
    """Strip badge images and HTML so the excerpt reads as prose."""
    body = _MD_NOISE.sub(" ", text)
    body = re.sub(r"[#*`>_|-]{2,}", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:README_CHARS]


def link_papers_from_text(con, repo_id: int, text: str) -> int:
    """Repos cite the paper they implement by arXiv id far more often than by
    title, so that is the high-confidence link."""
    ids = {a or b for a, b in _ARXIV_IN_TEXT.findall(text)}
    n = 0
    for aid in filter(None, ids):
        row = con.execute("SELECT id FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
        if row:
            con.execute(
                "INSERT OR REPLACE INTO links(paper_id, repo_id, confidence, method) "
                "VALUES(?,?,?,?)", (row["id"], repo_id, 0.95, "arxiv_id_in_readme"))
            n += 1
    return n


def fetch_readmes(con, gh: GitHub, limit: int | None, refresh: bool,
                  verbose: bool = True) -> dict:
    where = "" if refresh else "WHERE readme_excerpt IS NULL"
    rows = con.execute(
        f"SELECT id, full_name FROM repos {where} ORDER BY stars DESC").fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"fetched": 0, "links": 0}
    for i, r in enumerate(rows, 1):
        resp = gh.get(f"/repos/{r['full_name']}/readme",
                      accept="application/vnd.github.raw", allow_404=True)
        if resp is None:
            con.execute("UPDATE repos SET readme_excerpt='' WHERE id=?", (r["id"],))
            continue
        text = resp.text
        con.execute("UPDATE repos SET readme_excerpt=? WHERE id=?",
                    (readme_excerpt(text), r["id"]))
        stats["links"] += link_papers_from_text(con, r["id"], text)
        index_repo(con, r["id"])
        stats["fetched"] += 1
        if i % 50 == 0:
            con.commit()
            if verbose:
                print(f"    {i}/{len(rows)}", flush=True)
    con.commit()
    return stats


# ------------------------------------------------------------ star history ---
def stargazers_available(gh: GitHub) -> bool:
    """Can this account list stargazers with timestamps?

    On some accounts GitHub refuses the stargazer listing entirely: REST answers
    404 for every repository (401 unauthenticated) and the GraphQL `stargazers`
    connection comes back with `stargazerCount` populated but zero edges. The
    repository itself reads fine, so the failure is specific to the listing.
    Probing once up front turns that into a clear message instead of a silent
    run that records nothing.
    """
    resp = gh.get("/repos/ranaroussi/yfinance/stargazers",
                  {"per_page": 1, "page": 1},
                  accept="application/vnd.github.star+json", allow_404=True)
    if resp is None:
        return False
    try:
        items = resp.json()
    except ValueError:
        return False
    return isinstance(items, list) and bool(items) and "starred_at" in items[0]


def backfill_stars(con, gh: GitHub, limit: int | None, verbose: bool = True) -> dict:
    """Reconstruct a star curve without paging through every stargazer.

    Stargazers come back oldest first, so the last entry on page N is star
    number N*100, and two dozen sampled pages give the whole curve whether a
    repo has 500 stars or 50,000. Only usable where the account is allowed to
    list stargazers at all, which `stargazers_available` checks first.
    """
    if not stargazers_available(gh):
        print("  stargazer listing is not available to this account, so star\n"
              "  history cannot be backfilled. Growth builds forward instead:\n"
              "  run --daily each day and real 7d/30d figures appear within a\n"
              "  week. Until then the Growing sort falls back to lifetime\n"
              "  average stars per day.", flush=True)
        return {"repos": 0, "points": 0, "unavailable": True}
    rows = con.execute(
        "SELECT id, full_name, stars FROM repos WHERE stars > 0 AND id NOT IN "
        "(SELECT repo_id FROM repo_snapshots GROUP BY repo_id HAVING count(*) > 3) "
        "ORDER BY stars DESC").fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"repos": 0, "points": 0}
    for i, r in enumerate(rows, 1):
        total = r["stars"]
        last_page = min(math.ceil(total / PER_PAGE), 400)   # API stops at 40k
        if last_page < 2:
            continue
        step = max(1, last_page // STAR_SAMPLES)
        pages = sorted({p for p in range(1, last_page + 1, step)} | {last_page})
        for p in pages:
            resp = gh.get(f"/repos/{r['full_name']}/stargazers",
                          {"per_page": PER_PAGE, "page": p},
                          accept="application/vnd.github.star+json", allow_404=True)
            if resp is None:
                break
            items = resp.json()
            if not isinstance(items, list) or not items:
                break
            starred = items[-1].get("starred_at")
            if not starred:
                break
            day = starred[:10]
            cumulative = min((p - 1) * PER_PAGE + len(items), total)
            con.execute(
                "INSERT OR REPLACE INTO repo_snapshots(repo_id, day, stars) VALUES(?,?,?)",
                (r["id"], day, cumulative))
            stats["points"] += 1
        con.execute(
            "INSERT OR REPLACE INTO repo_snapshots(repo_id, day, stars) VALUES(?,?,?)",
            (r["id"], today(), total))
        stats["repos"] += 1
        con.commit()
        if verbose and i % 20 == 0:
            print(f"    {i}/{len(rows)} repos", flush=True)
    return stats


def snapshot(con, gh: GitHub, verbose: bool = True) -> dict:
    rows = con.execute("SELECT id, full_name FROM repos WHERE hidden=0").fetchall()
    stats = {"snapped": 0, "gone": 0}
    for i, r in enumerate(rows, 1):
        resp = gh.get(f"/repos/{r['full_name']}", allow_404=True)
        if resp is None:
            stats["gone"] += 1
            continue
        repo = resp.json()
        upsert_repo(con, repo)
        stats["snapped"] += 1
        if i % 100 == 0:
            con.commit()
            if verbose:
                print(f"    {i}/{len(rows)}", flush=True)
    con.commit()
    return stats


def prune(con, cfg: dict, apply: bool = False, verbose: bool = True) -> dict:
    """Re-apply the finance anchor to repositories already stored.

    Needed whenever `repo_anchor` changes, and after any discovery run made
    before a tightening: the search API cost is already paid, so filtering
    afterwards is cheaper than fetching everything again.
    """
    is_finance = anchor(cfg)
    rows = con.execute(
        "SELECT id, full_name, description, topics_json, stars FROM repos "
        "WHERE hidden=0").fetchall()
    doomed = []
    for r in rows:
        repo = {"full_name": r["full_name"], "description": r["description"],
                "topics": json.loads(r["topics_json"] or "[]")}
        if not is_finance(repo):
            doomed.append((r["id"], r["full_name"], r["stars"]))
    if verbose:
        print(f"  {len(doomed)} of {len(rows)} repos fail the finance anchor")
        for _, name, stars in doomed[:15]:
            print(f"    - {name} ({stars} stars)")
        if len(doomed) > 15:
            print(f"    ... and {len(doomed) - 15} more")
    if apply and doomed:
        con.executemany("DELETE FROM repos WHERE id=?", [(d[0],) for d in doomed])
        con.executemany("DELETE FROM repos_fts WHERE repo_id=?", [(d[0],) for d in doomed])
        con.executemany("DELETE FROM item_themes WHERE item_type='repo' AND item_id=?",
                        [(d[0],) for d in doomed])
        con.commit()
        if verbose:
            print(f"  removed {len(doomed)}")
    elif doomed and verbose:
        print("  dry run; pass --apply to remove them")
    return {"checked": len(rows), "failed": len(doomed), "removed": len(doomed) if apply else 0}


# ----------------------------------------------------------------- metrics ---
def _stars_at(history: list[tuple[str, int]], target: str) -> int | None:
    """Star count on or before `target`, the most recent such reading."""
    best = None
    for day, stars in history:
        if day <= target:
            best = stars
        else:
            break
    return best


def compute_metrics(con, verbose: bool = True) -> dict:
    rows = con.execute("SELECT id, stars, created_at FROM repos").fetchall()
    now = datetime.now(timezone.utc).date()
    d7 = (now - timedelta(days=7)).isoformat()
    d30 = (now - timedelta(days=30)).isoformat()
    updated = spikes = 0

    for r in rows:
        # Lifetime average is the interim ranking signal: it is available from
        # the first day, and it is what the Growing sort falls back to while
        # too little real history exists to measure a 30 day delta.
        per_day = None
        if r["created_at"]:
            try:
                born = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                age = max((datetime.now(timezone.utc) - born).days, 1)
                per_day = r["stars"] / age
            except ValueError:
                per_day = None
        con.execute("UPDATE repos SET stars_per_day=? WHERE id=?", (per_day, r["id"]))

        hist = [(h["day"], h["stars"]) for h in con.execute(
            "SELECT day, stars FROM repo_snapshots WHERE repo_id=? ORDER BY day",
            (r["id"],))]
        if len(hist) < 2:
            continue
        latest = hist[-1][1]
        s7 = _stars_at(hist, d7)
        s30 = _stars_at(hist, d30)
        stars_7d = latest - s7 if s7 is not None else None
        stars_30d = latest - s30 if s30 is not None else None

        # Dividing by log(stars) is what lets a 300 star repo adding 80 outrank
        # a 40k star repo adding 200.
        momentum = (stars_30d / math.log(1 + max(latest, 1))) if stars_30d else None

        # Spike: this week's gain far outside the repo's own weekly history.
        spike = 0
        weekly = []
        for a, b in zip(hist, hist[1:]):
            days = (datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[0])).days
            if days > 0:
                weekly.append((b[1] - a[1]) * 7 / days)
        if len(weekly) >= 5 and stars_7d is not None:
            body = weekly[:-1]
            sd = statistics.pstdev(body)
            if sd > 0 and stars_7d > statistics.fmean(body) + 3 * sd and stars_7d >= 20:
                spike, spikes = 1, spikes + 1

        con.execute(
            "UPDATE repos SET stars_7d=?, stars_30d=?, momentum=?, spike=? WHERE id=?",
            (stars_7d, stars_30d, momentum, spike, r["id"]))
        updated += 1
    con.commit()
    with_history = con.execute(
        "SELECT count(*) FROM repos WHERE stars_30d IS NOT NULL").fetchone()[0]
    return {"updated": updated, "spikes": spikes, "with_history": with_history}


# -------------------------------------------------------------------- main ---
def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="GitHub ingest")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--readme", action="store_true")
    ap.add_argument("--refresh-readme", action="store_true")
    ap.add_argument("--backfill-stars", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--metrics", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="re-apply the finance anchor to stored repos")
    ap.add_argument("--apply", action="store_true",
                    help="with --prune, actually delete rather than list")
    ap.add_argument("--daily", action="store_true", help="snapshot then metrics")
    ap.add_argument("--pages", type=int, default=SEARCH_PAGES)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not any([args.discover, args.readme, args.refresh_readme, args.backfill_stars,
                args.snapshot, args.metrics, args.daily, args.prune]):
        print("nothing to do; pass --discover, --readme, --snapshot, "
              "--backfill-stars, --metrics or --daily", file=sys.stderr)
        return 2

    con = connect()
    cfg = load_config()
    verbose = not args.quiet
    gh = GitHub()
    try:
        if args.discover:
            print("GitHub discover", flush=True)
            with IngestRun(con, "github:discover") as log:
                s = discover(con, gh, cfg, args.pages, args.limit, verbose)
                log.fetched, log.inserted, log.merged = s["fetched"], s["inserted"], s["merged"]
            print(f"  seen {s['fetched']}, new {s['inserted']}, updated {s['merged']}, "
                  f"rejected by gate {s['rejected']}")

        if args.prune:
            print("GitHub prune", flush=True)
            prune(con, cfg, args.apply, verbose)

        if args.readme or args.refresh_readme:
            print("GitHub readme", flush=True)
            s = fetch_readmes(con, gh, args.limit, args.refresh_readme, verbose)
            print(f"  readmes {s['fetched']}, paper links {s['links']}")

        if args.backfill_stars:
            print("GitHub star history backfill", flush=True)
            s = backfill_stars(con, gh, args.limit, verbose)
            print(f"  {s['repos']} repos, {s['points']} history points")

        if args.snapshot or args.daily:
            print("GitHub snapshot", flush=True)
            with IngestRun(con, "github:snapshot") as log:
                s = snapshot(con, gh, verbose)
                log.fetched = s["snapped"]
            print(f"  snapped {s['snapped']}, missing {s['gone']}")

        if args.metrics or args.daily:
            s = compute_metrics(con, verbose)
            print(f"metrics: {s['with_history']} repos have real 30d growth, "
                  f"{s['spikes']} spiking; the rest rank on lifetime average "
                  f"until daily snapshots accumulate")
    finally:
        gh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
