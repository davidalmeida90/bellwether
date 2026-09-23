<p align="center">
  <img src="docs/logo.png" alt="Bellwether: the GitHub trending board, tracked every day" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Database-SQLite_+_FTS5-003B57?logo=sqlite&logoColor=white" alt="SQLite with FTS5">
  <img src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Sources-GitHub_·_arXiv_·_OpenAlex-0b2545" alt="Sources">
  <img src="https://img.shields.io/badge/API_keys-none_needed-2e7d32" alt="No API keys">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-2e4bc9" alt="MIT"></a>
</p>

## What it is

GitHub shows you what is trending today and forgets it tomorrow. Bellwether captures the boards every day, keeps every row, sorts them the way you actually want them, and tells you when something you care about shows up.

It runs on your machine. No API key, no account, no service in the middle.

- **Six boards, every day.** Overall daily, overall weekly, and one board per language. Rows are stored forever, so last Tuesday is still there next month.
- **Themes, not just languages.** Every row is classified into AI agents, LLM apps and RAG, models and training, inference, data, developer tools, infra, web, systems, security and more. Filter the board by what a repository does.
- **Alerts you write yourself.** A theme, a keyword, a minimum number of stars gained. Hits arrive as a Windows notification and an unread count on the bell, once per repository per day.
- **A watchlist ranked by real growth.** Repositories you track get a daily snapshot, and momentum is measured against each one's own history rather than by raw star count.
- **Runs, not snapshots.** Because every day is kept, a repository's detail shows every board it has appeared on, and the list can be sorted by how long it has held on.

<p align="center">
  <img src="docs/demo.gif" alt="Sorting the watchlist by growth, filtering today's trending board by theme, and creating an alert from a theme" width="900">
</p>

## Quickstart

```
pip install -r requirements.txt
py -3 ingest/trending.py --daily     # capture today's boards
py -3 serve.py                       # opens http://127.0.0.1:8077/
```

Then make it a series rather than a single day, which is what growth, runs and alerts all depend on:

```
powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
```

GitHub metadata is read with the token already in your `gh` CLI keyring (`gh auth token`), so nothing is written into this project. The boards themselves need no auth at all.

## The board, kept

Trending pages have no API, so the boards are read from the public pages, once a day per board with a crawl delay. Parsing works by meaning rather than by class name (`href` ending in `/stargazers`, `itemprop="programmingLanguage"`, the "N stars today" phrase), because GitHub rewrites its utility classes far more often than it changes the page's structure. A layout change is reported rather than swallowed.

Board rows carry only a name, a description, a language and two counts, so each repository is read once through the API for its topics, which are the strongest signal a classifier gets.

What the interface then gives you over the page itself:

| | github.com/trending | Bellwether |
|---|---|---|
| Yesterday | gone | kept |
| Sort | board order | board order, stars gained, longest run, total stars |
| Filter | language | language, theme, first time on the board, free text |
| Alerts | none | your rules, desktop notification |
| History per repository | none | every day it has ever trended |

## Alerts

A rule is a row in the database, written from the interface: a theme, a keyword, a minimum number of stars gained in the board's window, and a board. Rules are evaluated right after each capture, so adding one never needs a restart.

Each hit is recorded once per rule per repository per day, which is what keeps a repository that trends all week from notifying you every morning. Desktop notifications go through Windows directly, with no extra package, and anything that cannot pop is still waiting on the bell.

```
py -3 ingest/notify.py --add "Agents breaking out" --theme ai-agents --min-stars 300
py -3 ingest/notify.py --run
py -3 ingest/notify.py --test
```

Notification text carries repository names and descriptions written by strangers, so it travels to PowerShell through the environment and never into the script's source. That is a regression test, not a footnote.

## Growth, measured against a repository's own history

Raw star counts rank the same twenty famous repositories forever. `repo_snapshots` holds one row per repository per day, and momentum is `stars_30d / log(1 + stars)`, which is what lets a 300 star repository adding 80 a month outrank a 40,000 star one adding 200. A spike flag fires when the week's gain sits more than three standard deviations above that repository's own weekly history.

**History cannot be backfilled on every account.** The intended shortcut was to sample the stargazers endpoint, whose entries carry `starred_at`, so two dozen sampled pages would reconstruct a whole curve. GitHub refuses that listing on some accounts: REST answers 404 for every repository (401 unauthenticated) and the GraphQL `stargazers` connection returns `stargazerCount` alongside zero edges, while the repository endpoint itself reads fine. `--backfill-stars` probes for this and says so rather than silently recording nothing.

So growth builds forward instead. Run the daily task and real 7d and 30d figures appear within a week. Until then the Growing sort falls back to lifetime average stars per day, shown as `~N/day avg` in muted text so it is never confused with measured growth.

## Themes are rules you can read

