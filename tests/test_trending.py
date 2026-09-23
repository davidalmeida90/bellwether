"""Offline checks for the trending boards and the alert rules.

No network and no shared database: the board HTML below is a trimmed copy of a
real page, and every database check runs against a temporary file.

Run: py -3 tests/test_trending.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BOARD = """
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/google/ax" data-view-component="true">google / <span class="text-normal">ax</span></a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Google&#39;s open agentic orchestration runtime</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Go</span>
    <a href="/google/ax/stargazers" class="Link--muted"><svg></svg> 8,794</a>
    <a href="/google/ax/forks" class="Link--muted"><svg></svg> 408</a>
    <span class="d-inline-block float-sm-right"><svg></svg> 1,542 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/ranaroussi/yfinance">ranaroussi / <span class="text-normal">yfinance</span></a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Download market data from Yahoo Finance for backtesting</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/ranaroussi/yfinance/stargazers" class="Link--muted"><svg></svg> 14,206</a>
    <span class="d-inline-block float-sm-right"><svg></svg> 96 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/torvalds/linux">torvalds / <span>linux</span></a></h2>
  <div class="f6">
    <span itemprop="programmingLanguage">C</span>
    <a href="/torvalds/linux/stargazers" class="Link--muted"><svg></svg> 201,455</a>
  </div>
