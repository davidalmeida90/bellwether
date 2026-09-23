"""End-to-end API checks against a seeded temporary database. No network.

Run: py -3 tests/test_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The API reads DB_PATH at import time, so the temp database is announced first.
TMP = Path(tempfile.mkdtemp()) / "api_test.db"
os.environ["GPT_DB"] = str(TMP)

from fastapi.testclient import TestClient  # noqa: E402

from ingest.classify import compile_rules, classify_papers, sync_themes  # noqa: E402
from ingest.common import connect, load_config, upsert_paper  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def check_true(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail or cond, True))


SEED = [
    dict(source="arxiv", ext_id="1802.03042", arxiv_id="1802.03042",
         title="Deep Hedging",
         abstract="We present a framework for hedging derivatives with deep "
                  "reinforcement learning under transaction costs.",
         authors=["Hans Buehler", "Lukas Gonon"], published_at="2018-02-08",
         categories=["q-fin.CP"], is_preprint=True,
         pdf_url="https://arxiv.org/pdf/1802.03042",
         landing_url="https://arxiv.org/abs/1802.03042", oa_status="green"),
    dict(source="openalex", ext_id="W123", doi="10.1111/jofi.12345",
         title="The Cross Section of Expected Stock Returns Revisited",
         abstract="We revisit the cross section of stock returns and the value "
                  "premium using a factor model.",
         authors=["Eugene Fama", "Kenneth French"], published_at="2021-06-01",
         venue="Journal of Finance", venue_type="journal", citations=980,
         landing_url="https://doi.org/10.1111/jofi.12345", oa_status="closed"),
    dict(source="arxiv", ext_id="2201.09999", arxiv_id="2201.09999",
         title="Limit Order Book Dynamics and Price Impact at High Frequency",
         abstract="We study the limit order book, order flow and price impact "
                  "in high-frequency trading.",
         authors=["Rama Cont"], published_at="2022-01-25",
         categories=["q-fin.TR"], is_preprint=True,
         pdf_url="https://arxiv.org/pdf/2201.09999",
         landing_url="https://arxiv.org/abs/2201.09999", oa_status="green"),
]


def seed():
    con = connect(TMP)
    for rec in SEED:
        upsert_paper(con, rec)
    con.commit()
    cfg = load_config()
    sync_themes(con, cfg)
    classify_papers(con, compile_rules(cfg), only_new=False, verbose=False)
    con.close()


def main() -> int:
    seed()
    from api.main import app
    c = TestClient(app)

    # health and stats
    h = c.get("/health").json()
    check("health/ok", h["ok"], True)
    check("health/papers", h["papers"], 3)
    st = c.get("/api/stats").json()
    check("stats/papers", st["papers"], 3)
    check("stats/with pdf", st["papers_with_pdf"], 2)
    check_true("stats/sources", {s["source"] for s in st["sources"]} == {"arxiv", "openalex"})

    # themes carry counts
    themes = c.get("/api/themes").json()
    by = {t["slug"]: t for t in themes}
    check("themes/count", len(themes), 12)
    check_true("themes/microstructure tagged", by["market-microstructure"]["papers"] >= 1)
    check_true("themes/ml tagged", by["ml-for-finance"]["papers"] >= 1)

    # unfiltered listing, newest first
    r = c.get("/api/papers").json()
    check("papers/total", r["total"], 3)
    check("papers/sorted recent", r["items"][0]["published_at"], "2022-01-25")
    check_true("papers/themes attached", len(r["items"][0]["themes"]) >= 1)
    check_true("papers/authors parsed", r["items"][0]["authors"] == ["Rama Cont"])

    # search
    check("search/phrase", c.get("/api/papers", params={"q": '"limit order book"'}
                                 ).json()["total"], 1)
    check("search/prefix", c.get("/api/papers", params={"q": "hedg"}).json()["total"], 1)
    check("search/author", c.get("/api/papers", params={"q": "Fama"}).json()["total"], 1)
    check("search/two terms AND", c.get("/api/papers", params={"q": "deep hedging"}
                                        ).json()["total"], 1)
    check("search/no hits", c.get("/api/papers", params={"q": "zzzznotathing"}
                                  ).json()["total"], 0)
    # a raw FTS operator must not blow up the query
    bad = c.get("/api/papers", params={"q": 'volatility OR ("'})
    check("search/injection safe", bad.status_code, 200)

    # filters
    check("filter/theme", c.get("/api/papers", params={"themes": "market-microstructure"}
                                ).json()["total"], 1)
    check("filter/two themes", c.get(
        "/api/papers", params={"themes": "market-microstructure,ml-for-finance"}
    ).json()["total"], 2)
    check("filter/date from", c.get("/api/papers", params={"date_from": "2021-01-01"}
                                    ).json()["total"], 2)
    check("filter/date range", c.get(
        "/api/papers", params={"date_from": "2021-01-01", "date_to": "2021-12-31"}
    ).json()["total"], 1)
    check("filter/pdf only", c.get("/api/papers", params={"pdf_only": True}
                                   ).json()["total"], 2)
    check("filter/venue type", c.get("/api/papers", params={"venue_type": "journal"}
                                     ).json()["total"], 1)
    check("filter/min citations", c.get("/api/papers", params={"min_citations": 100}
                                        ).json()["total"], 1)

    # sorts
    check("sort/cited", c.get("/api/papers", params={"sort": "cited"}
                              ).json()["items"][0]["citations"], 980)
    check("sort/relevance", c.get(
        "/api/papers", params={"q": "returns", "sort": "relevance"}
    ).json()["total"], 1)

    # paging
    p1 = c.get("/api/papers", params={"per_page": 2, "page": 1}).json()
    p2 = c.get("/api/papers", params={"per_page": 2, "page": 2}).json()
    check("page/first size", len(p1["items"]), 2)
    check("page/second size", len(p2["items"]), 1)
    check_true("page/no overlap",
               not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}))
    check("page/cap enforced", c.get("/api/papers", params={"per_page": 5000}).status_code, 422)

    # detail
    pid = r["items"][0]["id"]
    d = c.get(f"/api/papers/{pid}").json()
    check("detail/title", d["title"].startswith("Limit Order Book"), True)
    check_true("detail/source links", len(d["source_links"]) == 1)
    check_true("detail/themes labelled", all("label" in t for t in d["themes"]))
    check("detail/missing", c.get("/api/papers/999999").status_code, 404)

    # save, note, reel flag, then unsave
    check("save/post", c.post("/api/saved", json={
        "item_type": "paper", "item_id": pid, "saved": True,
        "note": "reel candidate", "reel_flag": True}).status_code, 200)
    check("save/reflected", c.get(f"/api/papers/{pid}").json()["is_saved"], True)
    check("save/note kept", c.get(f"/api/papers/{pid}").json()["note"], "reel candidate")
    check("save/filter", c.get("/api/papers", params={"saved_only": True}
                               ).json()["total"], 1)
    c.post("/api/saved", json={"item_type": "paper", "item_id": pid, "saved": False})
    check("save/removed", c.get("/api/papers", params={"saved_only": True}
                                ).json()["total"], 0)
    check("save/bad type", c.post("/api/saved", json={
        "item_type": "banana", "item_id": 1}).status_code, 400)

    # hide takes an item out of every listing
    c.post("/api/hidden", json={"item_type": "paper", "item_id": pid, "hidden": True})
    check("hide/excluded", c.get("/api/papers").json()["total"], 2)
    c.post("/api/hidden", json={"item_type": "paper", "item_id": pid, "hidden": False})
    check("hide/restored", c.get("/api/papers").json()["total"], 3)

    # repo endpoints answer sanely with an empty table
    check("repos/empty", c.get("/api/repos").json()["total"], 0)
    check("repos/missing", c.get("/api/repos/1").status_code, 404)
    check("languages/empty", c.get("/api/languages").json(), [])

    for name, got, want in FAIL:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
