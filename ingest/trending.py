"""GitHub trending boards, captured once a day.

GitHub publishes no trending API, so the boards are read from the public pages
at github.com/trending, which robots.txt leaves open. One fetch per board per
day, with a crawl delay, is the whole network cost.

Boards are stored row by row and never overwritten, which is the point: the
page itself only shows today, while `trending` keeps every day, so a repository
that sat on the board for six days is visible as a run rather than a snapshot.

Rows are classified with `config/trending_themes.yaml`, a general taxonomy (AI
agents, LLM apps, infra, security and so on) that is separate from the quant
finance taxonomy in `themes.yaml`. A trending row that also passes the finance
anchor is added to the tracked repository list, so the boards double as a
discovery source for the finance module.

  py -3 ingest/trending.py --daily        fetch today's boards, enrich, classify
  py -3 ingest/trending.py --classify     reapply trending_themes.yaml
  py -3 ingest/trending.py --show         print the last board captured
"""

from __future__ import annotations

import argparse
import html as ihtml
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import (  # noqa: E402
    IngestRun,
    ROOT,
    clean_text,
    connect,
    load_config,
    today,
    utf8_stdout,
)

TRENDING = "https://github.com/trending"
TRENDING_CONFIG = ROOT / "config" / "trending_themes.yaml"
CRAWL_DELAY = 2.0          # robots.txt asks named crawlers for 1s; double it
ENRICH_LIMIT = 120         # API calls a run, for topics and an exact star count

# Boards captured each day: the overall board plus the languages David actually
# reads. Each one is a single page fetch.
BOARDS = [("", "daily"), ("", "weekly"), ("python", "daily"),
          ("typescript", "daily"), ("rust", "daily"), ("go", "daily")]

_ARTICLE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
_TAGS = re.compile(r"<[^>]+>")
_WINDOW_STARS = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)", re.I)


def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", ihtml.unescape(_TAGS.sub(" ", fragment))).strip()


def _number(fragment: str | None) -> int | None:
    if not fragment:
        return None
    m = re.search(r"[\d,]+", strip_tags(fragment))
    return int(m.group(0).replace(",", "")) if m else None


