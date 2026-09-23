"""Shared plumbing: paths, sqlite access, normalization, paper dedupe/upsert.

Dedupe key priority is doi > arxiv_id > (title_key + first author). A paper
found on arXiv and again on OpenAlex collapses into one row whose
`sources_json` lists both, so the UI never shows the same work twice.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("GPT_DB", ROOT / "data" / "tracker.db"))
SCHEMA_PATH = ROOT / "db" / "schema.sql"
CONFIG_PATH = ROOT / "config" / "themes.yaml"

# Sent to the OpenAlex / Crossref / Unpaywall polite pools, which ask for a
# contact address and give faster, more reliable service in return. Set GPT_EMAIL
# to join those pools; without it the requests still work, anonymously.
CONTACT_EMAIL = os.environ.get("GPT_EMAIL", "").strip()
USER_AGENT = (f"bellwether/0.2 (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL
              else "bellwether/0.2")


# --------------------------------------------------------------- database ---
def connect(path: Path | str = DB_PATH, *, init: bool = True,
            same_thread: bool = True) -> sqlite3.Connection:
    """Open the database, creating it from schema.sql on first use.

    `same_thread=False` is for the API: FastAPI runs sync endpoints in a
    threadpool and can start a request on one thread and finish it on another,
    which sqlite refuses by default. Safe here because every request opens and
    closes its own connection, so one is never shared between two requests.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    con = sqlite3.connect(path, timeout=30, check_same_thread=same_thread)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    if init and (fresh or not _has_table(con, "papers")):
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        con.commit()
    elif init:
        _migrate(con)
    return con


# Columns added after the first databases were built. Each entry is applied
# only when missing, so an existing database upgrades in place.
_MIGRATIONS = (("repos", "stars_per_day", "REAL"),)

# Tables added after the first databases were built. Every statement in
# schema.sql is `CREATE TABLE IF NOT EXISTS`, so replaying the ones an old
# database lacks costs nothing and keeps one definition of each table.
_LATER_TABLES = ("trending", "alerts", "alert_hits")


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        if not _has_table(con, table):
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            con.commit()
    if any(not _has_table(con, t) for t in _LATER_TABLES):
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        con.commit()


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


class IngestRun:
    """Context manager that records one ingest pass in `ingest_log`."""

    def __init__(self, con: sqlite3.Connection, source: str):
        self.con, self.source = con, source
        self.fetched = self.inserted = self.merged = 0
        self.id = None

    def __enter__(self):
        cur = self.con.execute(
            "INSERT INTO ingest_log(source, started_at, status) VALUES(?,?,'running')",
            (self.source, datetime.now().isoformat(timespec="seconds")),
        )
        self.id = cur.lastrowid
        self.con.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.con.execute(
            "UPDATE ingest_log SET finished_at=?, fetched=?, inserted=?, merged=?, "
            "status=?, detail=? WHERE id=?",
            (
                datetime.now().isoformat(timespec="seconds"),
                self.fetched,
                self.inserted,
                self.merged,
                "error" if exc else "ok",
                f"{exc_type.__name__}: {exc}" if exc else None,
                self.id,
            ),
        )
        self.con.commit()
        return False


# ---------------------------------------------------------- normalization ---
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_LATEX = re.compile(r"\$[^$]*\$|\\[a-zA-Z]+\{?|[{}]")


def clean_text(s: str | None) -> str | None:
    if not s:
        return None
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("–", "-").replace("—", "-").replace("­", "")
    return _WS.sub(" ", s).strip() or None


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d if d.startswith("10.") else None


def norm_arxiv(aid: str | None) -> str | None:
    """`http://arxiv.org/abs/2101.12345v3` and `arXiv:2101.12345` both give
    `2101.12345`. Old-style ids such as `math/0605217` are kept intact."""
    if not aid:
        return None
    a = aid.strip().lower()
    a = re.sub(r"^(https?://)?(www\.)?arxiv\.org/(abs|pdf)/", "", a)
    a = re.sub(r"^arxiv:", "", a)
    a = re.sub(r"\.pdf$", "", a)
    a = re.sub(r"v\d+$", "", a)
    if re.fullmatch(r"\d{4}\.\d{4,5}", a) or re.fullmatch(r"[a-z-]+(\.[a-z]{2})?/\d{7}", a):
        return a
    return None


