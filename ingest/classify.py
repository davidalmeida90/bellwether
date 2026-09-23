"""Assign themes to papers and repos from the rules in themes.yaml.

Deliberately rule-based rather than learned: every assignment traces back to a
category, a topic or a named keyword, so a wrong theme is fixed by editing one
line of YAML and re-running, with no retraining and no black box.

Scoring
    category / topic match   1.0   an arXiv category or GitHub topic
    keyword match            0.6   a phrase in title, abstract or readme
    both                     1.0   capped

  py -3 ingest/classify.py            # only items with no themes yet
  py -3 ingest/classify.py --all      # reclassify everything after editing rules
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import connect, load_config, utf8_stdout  # noqa: E402

CAT_SCORE = 1.0
KEYWORD_SCORE = 0.6


def compile_rules(cfg: dict) -> list[dict]:
    """Prepare one matcher per theme. YAML folds a `>-` block into a single
    line with a space after each newline, which would leave alternatives like
    `| stochastic volatility` unable to match at the start of a string, so the
    padding around every `|` is stripped first."""
    rules = []
    for t in cfg["themes"]:
        include = t.get("include")
        exclude = t.get("exclude")
        if include:
            include = re.sub(r"\s*\|\s*", "|", re.sub(r"\s+", " ", include)).strip()
        if exclude:
            exclude = re.sub(r"\s*\|\s*", "|", re.sub(r"\s+", " ", exclude)).strip()
        rules.append({
            "slug": t["slug"],
            "label": t["label"],
            "ord": t.get("ord", 0),
            "arxiv_cats": set(t.get("arxiv_cats") or []),
            "gh_topics": {s.lower() for s in (t.get("gh_topics") or [])},
            "include": re.compile(include, re.I) if include else None,
            "exclude": re.compile(exclude, re.I) if exclude else None,
        })
    return rules


def sync_themes(con, cfg: dict) -> None:
    for t in cfg["themes"]:
        con.execute(
            "INSERT INTO themes(slug,label,ord,arxiv_cats_json,gh_topics_json,"
            "include_regex,exclude_regex) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET label=excluded.label, ord=excluded.ord, "
            "arxiv_cats_json=excluded.arxiv_cats_json, gh_topics_json=excluded.gh_topics_json, "
            "include_regex=excluded.include_regex, exclude_regex=excluded.exclude_regex",
            (t["slug"], t["label"], t.get("ord", 0),
             json.dumps(t.get("arxiv_cats") or []), json.dumps(t.get("gh_topics") or []),
             t.get("include"), t.get("exclude")),
        )
    con.commit()


def match(rules: list[dict], *, text: str, cats: set[str] | None = None,
          topics: set[str] | None = None) -> list[tuple[str, float, str]]:
    """Return (theme_slug, score, method) for every theme this item belongs to."""
    hits = []
    for r in rules:
        if r["exclude"] and r["exclude"].search(text):
            continue
        by_cat = bool(cats and (cats & r["arxiv_cats"]))
        by_topic = bool(topics and (topics & r["gh_topics"]))
        by_kw = bool(r["include"] and r["include"].search(text))
        if not (by_cat or by_topic or by_kw):
            continue
        if by_cat or by_topic:
            score, method = CAT_SCORE, "category" if by_cat else "topic"
        else:
            score, method = KEYWORD_SCORE, "keyword"
        hits.append((r["slug"], score, method))
    return hits


def classify_papers(con, rules, only_new: bool, verbose: bool = True) -> int:
    where = ("WHERE p.id NOT IN (SELECT item_id FROM item_themes WHERE item_type='paper')"
             if only_new else "")
    rows = con.execute(
        f"SELECT p.id, p.title, p.abstract, p.categories_json FROM papers p {where}"
    ).fetchall()
    n = 0
    for row in rows:
        text = f"{row['title']} {row['abstract'] or ''}"
        cats = set(json.loads(row["categories_json"] or "[]"))
        hits = match(rules, text=text, cats=cats)
        con.execute("DELETE FROM item_themes WHERE item_type='paper' AND item_id=?",
                    (row["id"],))
        for slug, score, method in hits:
            con.execute(
                "INSERT OR REPLACE INTO item_themes(item_type,item_id,theme_slug,score,method)"
                " VALUES('paper',?,?,?,?)", (row["id"], slug, score, method))
        n += 1
        if verbose and n % 2000 == 0:
            print(f"    {n}/{len(rows)}", flush=True)
            con.commit()
    con.commit()
    return n


def classify_repos(con, rules, only_new: bool, verbose: bool = True) -> int:
    where = ("WHERE r.id NOT IN (SELECT item_id FROM item_themes WHERE item_type='repo')"
             if only_new else "")
    rows = con.execute(
        f"SELECT r.id, r.full_name, r.description, r.topics_json, r.readme_excerpt "
        f"FROM repos r {where}"
    ).fetchall()
    n = 0
    for row in rows:
        text = " ".join(filter(None, [
            row["full_name"].replace("/", " ").replace("-", " "),
            row["description"] or "", row["readme_excerpt"] or ""]))
        topics = {t.lower() for t in json.loads(row["topics_json"] or "[]")}
        hits = match(rules, text=text, topics=topics)
        con.execute("DELETE FROM item_themes WHERE item_type='repo' AND item_id=?",
                    (row["id"],))
        for slug, score, method in hits:
            con.execute(
                "INSERT OR REPLACE INTO item_themes(item_type,item_id,theme_slug,score,method)"
                " VALUES('repo',?,?,?,?)", (row["id"], slug, score, method))
        n += 1
    con.commit()
    return n


def report(con) -> None:
    print(f"{'theme':28} {'papers':>7} {'repos':>7}")
    for r in con.execute(
        "SELECT t.slug,"
        " sum(CASE WHEN it.item_type='paper' THEN 1 ELSE 0 END) p,"
        " sum(CASE WHEN it.item_type='repo'  THEN 1 ELSE 0 END) g"
        " FROM themes t LEFT JOIN item_themes it ON it.theme_slug=t.slug"
        " GROUP BY t.slug ORDER BY t.ord"
    ):
        print(f"{r['slug']:28} {r['p'] or 0:>7} {r['g'] or 0:>7}")
    unclassified = con.execute(
        "SELECT count(*) n FROM papers WHERE id NOT IN "
        "(SELECT item_id FROM item_themes WHERE item_type='paper')").fetchone()["n"]
    print(f"{'(papers with no theme)':28} {unclassified:>7}")


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Assign themes from themes.yaml")
    ap.add_argument("--all", action="store_true",
                    help="reclassify every item, not only the unclassified ones")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    con = connect()
    cfg = load_config()
    sync_themes(con, cfg)
    rules = compile_rules(cfg)

    p = classify_papers(con, rules, not args.all, not args.quiet)
    g = classify_repos(con, rules, not args.all, not args.quiet)
    print(f"classified {p} papers, {g} repos")
    report(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
