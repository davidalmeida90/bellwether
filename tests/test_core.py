"""Offline checks for normalization, dedupe and merge. No network.

Run: py -3 tests/test_core.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import (  # noqa: E402
    connect,
    norm_arxiv,
    norm_doi,
    title_key,
    upsert_paper,
)

PASS, FAIL = [], []


def check(name: str, got, want) -> None:
    (PASS if got == want else FAIL).append((name, got, want))


def fresh_db():
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    return connect(tmp)


def test_normalizers() -> None:
    check("doi/url", norm_doi("https://doi.org/10.1080/14697688.2020.1817974"),
          "10.1080/14697688.2020.1817974")
    check("doi/prefix", norm_doi("DOI: 10.1016/J.JFINECO.2019.05.001"),
          "10.1016/j.jfineco.2019.05.001")
    check("doi/junk", norm_doi("not-a-doi"), None)
    check("arxiv/url+version", norm_arxiv("http://arxiv.org/abs/1802.03042v4"), "1802.03042")
    check("arxiv/prefix", norm_arxiv("arXiv:2101.12345"), "2101.12345")
    check("arxiv/old-style", norm_arxiv("math/0605217"), "math/0605217")
    check("arxiv/pdf", norm_arxiv("https://arxiv.org/pdf/2401.01234.pdf"), "2401.01234")
    check("arxiv/junk", norm_arxiv("W2790000"), None)
    check("titlekey/latex", title_key(r"Deep Hedging: $X_t$ under Frictions"),
          "deep hedging under frictions")
    check("titlekey/accents", title_key("Lévy Processes"), "levy processes")


def test_merge_preprint_and_journal() -> None:
    """Short title, no shared id: merges on author + nearby year."""
    con = fresh_db()
    upsert_paper(con, dict(
        source="arxiv", ext_id="1802.03042", arxiv_id="1802.03042", title="Deep Hedging",
        abstract="We present a framework for hedging.",
        authors=["Hans Buehler", "Lukas Gonon"], published_at="2018-02-08T00:00:00Z",
        categories=["q-fin.CP"], is_preprint=True,
        pdf_url="https://arxiv.org/pdf/1802.03042",
        landing_url="https://arxiv.org/abs/1802.03042"))
    _, action = upsert_paper(con, dict(
        source="openalex", ext_id="W2790000", doi="10.1080/14697688.2019.1571683",
        title="Deep Hedging",
        abstract="We present a framework for hedging derivatives under market frictions.",
        authors=["Hans Buehler", "Lukas Gonon", "Josef Teichmann", "Ben Wood"],
        published_at="2019-02-01", venue="Quantitative Finance", venue_type="journal",
        citations=412, concepts=["Hedge"], landing_url="https://doi.org/10.1080/x"))

    check("merge/action", action, "merge")
    check("merge/one row", con.execute("SELECT count(*) c FROM papers").fetchone()["c"], 1)

    p = con.execute("SELECT * FROM papers").fetchone()
    check("merge/doi kept", p["doi"], "10.1080/14697688.2019.1571683")
    check("merge/arxiv kept", p["arxiv_id"], "1802.03042")
    check("merge/venue", p["venue"], "Quantitative Finance")
    check("merge/citations", p["citations"], 412)
    check("merge/earliest date", p["published_at"], "2018-02-08")
    check("merge/latest revision", p["updated_at"], "2019-02-01")
    check("merge/richer abstract", p["abstract"].endswith("under market frictions."), True)
    check("merge/all authors", len(json.loads(p["authors_json"])), 4)
    check("merge/pdf kept", p["has_pdf"], 1)
    check("merge/no longer preprint", p["is_preprint"], 0)
    check("merge/both sources", sorted(json.loads(p["sources_json"])), ["arxiv", "openalex"])
    check("merge/source rows", con.execute(
        "SELECT count(*) c FROM paper_sources").fetchone()["c"], 2)
    check("merge/fts single", con.execute(
        "SELECT count(*) c FROM papers_fts WHERE papers_fts MATCH 'hedging'"
    ).fetchone()["c"], 1)


def test_no_false_merge() -> None:
    """Same short title, different author and era: must stay separate."""
    con = fresh_db()
    upsert_paper(con, dict(source="arxiv", ext_id="a1", arxiv_id="1001.00001",
                           title="Optimal Execution", authors=["Robert Almgren"],
                           published_at="2010-01-01"))
    _, action = upsert_paper(con, dict(source="arxiv", ext_id="a2", arxiv_id="2401.00002",
                                       title="Optimal Execution", authors=["Jane Smith"],
                                       published_at="2024-01-01"))
    check("nofalse/action", action, "insert")
    check("nofalse/two rows", con.execute("SELECT count(*) c FROM papers").fetchone()["c"], 2)


def test_idempotent() -> None:
    """Re-ingesting the same arXiv record twice changes nothing."""
    con = fresh_db()
    rec = dict(source="arxiv", ext_id="1802.03042", arxiv_id="1802.03042",
               title="Deep Hedging", authors=["Hans Buehler"], published_at="2018-02-08")
    upsert_paper(con, rec)
    _, action = upsert_paper(con, rec)
    check("idem/action", action, "merge")
    check("idem/one row", con.execute("SELECT count(*) c FROM papers").fetchone()["c"], 1)
    check("idem/one source", con.execute(
        "SELECT count(*) c FROM paper_sources").fetchone()["c"], 1)
    check("idem/one fts", con.execute(
        "SELECT count(*) c FROM papers_fts").fetchone()["c"], 1)


def main() -> int:
    for fn in (test_normalizers, test_merge_preprint_and_journal,
               test_no_false_merge, test_idempotent):
        fn()
    for name, got, want in FAIL:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
