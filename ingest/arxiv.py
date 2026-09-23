"""arXiv ingest.

Two modes over the same Atom API:

  backfill      walks month windows between --since and --until
  incremental   picks up where the last run stopped, with a 7 day overlap so
                nothing slips through a window boundary

Windowing by month matters: arXiv caps paging at 30k results per query, and a
single unwindowed q-fin sweep would blow past that. Month windows keep every
query in the low hundreds.

arXiv publishes the DOI and journal reference of the published version when one
exists, so records arrive already carrying the key that later merges them with
the OpenAlex copy.

  py -3 ingest/arxiv.py --since 2018-01-01
  py -3 ingest/arxiv.py --incremental
  py -3 ingest/arxiv.py --since 2024-01-01 --until 2024-02-01 --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from defusedxml.ElementTree import fromstring as xml_fromstring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import (  # noqa: E402
    USER_AGENT,
    IngestRun,
    connect,
    get_meta,
    set_meta,
    today,
    utf8_stdout,
    upsert_paper,
)

API = "https://export.arxiv.org/api/query"
PAGE = 200
SLEEP = 3.0  # arXiv asks for one request every three seconds
MAX_RETRIES = 5

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}

# Everything in q-fin is in scope by definition.
QFIN_CATS = [
    "q-fin.CP", "q-fin.EC", "q-fin.GN", "q-fin.MF", "q-fin.PM",
    "q-fin.PR", "q-fin.RM", "q-fin.ST", "q-fin.TR",
]

# Adjacent categories carry a lot of finance work that never gets cross-listed
# to q-fin, so they come in only when the abstract is clearly financial.
ADJACENT_CATS = ["cs.LG", "stat.ML", "stat.AP", "econ.EM", "cs.CE"]
ADJACENT_TERMS = [
    "financial market", "stock return", "stock price", "asset pricing",
    "portfolio", "trading strategy", "limit order book", "volatility",
    "option pricing", "derivative pricing", "credit risk", "hedging",
    "cryptocurrency", "market microstructure", "algorithmic trading",
    "risk premium", "yield curve", "hedge fund", "high-frequency trading",
]


def _cat_clause(cats: list[str]) -> str:
    return "(" + " OR ".join(f"cat:{c}" for c in cats) + ")"


def _terms_clause(terms: list[str]) -> str:
    return "(" + " OR ".join(f'abs:"{t}"' for t in terms) + ")"


def build_queries() -> list[tuple[str, str]]:
    return [
        ("q-fin", _cat_clause(QFIN_CATS)),
        ("adjacent", f"{_cat_clause(ADJACENT_CATS)} AND {_terms_clause(ADJACENT_TERMS)}"),
    ]


def month_windows(since: date, until: date):
    """Yield (start, end) month-long spans covering [since, until)."""
    cur = date(since.year, since.month, 1)
    while cur < until:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        yield max(cur, since), min(nxt, until)
        cur = nxt


def _stamp(d: date, end: bool = False) -> str:
    return d.strftime("%Y%m%d") + ("2359" if end else "0000")


def fetch_page(client: httpx.Client, query: str, start: int) -> ET.Element:
    """One API page, with backoff. arXiv answers 503 under load and sometimes
    returns an empty body, both of which are retried rather than fatal."""
    params = {
        "search_query": query,
        "start": start,
        "max_results": PAGE,
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    delay = SLEEP
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = client.get(API, params=params, timeout=90)
            if r.status_code in (429, 503):
                raise httpx.HTTPStatusError(f"http {r.status_code}", request=r.request,
                                            response=r)
            r.raise_for_status()
            if not r.text.strip():
                raise ValueError("empty body")
            # defusedxml: the stdlib parser is open to entity-expansion attacks
            return xml_fromstring(r.text)
        except (httpx.HTTPError, ET.ParseError, ValueError) as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"arXiv request failed after {MAX_RETRIES} tries: {last_err}")


def _text(el: ET.Element, path: str) -> str | None:
    node = el.find(path, NS)
    return node.text.strip() if node is not None and node.text else None


def parse_entry(entry: ET.Element) -> dict | None:
    raw_id = _text(entry, "a:id")
    title = _text(entry, "a:title")
    if not raw_id or not title:
        return None

    bare = raw_id.rsplit("/abs/", 1)[-1]
    bare_noversion = bare.rsplit("v", 1)[0] if "v" in bare.split("/")[-1] else bare

    cats = [c.get("term") for c in entry.findall("a:category", NS) if c.get("term")]
    authors = [a.text.strip() for a in entry.findall("a:author/a:name", NS) if a.text]
    journal_ref = _text(entry, "arxiv:journal_ref")

    return {
        "source": "arxiv",
        "ext_id": bare_noversion,
        "arxiv_id": bare_noversion,
        "doi": _text(entry, "arxiv:doi"),
        "title": title,
        "abstract": _text(entry, "a:summary"),
        "authors": authors,
        "published_at": _text(entry, "a:published"),
        "updated_at": _text(entry, "a:updated"),
        "categories": cats,
        "venue": journal_ref,
        "venue_type": "journal" if journal_ref else "preprint",
        "is_preprint": not journal_ref,
        # arXiv is a green open-access repository, so the PDF is always free.
        "pdf_url": f"https://arxiv.org/pdf/{bare_noversion}",
        "landing_url": f"https://arxiv.org/abs/{bare_noversion}",
        "oa_status": "green",
    }


def fetch_window(client: httpx.Client, query: str, start: date, end: date,
                 verbose: bool = True):
    """Yield every parsed entry for one query inside one date window."""
    windowed = f"{query} AND submittedDate:[{_stamp(start)} TO {_stamp(end, True)}]"
    offset, total = 0, None
    while True:
        root = fetch_page(client, windowed, offset)
        if total is None:
            node = root.find("os:totalResults", NS)
            total = int(node.text) if node is not None and node.text else 0
            if verbose:
                print(f"    {start} .. {end}  {total} results", flush=True)
        entries = root.findall("a:entry", NS)
        if not entries:
            return
        for e in entries:
            rec = parse_entry(e)
            if rec:
                yield rec
        offset += len(entries)
        if offset >= total:
            return
        time.sleep(SLEEP)


def run(con, since: date, until: date, dry_run: bool = False,
        limit: int | None = None, verbose: bool = True) -> dict:
    stats = {"fetched": 0, "inserted": 0, "merged": 0}
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for name, query in build_queries():
            if verbose:
                print(f"  [{name}]", flush=True)
            for w_start, w_end in month_windows(since, until):
                for rec in fetch_window(client, query, w_start, w_end, verbose):
                    stats["fetched"] += 1
                    if not dry_run:
                        _, action = upsert_paper(con, rec)
                        stats["inserted" if action == "insert" else "merged"] += 1
                    if limit and stats["fetched"] >= limit:
                        if not dry_run:
                            con.commit()
                        return stats
                if not dry_run:
                    con.commit()
                time.sleep(SLEEP)
    return stats


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Ingest arXiv papers")
    ap.add_argument("--since", help="start date YYYY-MM-DD")
    ap.add_argument("--until", help="end date YYYY-MM-DD, defaults to today")
    ap.add_argument("--incremental", action="store_true",
                    help="resume from the last run, with a 7 day overlap")
    ap.add_argument("--limit", type=int, help="stop after N records, for testing")
    ap.add_argument("--dry-run", action="store_true", help="fetch but do not write")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    con = connect()
    until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else date.today()

    if args.incremental:
        last = get_meta(con, "arxiv_last_date")
        if not last:
            print("no previous run recorded; use --since first", file=sys.stderr)
            return 2
        since = datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=7)
    elif args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        print("need --since or --incremental", file=sys.stderr)
        return 2

    print(f"arXiv ingest {since} -> {until}"
          f"{' (dry run)' if args.dry_run else ''}", flush=True)
    with IngestRun(con, "arxiv") as run_log:
        stats = run(con, since, until, args.dry_run, args.limit, not args.quiet)
        run_log.fetched = stats["fetched"]
        run_log.inserted = stats["inserted"]
        run_log.merged = stats["merged"]
    if not args.dry_run:
        set_meta(con, "arxiv_last_date", today())
        con.commit()
    print(f"fetched {stats['fetched']}, inserted {stats['inserted']}, "
          f"merged {stats['merged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
