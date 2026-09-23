"""Offline checks for the three source adapters, against captured payloads.

No network: every fixture below is a trimmed copy of a real response.

Run: py -3 tests/test_sources.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.arxiv import month_windows, parse_entry  # noqa: E402
from ingest.github import (  # noqa: E402
    _ARXIV_IN_TEXT,
    anchor,
    build_search_queries,
    gate,
    readme_excerpt,
)
from ingest.common import load_config  # noqa: E402
from ingest.openalex import (  # noqa: E402
    _expand,
    reconstruct_abstract,
    theme_phrases,
    to_record,
)

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def check_true(name, cond):
    (PASS if cond else FAIL).append((name, cond, True))


# ------------------------------------------------------------------- arXiv ---
ARXIV_ENTRY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.06724v2</id>
    <title>Equity auction dynamics: latent liquidity models</title>
    <updated>2024-07-18T13:28:36Z</updated>
    <summary>Equity auctions display several distinctive characteristics.</summary>
    <category term="q-fin.TR" scheme="http://arxiv.org/schemas/atom"/>
    <category term="q-fin.ST" scheme="http://arxiv.org/schemas/atom"/>
    <published>2024-01-12T17:49:16Z</published>
    <arxiv:journal_ref>Quantitative Finance, 24(10), 1381--1398, 2024</arxiv:journal_ref>
    <author><name>Mohammed Salek</name></author>
    <author><name>Damien Challet</name></author>
    <arxiv:doi>10.1080/14697688.2024.2367680</arxiv:doi>
  </entry>
</feed>"""

ARXIV_PREPRINT = ARXIV_ENTRY.replace(
    "<arxiv:journal_ref>Quantitative Finance, 24(10), 1381--1398, 2024</arxiv:journal_ref>", ""
).replace("<arxiv:doi>10.1080/14697688.2024.2367680</arxiv:doi>", "")


def test_arxiv():
    ns = "{http://www.w3.org/2005/Atom}"
    entry = ET.fromstring(ARXIV_ENTRY).find(f"{ns}entry")
    rec = parse_entry(entry)
    check("arxiv/id stripped of version", rec["arxiv_id"], "2401.06724")
    check("arxiv/ext id", rec["ext_id"], "2401.06724")
    check("arxiv/doi", rec["doi"], "10.1080/14697688.2024.2367680")
    check("arxiv/categories", rec["categories"], ["q-fin.TR", "q-fin.ST"])
    check("arxiv/authors", rec["authors"], ["Mohammed Salek", "Damien Challet"])
    check("arxiv/published", rec["published_at"], "2024-01-12T17:49:16Z")
    check("arxiv/pdf is versionless", rec["pdf_url"], "https://arxiv.org/pdf/2401.06724")
    check("arxiv/green oa", rec["oa_status"], "green")
    # A journal_ref means the work is published, not a preprint any more.
    check("arxiv/journal venue type", rec["venue_type"], "journal")
    check("arxiv/not preprint", rec["is_preprint"], False)

    rec2 = parse_entry(ET.fromstring(ARXIV_PREPRINT).find(f"{ns}entry"))
    check("arxiv/preprint type", rec2["venue_type"], "preprint")
    check("arxiv/preprint flag", rec2["is_preprint"], True)
    check("arxiv/no doi", rec2["doi"], None)


def test_month_windows():
    w = list(month_windows(date(2024, 1, 15), date(2024, 4, 1)))
    check("windows/count", len(w), 3)
    check("windows/first clipped to since", w[0], (date(2024, 1, 15), date(2024, 2, 1)))
    check("windows/last clipped to until", w[-1], (date(2024, 3, 1), date(2024, 4, 1)))
    check("windows/single month", len(list(month_windows(date(2024, 5, 1), date(2024, 6, 1)))), 1)
    check("windows/contiguous", all(a[1] == b[0] for a, b in zip(w, w[1:])), True)


# ---------------------------------------------------------------- OpenAlex ---
OA_WORK = {
    "id": "https://openalex.org/W2790000",
    "doi": "https://doi.org/10.1080/14697688.2019.1571683",
    "display_name": "Deep hedging",
    "publication_date": "2019-02-21",
    "type": "article",
    "cited_by_count": 322,
    "open_access": {"is_oa": True, "oa_status": "green",
                    "oa_url": "https://arxiv.org/pdf/1802.03042"},
    "best_oa_location": {"pdf_url": "https://arxiv.org/pdf/1802.03042",
                         "landing_page_url": "https://arxiv.org/abs/1802.03042"},
    "primary_location": {"source": {"display_name": "Quantitative Finance"}},
    "authorships": [{"author": {"display_name": "Hans Buehler"}},
                    {"author": {"display_name": "Josef Teichmann"}}],
    "topics": [{"display_name": "Stochastic processes and financial applications"}],
    "abstract_inverted_index": {"We": [0], "present": [1], "a": [2], "framework": [3]},
    "locations": [{"landing_page_url": "http://arxiv.org/abs/1802.03042v3"}],
}