```yaml
- slug: ai-agents
  label: AI agents
  topics: [ai-agents, agents, mcp, multi-agent]
  include: >-
    \bagent(s|ic)?\b|agent framework|autonomous|multi[- ]agent|\bmcp\b|
    computer use|browser use|coding assistant
  exclude: user[- ]agent|sales agent
```

A topic hit scores 1.0, a keyword hit 0.6. Assignment is rule-based on purpose: a wrong theme is fixed by editing one line and re-running, with no retraining and nothing hidden. Anything that matches nothing lands in "other", which is a real listing rather than a bin, and the first place to look when a theme needs widening.

```
py -3 ingest/trending.py --classify
```

## Papers, as a second module

The same index also holds a searchable table of research papers, pulled from arXiv and OpenAlex and deduplicated into one row per work, with citation counts, open access status and a badge saying whether a free PDF exists. A repository that cites an arXiv id in its README is linked to that paper, so a paper lists its implementations and a repository lists the papers behind it.

Papers use their own taxonomy in `config/themes.yaml`. What ships is one domain worked through end to end as an example; point those rules at your own field and the second module follows.

## How it works

Ingestion and serving are separate. Scripts in `ingest/` write to a SQLite database; the API in `api/` only reads it. Every page load is a local index lookup, so the interface stays instant and works with no network.

```
config/trending_themes.yaml   the board taxonomy: topics and regexes per theme
config/themes.yaml            the paper and watchlist taxonomy
db/schema.sql                 tables, FTS5 indexes
ingest/trending.py            the daily boards, parsed, enriched and classified
ingest/notify.py              alert rules and Windows notifications
ingest/github.py              search, README fetch, daily snapshots, growth metrics
ingest/arxiv.py               arXiv Atom API, month-windowed backfill + incremental
ingest/openalex.py            citations by DOI, plus discovery of work never on arXiv
ingest/classify.py            applies themes.yaml to papers and repositories
api/                          FastAPI, read-only
web/                          the interface, no build step
refresh.py                    one entry point for every ingest step
scripts/schedule_daily.ps1    registers the daily capture with Task Scheduler
```

## Keeping false positives out

A watchlist built by search needs a gate, because a term that is precise in one field is ambiguous in every other. `repo_anchor` requires either an unambiguous topic or a matching term in the name, description or topics, and every term is word-anchored, because unanchored stems produced exactly the false positives you would expect: a three letter stem matched inside "model-definition" and pulled in huggingface/transformers, another matched inside "quantization" and pulled in LlamaFactory, and `broker` matched "message broker" and pulled in redis. Each of those is now a regression test.

A trending row joins the watchlist only when it passes that anchor **and** lands in the matching theme, because one signal alone is too loose on a general board.

Tightening a gate later is cheap: `--prune` re-applies the anchor to what is already stored, so the search quota already spent is not wasted.

## Commands

```
py -3 ingest/trending.py --daily             capture the boards
py -3 ingest/trending.py --show              print the last board captured
py -3 ingest/trending.py --classify          reapply the board taxonomy
py -3 ingest/notify.py --run                 evaluate the alert rules
py -3 ingest/github.py --daily               snapshot + growth metrics
py -3 ingest/github.py --discover            build the watchlist from themes.yaml
py -3 ingest/github.py --prune --apply       drop what no longer passes the gate
py -3 ingest/arxiv.py --since 2015-01-01
py -3 ingest/openalex.py --enrich

py -3 refresh.py --trending                  boards + alerts, what the task runs
py -3 refresh.py --daily                     every module, incremental
py -3 refresh.py --seed                      full first-time backfill

py -3 tests/test_trending.py                 offline, board parsing, alerts, toast text
py -3 tests/test_sources.py                  offline, the source adapters
py -3 tests/test_core.py                     offline, dedupe and normalization
py -3 tests/test_api.py                      offline, every endpoint
node  tests/test_safeurl.mjs                 front-end URL scheme allowlist
```

## Interface

Filters live in the URL, so a board view can be bookmarked and reopened exactly as it was.

Keyboard: `/` focus search, `j` and `k` move, `Enter` open detail, `o` open the source link, `s` save, `Esc` close.

<p align="center">
  <img src="docs/screenshot-trending.png" alt="Today's trending board filtered by theme, with the alerts panel open" width="900">
</p>

## Limits

- **Trending is scraped, not an API.** Parsing reads the page by meaning rather than by class name, and a layout change is reported rather than swallowed, but it is still a page that can change.
- **Board rows are shallow.** Each row is enriched through the API before it is classified. With enrichment capped, a busy day can leave a few rows classified on their description alone.
- **Themes are rules, not judgement.** Check "other" when something seems missing.
- **Star history starts the day you start.** See growth above.
- **Desktop notifications are Windows only.** Everywhere else, hits still collect on the bell.

## License

MIT