</article>
"""

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def check_true(name, cond):
    check(name, bool(cond), True)


# ------------------------------------------------------------------- parse ---
def test_parse():
    from ingest.trending import parse_board

    rows = parse_board(BOARD)
    check("rows parsed", len(rows), 3)
    first = rows[0]
    check("full name", first["full_name"], "google/ax")
    check("rank", first["rank"], 1)
    check("description", first["description"],
          "Google's open agentic orchestration runtime")
    check("language", first["language"], "Go")
    # Star counts sit inside an anchor that also holds an icon, and the icon's
    # path data is full of digits. Tags are stripped before any number is read,
    # so the count is the repository's and not part of an SVG.
    check("stars", first["stars"], 8794)
    check("forks", first["forks"], 408)
    check("stars today", first["stars_window"], 1542)
    check("missing description stays empty", rows[2]["description"], None)
    check("missing window stays empty", rows[2]["stars_window"], None)
    check("url", rows[1]["url"], "https://github.com/ranaroussi/yfinance")


def test_themes():
    from ingest.trending import compile_themes, load_trending_config, parse_board, themes_for

    rules = compile_themes(load_trending_config())
    rows = {r["full_name"]: r for r in parse_board(BOARD)}
    check_true("agent runtime is an agent theme",
               "ai-agents" in themes_for(rows["google/ax"], rules))
    check_true("market data is finance",
               "quant-finance" in themes_for(rows["ranaroussi/yfinance"], rules))
    # A board row with no description carries almost no signal, which is why
    # every row is enriched with its topics before it is classified.
    check("a bare row lands in other", themes_for(rows["torvalds/linux"], rules),
          ["other"])
    check_true("its topics settle it",
               "systems-langs" in themes_for(
                   {**rows["torvalds/linux"], "topics": ["operating-system", "kernel"]},
                   rules))
    check("nothing matched falls to other",
          themes_for({"full_name": "someone/quiet", "description": "a diary"}, rules),
          ["other"])
    # `portfolio` on its own used to make every portfolio-of-projects repo look
    # like quant finance, so the word now has to sit next to a finance verb.
    check("a project portfolio is not finance",
          "quant-finance" in themes_for(
              {"full_name": "someone/cv", "description": "My portfolio of 70 projects"},
              rules), False)
    check_true("a portfolio optimiser is finance",
               "quant-finance" in themes_for(
                   {"full_name": "someone/pf", "description": "Portfolio optimisation in Rust"},
                   rules))


# ------------------------------------------------------------------ alerts ---
def test_alerts():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["GPT_DB"] = path
    for mod in ("ingest.common", "ingest.notify"):
        sys.modules.pop(mod, None)
    from ingest.common import connect          # noqa: E402  (after GPT_DB is set)
    from ingest.notify import add_rule, run    # noqa: E402

    con = connect(path)
    rows = [("google/ax", '["ai-agents"]', 1542), ("ranaroussi/yfinance", '["quant-finance"]', 96)]
    for i, (name, themes, gained) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO trending(day, window, board, rank, full_name, url, description, "
            "stars_window, themes_json, first_seen) "
            "VALUES('2026-09-23','daily','',?,?,?,?,?,?, '2026-09-23')",
            (i, name, f"https://github.com/{name}", "", gained, themes))
    con.commit()

    add_rule(con, "agents", theme="ai-agents", min_stars=500, desktop=False)
    add_rule(con, "finance", theme="quant-finance", desktop=False)
    add_rule(con, "never", theme="ai-agents", min_stars=99999, desktop=False)
    stats = run(con, day="2026-09-23", verbose=False)
    check("three rules ran", stats["rules"], 3)
    check("two rows caught", stats["hits"], 2)

    # A repository that stays on the board all week must not notify every day.
    again = run(con, day="2026-09-23", verbose=False)
    check("the same day never fires twice", again["hits"], 0)

    hits = con.execute(
        "SELECT full_name FROM alert_hits ORDER BY full_name").fetchall()
    check("what was caught", [h[0] for h in hits],
          ["google/ax", "ranaroussi/yfinance"])
    con.close()
    os.environ.pop("GPT_DB", None)
    os.unlink(path)


def test_toast_text():
    """Notification text comes from strangers, so it never reaches a shell.

    Repository names and descriptions are written by whoever owns the repo. The
    first version of the toast built a PowerShell here-string around them, and a
    description holding `'@` on its own line would have closed that string and
    run the rest. Text now travels in the environment, and this keeps the
    sanitiser honest.
    """
    from ingest.notify import TOAST_CHARS, _TOAST_PS, clean_toast_text

    payload = "harmless\n'@\nWrite-Output PWNED > pwned.txt\n@'"
    cleaned = clean_toast_text(payload)
    check("no newline survives", "\n" in cleaned, False)
    check("no carriage return survives", "\r" in cleaned, False)
    check("no null byte survives", "\x00" in clean_toast_text("a\x00b"), False)
    check("length is bounded", len(clean_toast_text("x" * 5000)), TOAST_CHARS)
    check("ordinary text is left alone",
          clean_toast_text("google/ax +1,542 stars today"),
          "google/ax +1,542 stars today")
    # The script itself has to stay constant: no f-string, no interpolation.
    check("the script takes its text from the environment",
          "$env:BW_TOAST_TITLE" in _TOAST_PS and "$env:BW_TOAST_BODY" in _TOAST_PS, True)


def test_queries():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    sys.modules.pop("ingest.common", None)
    from ingest.common import connect
    from api import queries as Q

    con = connect(path)
    for i, (name, day, gained, first) in enumerate([
            ("a/one", "2026-09-22", 10, "2026-09-22"),
            ("a/two", "2026-09-23", 900, "2026-09-23"),
            ("a/one", "2026-09-23", 40, "2026-09-22")], 1):
        con.execute(
            "INSERT INTO trending(day, window, board, rank, full_name, url, "
            "stars_window, themes_json, first_seen, days_seen) "
            "VALUES(?,'daily','',?,?,?,?,'[\"ai-agents\"]',?,1)",
            (day, i, name, f"https://github.com/{name}", gained, first))
    con.commit()

    latest = Q.trending(con)
    check("defaults to the last day captured", latest["day"], "2026-09-23")
    check("that day's rows", latest["total"], 2)
    by_gain = Q.trending(con, sort="gained")["items"]
    check("sorted by stars gained", by_gain[0]["full_name"], "a/two")
    fresh = Q.trending(con, new_only=True)["items"]
    check("first time on the board", [r["full_name"] for r in fresh], ["a/two"])
    check("history spans days", len(Q.trending_history(con, "a/one")), 2)
    meta = Q.trending_meta(con)
    check("days listed newest first", meta["days"], ["2026-09-23", "2026-09-22"])
    check("theme counts", meta["theme_counts"], {"ai-agents": 2})
    con.close()
    os.unlink(path)


def main() -> int:
    for fn in (test_parse, test_themes, test_toast_text, test_alerts, test_queries):
        fn()
    for name, got, want in FAIL:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
