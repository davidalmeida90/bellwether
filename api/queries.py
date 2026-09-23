"""SQL behind the API. Read-only apart from the small user-state writes.

Everything is parameterised. The one place user text reaches a query language
is the FTS5 MATCH expression, which is why `fts_query` rebuilds it from
scratch out of extracted terms rather than passing the raw string through.
"""

from __future__ import annotations

import json
import re
import sqlite3

MAX_PER_PAGE = 100
_PHRASE = re.compile(r'"([^"]+)"')
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.-]*")

PAPER_SORTS = {
    "recent": "p.published_at DESC, p.id DESC",
    "revised": "p.updated_at DESC, p.id DESC",
    "cited": "p.citations DESC, p.published_at DESC",
    "relevance": "rank",          # only meaningful with a search term
}
REPO_SORTS = {
    "popular": "r.stars DESC",
    # Real 30 day momentum ranks first; repos without enough history yet fall
    # back to their lifetime average rather than dropping out of the sort.
    "growing": "COALESCE(r.momentum, -1) DESC, COALESCE(r.stars_per_day, 0) DESC, r.stars DESC",
    "active": "r.pushed_at DESC",
    "new": "r.first_seen_at DESC, r.created_at DESC",
    "relevance": "rank",
}


def fts_query(text: str | None) -> str | None:
    """Build a safe FTS5 expression. Quoted runs stay phrases, bare words get a
    prefix wildcard so typing `volatil` already matches. Anything that is not a
    word character is dropped, so no user input can reach FTS as syntax."""
    if not text or not text.strip():
        return None
    parts = []
    rest = text
    for phrase in _PHRASE.findall(text):
        words = _WORD.findall(phrase)
        if words:
            parts.append('"' + " ".join(words) + '"')
        rest = rest.replace(f'"{phrase}"', " ")
    for word in _WORD.findall(rest):
        if len(word) >= 2:
            parts.append(f'"{word}"*')
    return " AND ".join(parts) if parts else None


def _theme_list(themes: str | list[str] | None) -> list[str]:
    if not themes:
        return []
    if isinstance(themes, str):
        themes = themes.split(",")
    return [t.strip() for t in themes if t and t.strip()]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for key, target in (("authors_json", "authors"), ("categories_json", "categories"),
                        ("concepts_json", "concepts"), ("sources_json", "sources"),
                        ("topics_json", "topics")):
        if key in d:
            try:
                d[target] = json.loads(d.pop(key) or "[]")
            except json.JSONDecodeError:
                d[target] = []
    return d