def _deaccent(s: str) -> str:
    """`Lévy` becomes `levy`, not `le vy`: combining marks are dropped, not
    turned into separators, so accented surnames still match."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def title_key(title: str | None) -> str:
    if not title:
        return ""
    t = _deaccent(title)
    t = _LATEX.sub(" ", t)
    t = _NON_ALNUM.sub(" ", t)
    return _WS.sub(" ", t).strip()


def last_name(author: str | None) -> str:
    if not author:
        return ""
    a = _NON_ALNUM.sub(" ", _deaccent(author)).strip()
    return a.split()[-1] if a else ""


def iso_date(value) -> str | None:
    """Accepts a datetime, date, ISO string, or arXiv timestamp; returns YYYY-MM-DD."""
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == "%Y":
                return f"{d.year}-01-01"
            if fmt == "%Y-%m":
                return f"{d.year}-{d.month:02d}-01"
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def today() -> str:
    return date.today().isoformat()


def utf8_stdout() -> None:
    """Windows consoles default to cp1252, which raises on an accented author
    name mid-ingest. Every entry point calls this before printing anything."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


# ------------------------------------------------------------ paper upsert ---
_PAPER_FIELDS = (
    "doi arxiv_id title_key title abstract authors_json first_author published_at "
    "updated_at year venue venue_type categories_json concepts_json citations "
    "citations_updated_at pdf_url landing_url oa_status has_pdf is_preprint sources_json"
).split()


def _record_to_row(rec: dict) -> dict:
    title = clean_text(rec.get("title")) or ""
    authors = [clean_text(a) for a in (rec.get("authors") or []) if clean_text(a)]
    published = iso_date(rec.get("published_at"))
    pdf_url = rec.get("pdf_url") or None
    return {
        "doi": norm_doi(rec.get("doi")),
        "arxiv_id": norm_arxiv(rec.get("arxiv_id")),
        "title_key": title_key(title),
        "title": title,
        "abstract": clean_text(rec.get("abstract")),
        "authors_json": json.dumps(authors, ensure_ascii=False),
        "first_author": authors[0] if authors else None,
        "published_at": published,
        "updated_at": iso_date(rec.get("updated_at")) or published,
        "year": int(published[:4]) if published else None,
        "venue": clean_text(rec.get("venue")),
        "venue_type": rec.get("venue_type"),
        "categories_json": json.dumps(rec.get("categories") or [], ensure_ascii=False),
        "concepts_json": json.dumps(rec.get("concepts") or [], ensure_ascii=False),
        "citations": int(rec.get("citations") or 0),
        "citations_updated_at": rec.get("citations_updated_at"),
        "pdf_url": pdf_url,
        "landing_url": rec.get("landing_url"),
        "oa_status": rec.get("oa_status"),
        "has_pdf": 1 if pdf_url else 0,
        "is_preprint": 1 if rec.get("is_preprint") else 0,
        "sources_json": json.dumps([rec["source"]]),
    }


def find_paper(con: sqlite3.Connection, row: dict) -> int | None:
    if row["doi"]:
        hit = con.execute("SELECT id FROM papers WHERE doi=?", (row["doi"],)).fetchone()
        if hit:
            return hit["id"]
    if row["arxiv_id"]:
        hit = con.execute(
            "SELECT id FROM papers WHERE arxiv_id=?", (row["arxiv_id"],)
        ).fetchone()
        if hit:
            return hit["id"]
    tk = row["title_key"]
    if not tk or len(tk) < 8:
        return None
    # Short titles collide easily ("Optimal Execution"), so how much corroboration
    # a title match needs scales with how distinctive the title is. Long titles
    # merge on their own; short ones ("Deep Hedging") need the same first author
    # and a publication date within three years, which is the normal gap between
    # a preprint and its journal version.
    want_author = last_name(row.get("first_author"))
    want_year = row.get("year")
    distinctive = len(tk) >= 25
    for hit in con.execute(
        "SELECT id, first_author, year FROM papers WHERE title_key=?", (tk,)
    ):
        have_author = last_name(hit["first_author"])
        author_ok = bool(want_author) and want_author == have_author
        author_unknown = not want_author or not have_author
        year_ok = (
            want_year is None
            or hit["year"] is None
            or abs(want_year - hit["year"]) <= 3
        )
        if distinctive and (author_ok or author_unknown) and year_ok:
            return hit["id"]
        if not distinctive and author_ok and year_ok:
            return hit["id"]
    return None