# ------------------------------------------------------------------- parse ---
def parse_board(page: str) -> list[dict]:
    """Read one trending page into rows.

    Every field is pulled by what it means rather than by the utility classes
    around it (`href` ending in /stargazers, `itemprop="programmingLanguage"`,
    the "N stars today" phrase), because GitHub rewrites those class names far
    more often than it changes the page's structure.
    """
    rows = []
    for rank, art in enumerate(_ARTICLE.findall(page), 1):
        head = re.search(r'<h2[^>]*>(.*?)</h2>', art, re.S)
        if not head:
            continue
        link = re.search(r'href="/([^"/]+/[^"?#]+)"', head.group(1))
        if not link:
            continue
        full_name = link.group(1).strip("/")
        desc = re.search(r"</h2>\s*(?:<[^>]+>\s*)*?<p[^>]*>(.*?)</p>", art, re.S)
        lang = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]*)', art)
        stars = re.search(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', art, re.S)
        forks = re.search(r'href="/[^"]+/(?:forks|network/members)"[^>]*>(.*?)</a>', art, re.S)
        window = _WINDOW_STARS.search(strip_tags(art))
        rows.append({
            "rank": rank,
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": clean_text(strip_tags(desc.group(1))) if desc else None,
            "language": (lang.group(1).strip() if lang else None) or None,
            "stars": _number(stars.group(1) if stars else None),
            "forks": _number(forks.group(1) if forks else None),
            "stars_window": int(window.group(1).replace(",", "")) if window else None,
        })
    return rows


# -------------------------------------------------------------- classifier ---
def _rx(pattern: str | None):
    """Compile a folded YAML regex. `>-` joins lines with a space, which would
    otherwise leave alternatives such as `| \\bmcp\\b` unable to match at the
    start of a string."""
    if not pattern:
        return None
    flat = re.sub(r"\s*\|\s*", "|", re.sub(r"\s+", " ", pattern)).strip()
    return re.compile(flat, re.I) if flat else None


def load_trending_config() -> dict:
    import yaml
    return yaml.safe_load(TRENDING_CONFIG.read_text(encoding="utf-8"))


def compile_themes(cfg: dict) -> list[dict]:
    out = []
    for t in cfg["themes"]:
        out.append({"slug": t["slug"], "label": t["label"], "ord": t.get("ord", 999),
                    "topics": {x.lower() for x in (t.get("topics") or [])},
                    "include": _rx(t.get("include")), "exclude": _rx(t.get("exclude"))})
    return out


def themes_for(row: dict, rules: list[dict]) -> list[str]:
    topics = {t.lower() for t in (row.get("topics") or [])}
    text = " ".join(filter(None, [
        row.get("full_name", "").replace("/", " ").replace("-", " "),
        row.get("description") or "", row.get("language") or "",
        " ".join(topics).replace("-", " "),
    ]))
    hits = []
    for r in rules:
        if r["exclude"] and r["exclude"].search(text):
            continue
        if topics & r["topics"] or (r["include"] and r["include"].search(text)):
            hits.append((r["ord"], r["slug"]))
    return [slug for _, slug in sorted(hits)] or ["other"]


# ------------------------------------------------------------------ enrich ---
def enrich(rows: list[dict], gh, limit: int = ENRICH_LIMIT, verbose: bool = True) -> int:
    """Topics and exact counts for rows the API can still afford.

    Trending pages carry no topics, and topics are the strongest theme signal,
    so each distinct repository is read once per run through the API the rest of
    the project already uses.
    """
    seen: dict[str, dict] = {}
    done = 0
    for row in rows:
        name = row["full_name"]
        if name in seen:
            row.update(seen[name])
            continue
        if done >= limit:
            continue
        resp = gh.get(f"/repos/{name}", allow_404=True)
        done += 1
        if resp is None:
            continue
        repo = resp.json()
        patch = {
            "topics": repo.get("topics") or [],
            "stars": repo.get("stargazers_count", row.get("stars")),
            "forks": repo.get("forks_count", row.get("forks")),
            "description": clean_text(repo.get("description")) or row.get("description"),
            "language": repo.get("language") or row.get("language"),
            "api": repo,
        }
        seen[name] = patch
        row.update(patch)
        if verbose and done % 25 == 0:
            print(f"    enriched {done}", flush=True)
    return done


# ------------------------------------------------------------------- store ---
def store(con, day: str, board: str, window: str, rows: list[dict],
          rules: list[dict]) -> dict:
    stats = {"rows": 0, "new_repos": 0}
    for row in rows:
        row["themes"] = themes_for(row, rules)
        prior = con.execute(
            "SELECT min(day) AS first, count(DISTINCT day) AS days FROM trending "
            "WHERE full_name=?", (row["full_name"],)).fetchone()
        first_seen = prior["first"] or day
        days_seen = (prior["days"] or 0) + (0 if prior["first"] == day else 1)
        if not prior["first"]:
            stats["new_repos"] += 1
        tracked = con.execute("SELECT id FROM repos WHERE full_name=?",
                              (row["full_name"],)).fetchone()
        con.execute(
            """INSERT INTO trending(day, window, board, rank, full_name, url, description,
                                    language, stars, forks, stars_window, topics_json,
                                    themes_json, repo_id, first_seen, days_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, window, board, full_name) DO UPDATE SET
                 rank=excluded.rank, stars=excluded.stars, forks=excluded.forks,
                 stars_window=excluded.stars_window, description=excluded.description,
                 topics_json=excluded.topics_json, themes_json=excluded.themes_json,
                 repo_id=excluded.repo_id, days_seen=excluded.days_seen""",
            (day, window, board, row["rank"], row["full_name"], row["url"],
             row["description"], row["language"], row.get("stars"), row.get("forks"),
             row.get("stars_window"), json.dumps(row.get("topics") or []),
             json.dumps(row["themes"]), tracked["id"] if tracked else None,
             first_seen, days_seen))
        stats["rows"] += 1
    con.commit()
    return stats


def adopt_finance(con, rows: list[dict], cfg: dict, verbose: bool = True) -> int:
    """Add trending repositories that pass the finance anchor to the watchlist.

    Discovery searches by topic and phrase, so a finance repository that is
    trending before anyone has topic-tagged it is invisible to them. The boards
    catch exactly that case, and the anchor keeps everything else out.
    """
    from ingest.github import anchor, upsert_repo

    is_finance = anchor(cfg)
    added = 0
    for row in rows:
        repo = row.get("api")
        # Two independent signals, because one alone is too loose on a general
        # board: `swap` in llama-swap and `project` in a security repo both pass
        # the finance anchor on their own.
        if not repo or not is_finance(repo) or "quant-finance" not in (row.get("themes") or []):
            continue
        if con.execute("SELECT 1 FROM repos WHERE id=?", (repo["id"],)).fetchone():
            continue
        upsert_repo(con, repo)
        added += 1
        if verbose:
            print(f"    finance repo from the board: {row['full_name']}", flush=True)
    con.commit()
    return added


def reclassify(con, rules: list[dict]) -> int:
    rows = con.execute(
        "SELECT rowid, full_name, description, language, topics_json FROM trending"
    ).fetchall()
    n = 0
    for r in rows:
        themes = themes_for({"full_name": r["full_name"], "description": r["description"],
                             "language": r["language"],
                             "topics": json.loads(r["topics_json"] or "[]")}, rules)
        con.execute("UPDATE trending SET themes_json=? WHERE rowid=?",
                    (json.dumps(themes), r["rowid"]))
        n += 1
    con.commit()
    return n


# -------------------------------------------------------------------- run ----
def fetch_boards(con, boards=BOARDS, day: str | None = None, enrich_limit: int = ENRICH_LIMIT,
                 verbose: bool = True) -> dict:
    from ingest.github import GitHub

    day = day or today()
    rules = compile_themes(load_trending_config())
    client = httpx.Client(
        headers={"User-Agent": "bellwether/0.2 (personal research tracker)",
                 "Accept": "text/html"},
        timeout=60, follow_redirects=True)
    gh = GitHub()
    stats = {"boards": 0, "rows": 0, "new_repos": 0, "enriched": 0, "finance": 0}
    try:
        for board, window in boards:
            params = {"since": window}
            url = f"{TRENDING}/{board}" if board else TRENDING
            if verbose:
                print(f"  board {board or 'all'} / {window}", flush=True)
            r = client.get(url, params=params)
            r.raise_for_status()
            rows = parse_board(r.text)
            if not rows:
                print("    no rows parsed, page layout may have changed", flush=True)
                continue
            stats["enriched"] += enrich(rows, gh, enrich_limit, verbose)
            s = store(con, day, board, window, rows, rules)
            stats["rows"] += s["rows"]
            stats["new_repos"] += s["new_repos"]
            stats["finance"] += adopt_finance(con, rows, load_config(), verbose)
            stats["boards"] += 1
            time.sleep(CRAWL_DELAY)
    finally:
        client.close()
        gh.close()
    return stats


def show(con, limit: int = 25) -> None:
    day = con.execute("SELECT max(day) FROM trending").fetchone()[0]
    if not day:
        print("no boards captured yet; run --daily")
        return
    rows = con.execute(
        "SELECT rank, full_name, language, stars_window, themes_json FROM trending "
        "WHERE day=? AND window='daily' AND board='' ORDER BY rank LIMIT ?",
        (day, limit)).fetchall()
    print(f"trending {day}")
    for r in rows:
        themes = ", ".join(json.loads(r["themes_json"])[:2])
        gained = f"+{r['stars_window']}" if r["stars_window"] else ""
        print(f"  {r['rank']:>2}. {r['full_name']:<45} {gained:>7} "
              f"{(r['language'] or ''):<12} {themes}")


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="GitHub trending boards")
    ap.add_argument("--daily", action="store_true", help="fetch today's boards")
    ap.add_argument("--classify", action="store_true", help="reapply trending_themes.yaml")
    ap.add_argument("--show", action="store_true", help="print the last board captured")
    ap.add_argument("--enrich-limit", type=int, default=ENRICH_LIMIT)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not any([args.daily, args.classify, args.show]):
        print("nothing to do; pass --daily, --classify or --show", file=sys.stderr)
        return 2

    con = connect()
    verbose = not args.quiet
    if args.daily:
        print("GitHub trending", flush=True)
        with IngestRun(con, "github:trending") as log:
            s = fetch_boards(con, enrich_limit=args.enrich_limit, verbose=verbose)
            log.fetched, log.inserted = s["rows"], s["new_repos"]
        print(f"  {s['boards']} boards, {s['rows']} rows, {s['new_repos']} repos never "
              f"seen before, {s['finance']} added to the finance watchlist")
    if args.classify:
        n = reclassify(con, compile_themes(load_trending_config()))
        print(f"reclassified {n} trending rows")
    if args.show:
        show(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