# ------------------------------------------------------------------ papers ---
def search_papers(con, *, q=None, themes=None, date_from=None, date_to=None,
                  sort="recent", pdf_only=False, venue_type=None, saved_only=False,
                  min_citations=0, page=1, per_page=50) -> dict:
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))
    page = max(1, int(page))
    match = fts_query(q)
    order = PAPER_SORTS.get(sort, PAPER_SORTS["recent"])
    if order == "rank" and not match:
        order = PAPER_SORTS["recent"]

    joins, where, params = [], ["p.hidden = 0"], {}

    if match:
        joins.append("JOIN papers_fts f ON f.paper_id = p.id")
        where.append("papers_fts MATCH :match")
        params["match"] = match

    slugs = _theme_list(themes)
    if slugs:
        marks = ",".join(f":th{i}" for i in range(len(slugs)))
        joins.append(
            "JOIN item_themes it ON it.item_id = p.id AND it.item_type = 'paper' "
            f"AND it.theme_slug IN ({marks})"
        )
        params.update({f"th{i}": s for i, s in enumerate(slugs)})

    if saved_only:
        joins.append("JOIN saved sv ON sv.item_id = p.id AND sv.item_type = 'paper'")
    if date_from:
        where.append("p.published_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("p.published_at <= :date_to")
        params["date_to"] = date_to
    if pdf_only:
        where.append("p.has_pdf = 1")
    if venue_type:
        vts = _theme_list(venue_type)
        marks = ",".join(f":vt{i}" for i in range(len(vts)))
        where.append(f"p.venue_type IN ({marks})")
        params.update({f"vt{i}": v for i, v in enumerate(vts)})
    if min_citations:
        where.append("p.citations >= :min_cit")
        params["min_cit"] = int(min_citations)

    join_sql = " ".join(joins)
    where_sql = " AND ".join(where)

    total = con.execute(
        f"SELECT count(DISTINCT p.id) n FROM papers p {join_sql} WHERE {where_sql}",
        params,
    ).fetchone()["n"]

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = con.execute(
        f"""SELECT p.id, p.title, p.abstract, p.authors_json, p.published_at,
                   p.updated_at, p.year, p.venue, p.venue_type, p.citations,
                   p.pdf_url, p.landing_url, p.oa_status, p.has_pdf, p.doi,
                   p.arxiv_id, p.categories_json, p.sources_json,
                   EXISTS(SELECT 1 FROM saved s WHERE s.item_type='paper'
                          AND s.item_id=p.id) AS is_saved
            FROM papers p {join_sql}
            WHERE {where_sql}
            GROUP BY p.id
            ORDER BY {order}
            LIMIT :limit OFFSET :offset""",
        params,
    ).fetchall()

    items = []
    for r in rows:
        d = _row(r)
        d["abstract"] = (d["abstract"] or "")[:400]
        d["themes"] = [
            t["theme_slug"] for t in con.execute(
                "SELECT theme_slug FROM item_themes WHERE item_type='paper' "
                "AND item_id=? ORDER BY score DESC", (d["id"],))
        ]
        items.append(d)

    return {"total": total, "page": page, "per_page": per_page, "items": items}


def get_paper(con, paper_id: int) -> dict | None:
    row = con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["themes"] = [dict(t) for t in con.execute(
        "SELECT it.theme_slug, it.score, it.method, t.label FROM item_themes it "
        "JOIN themes t ON t.slug=it.theme_slug "
        "WHERE it.item_type='paper' AND it.item_id=? ORDER BY it.score DESC", (paper_id,))]
    d["source_links"] = [dict(s) for s in con.execute(
        "SELECT source, ext_id, url, pdf_url FROM paper_sources WHERE paper_id=?",
        (paper_id,))]
    d["citation_history"] = [dict(s) for s in con.execute(
        "SELECT day, citations FROM paper_snapshots WHERE paper_id=? ORDER BY day",
        (paper_id,))]
    d["repos"] = [dict(r) for r in con.execute(
        "SELECT r.id, r.full_name, r.stars, r.url, l.method, l.confidence "
        "FROM links l JOIN repos r ON r.id=l.repo_id "
        "WHERE l.paper_id=? ORDER BY l.confidence DESC, r.stars DESC", (paper_id,))]
    sv = con.execute("SELECT note, reel_flag FROM saved WHERE item_type='paper' "
                     "AND item_id=?", (paper_id,)).fetchone()
    d["is_saved"] = sv is not None
    d["note"] = sv["note"] if sv else None
    d["reel_flag"] = bool(sv["reel_flag"]) if sv else False
    return d


# ------------------------------------------------------------------- repos ---
def search_repos(con, *, q=None, themes=None, sort="popular", language=None,
                 min_stars=0, saved_only=False, page=1, per_page=50) -> dict:
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))
    page = max(1, int(page))
    match = fts_query(q)
    order = REPO_SORTS.get(sort, REPO_SORTS["popular"])
    if order == "rank" and not match:
        order = REPO_SORTS["popular"]

    joins, where, params = [], ["r.hidden = 0"], {}
    if match:
        joins.append("JOIN repos_fts f ON f.repo_id = r.id")
        where.append("repos_fts MATCH :match")
        params["match"] = match

    slugs = _theme_list(themes)
    if slugs:
        marks = ",".join(f":th{i}" for i in range(len(slugs)))
        joins.append(
            "JOIN item_themes it ON it.item_id = r.id AND it.item_type = 'repo' "
            f"AND it.theme_slug IN ({marks})")
        params.update({f"th{i}": s for i, s in enumerate(slugs)})

    if saved_only:
        joins.append("JOIN saved sv ON sv.item_id = r.id AND sv.item_type = 'repo'")
    if language:
        where.append("r.language = :language")
        params["language"] = language
    if min_stars:
        where.append("r.stars >= :min_stars")
        params["min_stars"] = int(min_stars)

    join_sql, where_sql = " ".join(joins), " AND ".join(where)
    total = con.execute(
        f"SELECT count(DISTINCT r.id) n FROM repos r {join_sql} WHERE {where_sql}",
        params).fetchone()["n"]

    params["limit"], params["offset"] = per_page, (page - 1) * per_page
    rows = con.execute(
        f"""SELECT r.id, r.full_name, r.owner, r.name, r.description, r.url,
                   r.homepage, r.language, r.stars, r.forks, r.created_at,
                   r.pushed_at, r.topics_json, r.license, r.stars_7d, r.stars_30d,
                   r.momentum, r.stars_per_day, r.spike,
                   EXISTS(SELECT 1 FROM saved s WHERE s.item_type='repo'
                          AND s.item_id=r.id) AS is_saved
            FROM repos r {join_sql} WHERE {where_sql}
            GROUP BY r.id ORDER BY {order} LIMIT :limit OFFSET :offset""",
        params).fetchall()

    items = []
    for r in rows:
        d = _row(r)
        d["themes"] = [t["theme_slug"] for t in con.execute(
            "SELECT theme_slug FROM item_themes WHERE item_type='repo' AND item_id=? "
            "ORDER BY score DESC", (d["id"],))]
        items.append(d)
    return {"total": total, "page": page, "per_page": per_page, "items": items}


