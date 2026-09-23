"""FastAPI app. Reads the SQLite database that the ingest scripts fill.

Ingestion and serving are deliberately separate processes: the API never calls
an external service, so every page load is a local index lookup and the whole
thing keeps working with no network.

  py -3 serve.py           # or: uvicorn api.main:app --reload
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import queries as Q  # noqa: E402
from ingest.common import DB_PATH, connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

app = FastAPI(title="Bellwether", version="0.2",
              description="Quant finance papers, repositories and the GitHub trending boards")


def db():
    con = connect(DB_PATH, same_thread=False)
    try:
        yield con
    finally:
        con.close()


# ------------------------------------------------------------------ papers ---
@app.get("/api/papers")
def api_papers(
    q: str | None = None,
    themes: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "recent",
    pdf_only: bool = False,
    venue_type: str | None = None,
    saved_only: bool = False,
    min_citations: int = 0,
    page: int = 1,
    per_page: int = Query(50, le=100),
    con=Depends(db),
):
    return Q.search_papers(con, q=q, themes=themes, date_from=date_from,
                           date_to=date_to, sort=sort, pdf_only=pdf_only,
                           venue_type=venue_type, saved_only=saved_only,
                           min_citations=min_citations, page=page, per_page=per_page)


@app.get("/api/papers/{paper_id}")
def api_paper(paper_id: int, con=Depends(db)):
    paper = Q.get_paper(con, paper_id)
    if not paper:
        raise HTTPException(404, "paper not found")
    return paper


# ------------------------------------------------------------------- repos ---
@app.get("/api/repos")
def api_repos(
    q: str | None = None,
    themes: str | None = None,
    sort: str = "popular",
    language: str | None = None,
    min_stars: int = 0,
    saved_only: bool = False,
    page: int = 1,
    per_page: int = Query(50, le=100),
    con=Depends(db),
):
    return Q.search_repos(con, q=q, themes=themes, sort=sort, language=language,
                          min_stars=min_stars, saved_only=saved_only,
                          page=page, per_page=per_page)


@app.get("/api/repos/{repo_id}")
def api_repo(repo_id: int, con=Depends(db)):
    repo = Q.get_repo(con, repo_id)
    if not repo:
        raise HTTPException(404, "repo not found")
    return repo


# --------------------------------------------------------------- trending ---
@app.get("/api/trending")
def api_trending(
    day: str | None = None,
    window: str = "daily",
    board: str = "",
    theme: str | None = None,
    q: str | None = None,
    sort: str = "rank",
    new_only: bool = False,
    page: int = 1,
    per_page: int = Query(50, le=100),
    con=Depends(db),
):
    return Q.trending(con, day=day, window=window, board=board, theme=theme, q=q,
                      sort=sort, new_only=new_only, page=page, per_page=per_page)


@app.get("/api/trending/meta")
def api_trending_meta(con=Depends(db)):
    # Labels live in the YAML taxonomy rather than in the database, so the theme
    # list is read from there and the counts come from the boards.
    from ingest.trending import load_trending_config

    themes = [{"slug": t["slug"], "label": t["label"]}
              for t in load_trending_config()["themes"]]
    themes.append({"slug": "other", "label": "Other"})
    return Q.trending_meta(con, themes)


@app.get("/api/trending/history")
def api_trending_history(full_name: str, con=Depends(db)):
    return {"full_name": full_name, "history": Q.trending_history(con, full_name)}


# ------------------------------------------------------------------ alerts ---
@app.get("/api/alerts")
def api_alerts(con=Depends(db)):
    return {"alerts": Q.alerts(con), "hits": Q.alert_hits(con, limit=60)}


@app.post("/api/alerts")
def api_add_alert(payload: dict = Body(...), con=Depends(db)):
    try:
        return Q.add_alert(con, payload.get("label", ""), payload.get("theme", ""),
                           payload.get("keyword", ""), payload.get("min_stars", 0),
                           payload.get("board", ""), payload.get("desktop", True))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/alerts/{alert_id}")
def api_update_alert(alert_id: int, payload: dict = Body(default={}), con=Depends(db)):
    return Q.update_alert(con, alert_id, active=payload.get("active"),
                          delete=bool(payload.get("delete")))


@app.post("/api/alerts/seen")
def api_alerts_seen(payload: dict = Body(default={}), con=Depends(db)):
    return Q.mark_alerts_seen(con, payload.get("alert_id"))


# ------------------------------------------------------------------ shared ---
@app.get("/api/themes")
def api_themes(con=Depends(db)):
    return Q.themes_with_counts(con)


@app.get("/api/stats")
def api_stats(con=Depends(db)):
    return Q.stats(con)


@app.get("/api/languages")
def api_languages(con=Depends(db)):
    return Q.languages(con)


@app.post("/api/saved")
def api_save(payload: dict = Body(...), con=Depends(db)):
    try:
        return Q.set_saved(con, payload["item_type"], int(payload["item_id"]),
                           bool(payload.get("saved", True)), payload.get("note"),
                           payload.get("reel_flag"))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/hidden")
def api_hide(payload: dict = Body(...), con=Depends(db)):
    try:
        return Q.set_hidden(con, payload["item_type"], int(payload["item_id"]),
                            bool(payload.get("hidden", True)))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/health")
def health(con=Depends(db)):
    n = con.execute("SELECT count(*) FROM papers").fetchone()[0]
    return {"ok": True, "db": str(DB_PATH), "papers": n}


# --------------------------------------------------------------------- web ---
if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")