OA_SSRN = {
    "id": "https://openalex.org/W999", "doi": None,
    "display_name": "Factor Timing", "publication_date": "2021-03-01",
    "type": "preprint", "cited_by_count": 40,
    "open_access": {"oa_status": "closed"}, "best_oa_location": None,
    "primary_location": {"source": {"display_name": "SSRN Electronic Journal"}},
    "authorships": [], "topics": [], "abstract_inverted_index": None, "locations": [],
}


def test_openalex():
    rec = to_record(OA_WORK)
    check("oa/doi", rec["doi"], "https://doi.org/10.1080/14697688.2019.1571683")
    check("oa/arxiv from locations", rec["arxiv_id"], "1802.03042")
    check("oa/citations", rec["citations"], 322)
    check("oa/venue", rec["venue"], "Quantitative Finance")
    check("oa/venue type", rec["venue_type"], "journal")
    check("oa/pdf", rec["pdf_url"], "https://arxiv.org/pdf/1802.03042")
    check("oa/abstract rebuilt", rec["abstract"], "We present a framework")

    ssrn = to_record(OA_SSRN)
    check("oa/ssrn is working paper", ssrn["venue_type"], "working_paper")
    check("oa/closed has no pdf", ssrn["pdf_url"], None)
    check("oa/closed status", ssrn["oa_status"], "closed")

    check("oa/inverted index order",
          reconstruct_abstract({"hedging": [1], "Deep": [0], "works": [2]}),
          "Deep hedging works")
    check("oa/empty index", reconstruct_abstract({}), None)


def test_phrase_expansion():
    # A stem OpenAlex cannot match must be completed to a real word.
    check("phrase/stem completed", _expand("implied volatilit"), ["implied volatility"])
    # Spelling classes expand to both forms.
    check("phrase/sz class", sorted(_expand("portfolio optimi[sz]ation")),
          ["portfolio optimisation", "portfolio optimization"])
    # `[- ]` collapses to a space rather than doubling every query.
    check("phrase/hyphen class", _expand("value[- ]at[- ]risk"), ["value at risk"])
    # Groups expand to each alternative.
    check("phrase/group", sorted(_expand("risk premi(um|a)")),
          ["risk premia", "risk premium"])
    # Wildcards are dropped rather than guessed at.
    check("phrase/wildcard dropped", _expand("characteristics.{0,20}returns"), [])
    # Single bare words are too broad to search on their own.
    check("phrase/single word dropped", _expand("volatility"), [])

    cfg = load_config()
    vol = next(t for t in cfg["themes"] if t["slug"] == "volatility-options")
    phrases = theme_phrases(vol)
    check_true("phrase/extra merged first", phrases[0] == "implied volatility")
    check_true("phrase/no stems survive", not any(p.endswith("volatilit") for p in phrases))
    check_true("phrase/all lowercase words", all(p.replace(" ", "").isalpha() for p in phrases))
    check_true("phrase/every theme has some",
               all(theme_phrases(t) for t in cfg["themes"]))