def get_repo(con, repo_id: int) -> dict | None:
    row = con.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["themes"] = [dict(t) for t in con.execute(
        "SELECT it.theme_slug, it.score, it.method, t.label FROM item_themes it "
        "JOIN themes t ON t.slug=it.theme_slug "
        "WHERE it.item_type='repo' AND it.item_id=? ORDER BY it.score DESC", (repo_id,))]
    d["star_history"] = [dict(s) for s in con.execute(
        "SELECT day, stars FROM repo_snapshots WHERE repo_id=? ORDER BY day", (repo_id,))]
    d["papers"] = [dict(p) for p in con.execute(
        "SELECT p.id, p.title, p.year, p.citations, p.landing_url, l.method "
        "FROM links l JOIN papers p ON p.id=l.paper_id WHERE l.repo_id=? "
        "ORDER BY l.confidence DESC", (repo_id,))]
    sv = con.execute("SELECT note, reel_flag FROM saved WHERE item_type='repo' "
                     "AND item_id=?", (repo_id,)).fetchone()
    d["is_saved"] = sv is not None
    d["note"] = sv["note"] if sv else None
    return d


# ------------------------------------------------------------------ shared ---
def themes_with_counts(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        """SELECT t.slug, t.label, t.ord,
                  COALESCE(SUM(it.item_type='paper'), 0) AS papers,
                  COALESCE(SUM(it.item_type='repo'), 0)  AS repos
           FROM themes t LEFT JOIN item_themes it ON it.theme_slug = t.slug
           GROUP BY t.slug ORDER BY t.ord""")]


def stats(con) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "papers": one("SELECT count(*) FROM papers WHERE hidden=0"),
        "papers_with_pdf": one("SELECT count(*) FROM papers WHERE hidden=0 AND has_pdf=1"),
        "papers_with_citations": one("SELECT count(*) FROM papers WHERE citations>0"),
        "repos": one("SELECT count(*) FROM repos WHERE hidden=0"),
        "links": one("SELECT count(*) FROM links"),
        "saved": one("SELECT count(*) FROM saved"),
        "years": [dict(r) for r in con.execute(
            "SELECT year, count(*) n FROM papers WHERE year IS NOT NULL AND hidden=0 "
            "GROUP BY year ORDER BY year")],
        "sources": [dict(r) for r in con.execute(
            "SELECT source, count(*) n FROM paper_sources GROUP BY source ORDER BY n DESC")],
        "last_ingest": [dict(r) for r in con.execute(
            "SELECT source, finished_at, fetched, inserted, merged, status "
            "FROM ingest_log ORDER BY id DESC LIMIT 6")],
    }


def languages(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT language, count(*) n FROM repos WHERE language IS NOT NULL AND hidden=0 "
        "GROUP BY language ORDER BY n DESC LIMIT 25")]


# -------------------------------------------------------------- user state ---
def set_saved(con, item_type: str, item_id: int, saved: bool,
              note: str | None = None, reel_flag: bool | None = None) -> dict:
    if item_type not in ("paper", "repo"):
        raise ValueError("item_type must be paper or repo")
    if saved:
        con.execute(
            "INSERT INTO saved(item_type,item_id,note,reel_flag) VALUES(?,?,?,?) "
            "ON CONFLICT(item_type,item_id) DO UPDATE SET "
            "note=COALESCE(excluded.note, saved.note), "
            "reel_flag=COALESCE(excluded.reel_flag, saved.reel_flag)",
            (item_type, item_id, note, int(reel_flag) if reel_flag is not None else None))
    else:
        con.execute("DELETE FROM saved WHERE item_type=? AND item_id=?",
                    (item_type, item_id))
    con.commit()
    return {"item_type": item_type, "item_id": item_id, "saved": saved}


def set_hidden(con, item_type: str, item_id: int, hidden: bool) -> dict:
    table = {"paper": "papers", "repo": "repos"}.get(item_type)
    if not table:
        raise ValueError("item_type must be paper or repo")
    con.execute(f"UPDATE {table} SET hidden=? WHERE id=?", (int(hidden), item_id))
    con.commit()
    return {"item_type": item_type, "item_id": item_id, "hidden": hidden}


# ---------------------------------------------------------------- trending ---
TRENDING_SORTS = {
    "rank": "t.rank ASC",
    "gained": "COALESCE(t.stars_window, 0) DESC, t.rank ASC",
    "run": "t.days_seen DESC, t.rank ASC",
    "stars": "COALESCE(t.stars, 0) DESC",
}


