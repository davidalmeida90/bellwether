"""OpenAlex ingest. Keyless, free, polite pool via a mailto parameter.

Two jobs, both needed:

  --enrich    every paper already in the database that carries a DOI gets its
              citation count, venue and open-access PDF looked up in batches of
              50. That is what gives the arXiv corpus citation numbers, which
              arXiv itself never publishes.

  --discover  theme-driven search that pulls in work never posted to arXiv:
              journal articles, SSRN working papers, NBER and RePEc. Search
              phrases are derived from the same `include` regexes in
              themes.yaml, so the taxonomy stays the single source of truth.

  py -3 ingest/openalex.py --enrich
  py -3 ingest/openalex.py --discover --since 2018-01-01
  py -3 ingest/openalex.py --discover --theme volatility-options --limit 50
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import (  # noqa: E402
    CONTACT_EMAIL,
    get_meta,
    set_meta,
    USER_AGENT,
    IngestRun,
    clean_text,
    connect,
    load_config,
    norm_arxiv,
    today,
    utf8_stdout,
    upsert_paper,
)

API = "https://api.openalex.org/works"
PAGE = 200
BATCH = 50            # DOIs per enrich request
SLEEP = 0.45          # sorted cursor paging is costly; stay well under
MAX_RETRIES = 7
MAX_BACKOFF = 120     # never sleep longer than this on a 429

SELECT = ",".join([
    "id", "doi", "display_name", "publication_date", "publication_year", "type",
    "cited_by_count", "open_access", "best_oa_location", "primary_location",
    "authorships", "topics", "abstract_inverted_index", "locations",
])

# Sources that are working-paper series rather than journals.
WORKING_PAPER_HOSTS = ("ssrn", "research papers in economics", "repec",
                       "national bureau of economic research", "nber",
                       "cepr", "bis working", "working paper")


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                        timeout=90)


def get(client: httpx.Client, params: dict) -> dict:
    """One request, with backoff.

    A 429 means throttled rather than broken, so it waits out `Retry-After`,
    which runs to tens of seconds. Retrying such a response after one second
    just burns the retry budget and fails the whole run.
    """
    params = {**params, "select": SELECT}
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    delay = 2.0
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = client.get(API, params=params)
            if r.status_code in (429, 503):
                asked = int(r.headers.get("retry-after", 0) or 0)
                # OpenAlex answers a burst limit with seconds-until-reset, which
                # can be hours. Waiting that out would hang the run for a whole
                # evening, so anything past the cap gives up on this theme and
                # lets the caller move to the next one.
                if asked > MAX_BACKOFF:
                    raise RuntimeError(
                        f"http {r.status_code}, throttled for {asked}s "
                        f"(over the {MAX_BACKOFF}s cap); skipping")
                wait = asked or min(delay * 4, MAX_BACKOFF)
                print(f"    throttled ({r.status_code}), waiting {wait:.0f}s",
                      flush=True)
                time.sleep(wait)
                delay *= 2
                last_err = RuntimeError(f"http {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"OpenAlex request failed: {last_err}")


# ------------------------------------------------------------- conversion ---
def reconstruct_abstract(index: dict | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]} for copyright reasons.
    Inverting that back to running text is exact, not approximate."""
    if not index:
        return None
    slots: list[tuple[int, str]] = []
    for word, positions in index.items():
        for p in positions:
            slots.append((p, word))
    if not slots:
        return None
    slots.sort()
    return clean_text(" ".join(w for _, w in slots))