# ------------------------------------------------------------------ GitHub ---
def test_github():
    cfg = load_config()
    ok = gate(cfg)
    base = dict(full_name="microsoft/qlib", stargazers_count=48000, fork=False,
                archived=False, pushed_at="2026-08-01T00:00:00Z",
                description="An AI-oriented quantitative investment platform",
                topics=["quant", "finance", "algorithmic-trading"])
    check("gh/keeps good repo", ok(base), True)
    check("gh/rejects low stars", ok({**base, "stargazers_count": 3}), False)
    check("gh/rejects stale", ok({**base, "pushed_at": "2019-01-01T00:00:00Z"}), False)
    check("gh/rejects fork", ok({**base, "fork": True}), False)
    check("gh/rejects archived", ok({**base, "archived": True}), False)
    check("gh/rejects awesome list",
          ok({**base, "full_name": "wilsonfreitas/awesome-quant"}), False)
    check("gh/rejects tutorial",
          ok({**base, "full_name": "someone/python-tutorial-finance"}), False)
    check("gh/handles missing pushed_at", ok({**base, "pushed_at": None}), True)

    # Finance anchor: generic topics collide across unrelated domains, so a
    # repo has to prove it is about finance before the rest of the gate runs.
    is_finance = anchor(cfg)
    check("anchor/keeps quant platform", is_finance(base), True)
    check("anchor/keeps by clear topic alone", is_finance(dict(
        full_name="user/thing", description=None,
        topics=["options-pricing"])), True)
    check("anchor/keeps by description alone", is_finance(dict(
        full_name="user/thing", description="Backtesting engine for equity strategies",
        topics=[])), True)
    # These three are real false positives the topic search actually returned.
    check("anchor/drops select box widget", is_finance(dict(
        full_name="Choices-js/Choices",
        description="A vanilla JS customisable select box / text input plugin",
        topics=["options", "dropdown"])), False)
    check("anchor/drops memory forensics", is_finance(dict(
        full_name="volatilityfoundation/volatility3",
        description="Volatility 3.0: volatile memory extraction framework",
        topics=["volatility", "memory-forensics"])), False)
    check("anchor/drops photon transport", is_finance(dict(
        full_name="fangq/mcx",
        description="Monte Carlo eXtreme: GPU-accelerated photon transport simulator",
        topics=["monte-carlo", "gpu"])), False)
    check("anchor/drops bare repo with no signal", is_finance(dict(
        full_name="user/thing", description=None, topics=[])), False)

    # Substring matches that actually happened before the terms were word
    # anchored. Each of these three shipped into the watchlist.
    check("anchor/defi does not match definition", is_finance(dict(
        full_name="huggingface/transformers",
        description="the model-definition framework for state-of-the-art machine learning",
        topics=["nlp", "pytorch"])), False)
    check("anchor/quant does not match quantization", is_finance(dict(
        full_name="hiyouga/LlamaFactory",
        description="Unified efficient fine-tuning of 100+ LLMs, quantization supported",
        topics=["llm", "quantization"])), False)
    check("anchor/broker does not match message broker", is_finance(dict(
        full_name="redis/redis",
        description="For developers building real-time data-driven applications",
        topics=["database", "cache"])), False)
    # A coin name alone is infrastructure, not quantitative finance.
    check("anchor/drops coin implementation", is_finance(dict(
        full_name="bitcoin/bitcoin", description="Bitcoin Core integration/staging tree",
        topics=["bitcoin", "c-plus-plus"])), False)
    check("anchor/keeps crypto trading tool", is_finance(dict(
        full_name="freqtrade/freqtrade", description="Free, open source crypto trading bot",
        topics=["crypto-trading", "trading-bot"])), True)
    # Real repos that must survive.
    for name, desc, tops in [
        ("QuantConnect/Lean", "Lean Algorithmic Trading Engine", ["algorithmic-trading"]),
        ("ranaroussi/yfinance", "Download market data from Yahoo Finance API", []),
        ("robertmartin8/PyPortfolioOpt", "Financial portfolio optimisation in python", []),
        ("domokane/FinancePy", "A Python finance library focused on pricing derivatives", []),
        ("bukosabino/ta", "Technical Analysis Library using Pandas and Numpy", []),
    ]:
        check(f"anchor/keeps {name}", is_finance(
            dict(full_name=name, description=desc, topics=tops)), True)

    qs = build_search_queries(cfg)
    check_true("gh/queries built", len(qs) > 40)
    check_true("gh/queries deduped", len(qs) == len(set(qs)))
    check_true("gh/every query gated", all("stars:>=" in q and "pushed:>=" in q for q in qs))

    md = "# Title\n![badge](http://x/y.svg)\n\nImplements **deep hedging**, arXiv:1802.03042."
    ex = readme_excerpt(md)
    check_true("gh/readme strips badges", "badge" not in ex and "svg" not in ex)
    check_true("gh/readme keeps prose", "deep hedging" in ex)

    found = {a or b for a, b in _ARXIV_IN_TEXT.findall(
        "see https://arxiv.org/abs/2401.12345 and arXiv: 1802.03042 "
        "and https://arxiv.org/pdf/2203.00001")}
    check("gh/arxiv ids found", sorted(found), ["1802.03042", "2203.00001", "2401.12345"])
    check("gh/no false arxiv", _ARXIV_IN_TEXT.findall("version 2024.1 of the code"), [])


def test_classification():
    """Rules have to catch a phrase in the order papers actually write it."""
    from ingest.classify import compile_rules, match

    rules = compile_rules(load_config())

    def themes_for(title, cats=None):
        return {slug for slug, _, _ in match(rules, text=title, cats=set(cats or []))}

    # This exact title sat unclassified: the rules only had `option pricing`.
    check_true("classify/pricing european option",
               "derivatives-pricing" in themes_for(
                   "Pricing European option under the generalized fractional "
                   "jump-diffusion model"))
    check_true("classify/option pricing other order",
               "derivatives-pricing" in themes_for(
                   "A neural network approach to option pricing"))
    check_true("classify/limit order book",
               "market-microstructure" in themes_for(
                   "Limit order book dynamics and price impact"))
    check_true("classify/arxiv category alone is enough",
               "volatility-options" in themes_for("Some untitled work", ["q-fin.PR"]))
    # An exclude rule has to veto a theme it would otherwise match.
    check_true("classify/exclude vetoes",
               "volatility-options" not in themes_for(
                   "Volatility of inflation and the implied volatility of policy"))
    check_true("classify/unrelated title gets nothing",
               themes_for("A study of medieval pottery glazes") == set())


def main() -> int:
    for fn in (test_arxiv, test_month_windows, test_openalex,
               test_phrase_expansion, test_github, test_classification):
        fn()
    for name, got, want in FAIL:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