def trending(con, *, day=None, window="daily", board="", theme=None, q=None,
             sort="rank", new_only=False, page=1, per_page=50) -> dict:
    """One board, one day. Defaults to the most recent day captured, so the
    interface opens on today without having to know today's date."""
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))
    page = max(1, int(page))
    day = day or con.execute(
        "SELECT max(day) FROM trending WHERE window=?", (window,)).fetchone()[0]
    if not day:
        return {"total": 0, "page": 1, "per_page": per_page, "items": [], "day": None}

    where = ["t.day = :day", "t.window = :window", "t.board = :board"]
    params = {"day": day, "window": window, "board": board or ""}
    if theme:
        where.append("t.themes_json LIKE :theme")
        params["theme"] = f'%"{theme}"%'
    if q and q.strip():
        where.append("(t.full_name LIKE :q OR t.description LIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if new_only:
        where.append("t.first_seen = t.day")
    where_sql = " AND ".join(where)
    order = TRENDING_SORTS.get(sort, TRENDING_SORTS["rank"])

    total = con.execute(
        f"SELECT count(*) n FROM trending t WHERE {where_sql}", params).fetchone()["n"]
    params["limit"], params["offset"] = per_page, (page - 1) * per_page
    rows = con.execute(
        f"""SELECT t.* FROM trending t WHERE {where_sql}
            ORDER BY {order} LIMIT :limit OFFSET :offset""", params).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        d["themes"] = json.loads(d.pop("themes_json") or "[]")
        d["topics"] = json.loads(d.pop("topics_json") or "[]")
        d["is_new"] = d["first_seen"] == d["day"]
        items.append(d)
    return {"total": total, "page": page, "per_page": per_page, "items": items, "day": day}


def trending_meta(con, themes: list[dict] | None = None) -> dict:
    """Days captured, boards available, and today's theme counts, in one call so
    the interface can draw its filters before the first board arrives."""
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT day FROM trending ORDER BY day DESC LIMIT 60")]
    boards = [r[0] for r in con.execute(
        "SELECT DISTINCT board FROM trending ORDER BY board")]
    counts: dict[str, int] = {}
    if days:
        for r in con.execute(
                "SELECT themes_json FROM trending WHERE day=? AND window='daily'",
                (days[0],)):
            for slug in json.loads(r["themes_json"] or "[]"):
                counts[slug] = counts.get(slug, 0) + 1
    return {"days": days, "boards": boards, "theme_counts": counts,
            "themes": themes or [], "latest": days[0] if days else None}


def trending_history(con, full_name: str) -> list[dict]:
    """Every day a repository has appeared, so a run on the board reads as a run."""
    return [dict(r) for r in con.execute(
        "SELECT day, window, board, rank, stars, stars_window FROM trending "
        "WHERE full_name=? ORDER BY day DESC, window, board", (full_name,))]


# ------------------------------------------------------------------ alerts ---
def alerts(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM alerts ORDER BY id"):
        d = dict(r)
        d["unread"] = con.execute(
            "SELECT count(*) FROM alert_hits WHERE alert_id=? AND seen=0",
            (r["id"],)).fetchone()[0]
        out.append(d)
    return out


def add_alert(con, label: str, theme="", keyword="", min_stars=0, board="",
              desktop=True) -> dict:
    label = (label or "").strip()
    if not label:
        raise ValueError("an alert needs a label")
    cur = con.execute(
        "INSERT INTO alerts(label, theme, keyword, min_stars, board, desktop) "
        "VALUES(?,?,?,?,?,?)",
        (label[:80], theme or "", (keyword or "").strip()[:80], int(min_stars or 0),
         board or "", int(bool(desktop))))
    con.commit()
    return {"id": cur.lastrowid}


def update_alert(con, alert_id: int, *, active=None, delete=False) -> dict:
    if delete:
        con.execute("DELETE FROM alert_hits WHERE alert_id=?", (alert_id,))
        con.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        con.commit()
        return {"deleted": alert_id}
    if active is not None:
        con.execute("UPDATE alerts SET active=? WHERE id=?",
                    (int(bool(active)), alert_id))
        con.commit()
    return {"id": alert_id}


def alert_hits(con, *, unread_only=False, limit=100) -> list[dict]:
    sql = ("SELECT h.*, a.label FROM alert_hits h JOIN alerts a ON a.id = h.alert_id "
           + ("WHERE h.seen = 0 " if unread_only else "")
           + "ORDER BY h.day DESC, h.id DESC LIMIT ?")
    out = []
    for r in con.execute(sql, (int(limit),)):
        d = dict(r)
        d["themes"] = json.loads(d.pop("themes_json") or "[]")
        out.append(d)
    return out


def mark_alerts_seen(con, alert_id: int | None = None) -> dict:
    sql = "UPDATE alert_hits SET seen=1 WHERE seen=0"
    params: tuple = ()
    if alert_id:
        sql += " AND alert_id=?"
        params = (alert_id,)
    n = con.execute(sql, params).rowcount
    con.commit()
    return {"marked": n}