def _merge_values(old: sqlite3.Row, new: dict) -> dict:
    """Field-level merge policy for a paper seen in more than one place."""
    out: dict = {}

    def keep_longer(field):
        a, b = old[field], new.get(field)
        if b and (not a or len(b) > len(a)):
            out[field] = b

    def fill(field):
        if new.get(field) and not old[field]:
            out[field] = new[field]

    for f in ("doi", "arxiv_id", "venue", "venue_type", "oa_status", "landing_url"):
        fill(f)
    keep_longer("abstract")
    keep_longer("title")

    # Richer author list wins.
    if len(json.loads(new["authors_json"])) > len(json.loads(old["authors_json"])):
        out["authors_json"] = new["authors_json"]
        out["first_author"] = new["first_author"]

    # Earliest known appearance is the publication date; latest is the revision.
    if new.get("published_at") and (
        not old["published_at"] or new["published_at"] < old["published_at"]
    ):
        out["published_at"] = new["published_at"]
        out["year"] = new["year"]
    if new.get("updated_at") and (
        not old["updated_at"] or new["updated_at"] > old["updated_at"]
    ):
        out["updated_at"] = new["updated_at"]

    # An open-access PDF beats no PDF; an existing one is left alone.
    if new.get("pdf_url") and not old["pdf_url"]:
        out["pdf_url"] = new["pdf_url"]
        out["has_pdf"] = 1

    # Citation counts only ever come from a source that actually has them.
    if new.get("citations", 0) > 0 and new["citations"] != old["citations"]:
        out["citations"] = new["citations"]
        out["citations_updated_at"] = new.get("citations_updated_at") or today()

    for f in ("categories_json", "concepts_json"):
        merged = _union_json(old[f], new[f])
        if merged != old[f]:
            out[f] = merged

    # A work published in a journal stops being a preprint.
    if old["is_preprint"] and not new["is_preprint"]:
        out["is_preprint"] = 0

    out["sources_json"] = _union_json(old["sources_json"], new["sources_json"])
    return out


def _union_json(a: str | None, b: str | None) -> str:
    try:
        xs = list(json.loads(a or "[]"))
    except json.JSONDecodeError:
        xs = []
    try:
        ys = list(json.loads(b or "[]"))
    except json.JSONDecodeError:
        ys = []
    seen, out = set(), []
    for v in xs + ys:
        k = json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return json.dumps(out, ensure_ascii=False)


def upsert_paper(con: sqlite3.Connection, rec: dict) -> tuple[int, str]:
    """Insert or merge one paper record. Returns (paper_id, 'insert'|'merge')."""
    row = _record_to_row(rec)
    if not row["title"]:
        raise ValueError("paper record has no title")

    pid = find_paper(con, row)
    if pid is None:
        cols = ",".join(_PAPER_FIELDS)
        marks = ",".join("?" for _ in _PAPER_FIELDS)
        cur = con.execute(
            f"INSERT INTO papers({cols}) VALUES({marks})",
            [row[f] for f in _PAPER_FIELDS],
        )
        pid, action = int(cur.lastrowid or 0), "insert"
    else:
        old = con.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
        changes = _merge_values(old, row)
        changes["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ",".join(f"{k}=?" for k in changes)
        con.execute(
            f"UPDATE papers SET {sets} WHERE id=?", [*changes.values(), pid]
        )
        action = "merge"

    con.execute(
        "INSERT INTO paper_sources(paper_id, source, ext_id, url, pdf_url) "
        "VALUES(?,?,?,?,?) ON CONFLICT(source, ext_id) DO UPDATE SET "
        "paper_id=excluded.paper_id, url=excluded.url, pdf_url=excluded.pdf_url, "
        "fetched_at=datetime('now')",
        (pid, rec["source"], str(rec["ext_id"]), rec.get("landing_url"), rec.get("pdf_url")),
    )
    index_paper(con, pid)
    if row["citations"]:
        con.execute(
            "INSERT OR REPLACE INTO paper_snapshots(paper_id, day, citations) VALUES(?,?,?)",
            (pid, today(), row["citations"]),
        )
    return pid, action


def index_paper(con: sqlite3.Connection, pid: int) -> None:
    p = con.execute(
        "SELECT title, abstract, authors_json, venue FROM papers WHERE id=?", (pid,)
    ).fetchone()
    if not p:
        return
    authors = " ".join(json.loads(p["authors_json"]))
    con.execute("DELETE FROM papers_fts WHERE paper_id=?", (pid,))
    con.execute(
        "INSERT INTO papers_fts(title, abstract, authors, venue, paper_id) VALUES(?,?,?,?,?)",
        (p["title"], p["abstract"] or "", authors, p["venue"] or "", pid),
    )


# ------------------------------------------------------------------ config ---
def load_config() -> dict:
    """Read themes.yaml and fold the top-level `search_extra` map onto each
    theme, so callers see one self-contained dict per theme."""
    import yaml

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    extra = cfg.get("search_extra") or {}
    for theme in cfg.get("themes", []):
        theme["search_extra"] = extra.get(theme["slug"], [])
    return cfg