def _arxiv_from_locations(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        for url in (loc.get("landing_page_url"), loc.get("pdf_url")):
            aid = norm_arxiv(url) if url and "arxiv.org" in url else None
            if aid:
                return aid
    return None


def _venue(work: dict) -> tuple[str | None, str]:
    src = (work.get("primary_location") or {}).get("source") or {}
    name = src.get("display_name")
    wtype = (work.get("type") or "").lower()
    low = (name or "").lower()
    if any(h in low for h in WORKING_PAPER_HOSTS):
        return name, "working_paper"
    if wtype in ("preprint", "posted-content"):
        return name, "preprint"
    if name:
        return name, "journal"
    return None, "repository"


def to_record(work: dict) -> dict | None:
    title = clean_text(work.get("display_name"))
    if not title:
        return None
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    venue, venue_type = _venue(work)
    pdf_url = best.get("pdf_url") or oa.get("oa_url")
    landing = (best.get("landing_page_url") or work.get("doi")
               or work.get("id"))
    return {
        "source": "openalex",
        "ext_id": (work.get("id") or "").rsplit("/", 1)[-1],
        "doi": work.get("doi"),
        "arxiv_id": _arxiv_from_locations(work),
        "title": title,
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "authors": [
            a.get("author", {}).get("display_name")
            for a in work.get("authorships") or []
        ],
        "published_at": work.get("publication_date"),
        "updated_at": work.get("publication_date"),
        "venue": venue,
        "venue_type": venue_type,
        "is_preprint": venue_type == "preprint",
        "concepts": [t.get("display_name") for t in (work.get("topics") or [])
                     if t.get("display_name")],
        "citations": work.get("cited_by_count") or 0,
        "citations_updated_at": today(),
        "pdf_url": pdf_url,
        "landing_url": landing,
        "oa_status": oa.get("oa_status") or ("closed" if not pdf_url else "unknown"),
    }


# ------------------------------------------------------------ theme phrases ---
_CLEAN_PHRASE = re.compile(r"^[a-z][a-z ]{4,}$")
_TOO_COMPLEX = re.compile(r"[.\\+*?^$]|\{\d")
_CHAR_CLASS = re.compile(r"\[([^\]]+)\]")
_GROUP = re.compile(r"\(([^()]+)\)")

# Regexes are written with stems so they catch every inflection. OpenAlex
# searches whole tokens, so `implied volatilit` matches nothing at all and the
# stem has to be completed before the phrase is usable.
_STEMS = {
    "volatilit": "volatility",
    "cryptocurrenc": "cryptocurrency",
    "rebalanc": "rebalancing",
    "hedg": "hedging",
    "switch": "switching",
    "anomal": "anomaly",
    "premi": "premium",
    "nowcast": "nowcasting",
    "optimi": "optimization",
    "filing": "filings",
}

_MAX_PHRASES = 24


def _expand(alt: str) -> list[str]:
    """Turn one regex alternative into the literal strings it can match.

    Handles the two constructs the taxonomy actually uses, `[sz]` style classes
    and `(um|a)` style groups. Anything with wildcards or quantifiers is
    dropped, since guessing at those would produce junk queries.
    """
    alt = alt.strip()
    if not alt or _TOO_COMPLEX.search(alt):
        return []

    variants = [alt]
    for _ in range(3):  # nested constructs are at most a couple deep here
        grown: list[str] = []
        changed = False
        for v in variants:
            m = _CHAR_CLASS.search(v) or _GROUP.search(v)
            if not m:
                grown.append(v)
                continue
            changed = True
            body = m.group(1)
            # `[- ]` means "hyphen or space"; a space alone searches the same,
            # so only one variant is emitted rather than doubling every query.
            opts = [" "] if body == "- " else (
                body.split("|") if "|" in body else list(body)
            )
            for o in opts:
                grown.append(v[: m.start()] + o + v[m.end():])
        variants = grown
        if not changed:
            break

    out = []
    for v in variants:
        v = re.sub(r"\s+", " ", v.replace("-", " ")).strip()
        words = v.split()
        if words and words[-1] in _STEMS:
            words[-1] = _STEMS[words[-1]]
            v = " ".join(words)
        if _CLEAN_PHRASE.fullmatch(v) and " " in v:
            out.append(v)
    return out


def theme_phrases(theme: dict | str | None) -> list[str]:
    """Search phrases for one theme: literals expanded out of its `include`
    regex, plus any high-precision single words listed in `search_extra`."""
    if theme is None:
        return []
    if isinstance(theme, str):        # regex passed directly
        theme = {"include": theme}

    include = theme.get("include")
    phrases: list[str] = []
    seen: set[str] = set()
    for extra in theme.get("search_extra") or []:
        e = str(extra).strip().lower()
        if e and e not in seen:
            seen.add(e)
            phrases.append(e)
    if include:
        flat = re.sub(r"\s+", " ", include.replace("\n", ""))
        for alt in flat.split("|"):
            for p in _expand(alt):
                if p not in seen:
                    seen.add(p)
                    phrases.append(p)
    return phrases[:_MAX_PHRASES]


def discover_filters(cfg: dict, since: str, until: str | None,
                     only: str | None) -> list[tuple[str, str]]:
    filters = []
    for theme in cfg["themes"]:
        if only and theme["slug"] != only:
            continue
        phrases = theme_phrases(theme)
        if not phrases:
            continue
        # OpenAlex ORs values inside one filter with `|`, and ANDs filters
        # separated by commas.
        search = "|".join(phrases)
        parts = [
            f"title_and_abstract.search:{search}",
            f"from_publication_date:{since}",
            "type:article|preprint|review",
        ]
        subfields = (cfg.get("openalex") or {}).get("subfields") or []
        if subfields:
            ids = "|".join(f"https://openalex.org/{s}" for s in subfields)
            parts.append(f"primary_topic.subfield.id:{ids}")
        if until:
            parts.append(f"to_publication_date:{until}")
        filters.append((theme["slug"], ",".join(parts)))
    return filters


# ------------------------------------------------------------------- runs ---
def enrich(con, limit: int | None = None, stale_days: int = 30,
           verbose: bool = True) -> dict:
    """Refresh citations, venue and OA links for papers that have a DOI."""
    rows = con.execute(
        "SELECT id, doi FROM papers WHERE doi IS NOT NULL AND ("
        "  citations_updated_at IS NULL"
        "  OR julianday('now') - julianday(citations_updated_at) > ?"
        ") ORDER BY citations DESC, published_at DESC",
        (stale_days,),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"fetched": 0, "inserted": 0, "merged": 0}
    if not rows:
        if verbose:
            print("  nothing stale to enrich")
        return stats

    if verbose:
        print(f"  {len(rows)} papers with a DOI to refresh")
    with _client() as client:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            dois = "|".join(f"https://doi.org/{r['doi']}" for r in chunk)
            try:
                data = get(client, {"filter": f"doi:{dois}", "per-page": BATCH})
            except RuntimeError as e:
                # Throttling can last hours. Everything fetched so far is
                # already committed, and the next run resumes from here because
                # rows are selected by how stale `citations_updated_at` is.
                con.commit()
                stats["stopped_early"] = str(e)
                print(f"  stopped after {stats['fetched']} of {len(rows)}: {e}\n"
                      f"  progress is saved; re-run --enrich later to continue",
                      file=sys.stderr, flush=True)
                return stats
            for work in data.get("results", []):
                rec = to_record(work)
                if not rec:
                    continue
                stats["fetched"] += 1
                _, action = upsert_paper(con, rec)
                stats["inserted" if action == "insert" else "merged"] += 1
            con.commit()
            if verbose and (i // BATCH) % 10 == 0:
                print(f"    {min(i + BATCH, len(rows))}/{len(rows)}", flush=True)
            time.sleep(SLEEP)
    return stats


def discover(con, since: str, until: str | None = None, only: str | None = None,
             limit: int | None = None, max_per_theme: int = 600,
             dry_run: bool = False, verbose: bool = True,
             force: bool = False) -> dict:
    cfg = load_config()
    stats: dict = {"fetched": 0, "inserted": 0, "merged": 0, "failed": []}
    with _client() as client:
        for slug, filt in discover_filters(cfg, since, until, only):
            done_key = f"openalex_done:{slug}:{since}"
            if not force and get_meta(con, done_key):
                if verbose:
                    print(f"  [{slug}] already done, skipping", flush=True)
                continue
            try:
                got = _discover_theme(con, client, slug, filt, stats, limit,
                                      max_per_theme, dry_run, verbose)
            except (RuntimeError, httpx.HTTPError) as e:
                # Losing one theme is survivable; losing the eleven queued behind
                # it is not, so the run continues and reports what failed.
                stats["failed"].append(slug)
                print(f"  [{slug}] FAILED: {e}", file=sys.stderr, flush=True)
                continue
            if not dry_run:
                set_meta(con, done_key, today())
                con.commit()
            if verbose:
                print(f"    kept {got}", flush=True)
            if limit and stats["fetched"] >= limit:
                break
    return stats


def _discover_theme(con, client, slug, filt, stats, limit, max_per_theme,
                    dry_run, verbose) -> int:
    """Page through one theme. Returns how many records it contributed."""
    cursor, got = "*", 0
    first = True
    while cursor:
        # Most-cited first, so the per-theme cap keeps the papers that matter
        # rather than an arbitrary slice of the tail.
        data = get(client, {"filter": filt, "per-page": PAGE,
                            "cursor": cursor, "sort": "cited_by_count:desc"})
        if first and verbose:
            print(f"  [{slug}] {data['meta']['count']} matches", flush=True)
            first = False
        for work in data.get("results", []):
            rec = to_record(work)
            if not rec:
                continue
            stats["fetched"] += 1
            got += 1
            if not dry_run:
                _, action = upsert_paper(con, rec)
                stats["inserted" if action == "insert" else "merged"] += 1
            if limit and stats["fetched"] >= limit:
                if not dry_run:
                    con.commit()
                return got
        if not dry_run:
            con.commit()
        cursor = data["meta"].get("next_cursor")
        if got >= max_per_theme or not data.get("results"):
            break
        time.sleep(SLEEP)
    return got


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Ingest / enrich from OpenAlex")
    ap.add_argument("--enrich", action="store_true",
                    help="refresh citations for papers that have a DOI")
    ap.add_argument("--discover", action="store_true",
                    help="theme-driven search for non-arXiv work")
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--until")
    ap.add_argument("--theme", help="restrict discover to one theme slug")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-per-theme", type=int, default=600)
    ap.add_argument("--stale-days", type=int, default=30)
    ap.add_argument("--force", action="store_true",
                    help="redo themes already recorded as done")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not (args.enrich or args.discover):
        print("need --enrich or --discover", file=sys.stderr)
        return 2

    con = connect()
    verbose = not args.quiet
    total = {"fetched": 0, "inserted": 0, "merged": 0}

    if args.discover:
        print(f"OpenAlex discover since {args.since}", flush=True)
        with IngestRun(con, "openalex:discover") as log:
            s = discover(con, args.since, args.until, args.theme, args.limit,
                         args.max_per_theme, args.dry_run, verbose, args.force)
            log.fetched, log.inserted, log.merged = s["fetched"], s["inserted"], s["merged"]
        for k in total:
            total[k] += s.get(k, 0)
        if s.get("failed"):
            print("  themes that failed: " + ", ".join(s["failed"]), file=sys.stderr)

    if args.enrich:
        print("OpenAlex enrich", flush=True)
        with IngestRun(con, "openalex:enrich") as log:
            s = enrich(con, args.limit, args.stale_days, verbose)
            log.fetched, log.inserted, log.merged = s["fetched"], s["inserted"], s["merged"]
        for k in total:
            total[k] += s.get(k, 0)

    print(f"fetched {total['fetched']}, inserted {total['inserted']}, "
          f"merged {total['merged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
