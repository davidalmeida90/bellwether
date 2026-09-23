-- github-paper-tracker :: schema
-- One canonical row per work in `papers`; every place it was found lives in
-- `paper_sources`. Dedupe key priority: doi > arxiv_id > title_key.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- papers ---
CREATE TABLE IF NOT EXISTS papers (
    id                  INTEGER PRIMARY KEY,
    doi                 TEXT UNIQUE,              -- normalized: lowercase, bare 10.x/...
    arxiv_id            TEXT UNIQUE,              -- bare id, no version suffix
    title_key           TEXT NOT NULL,            -- normalized title, dedupe fallback
    title               TEXT NOT NULL,
    abstract            TEXT,
    authors_json        TEXT NOT NULL DEFAULT '[]',
    first_author        TEXT,
    published_at        TEXT,                     -- ISO date, earliest known version
    updated_at          TEXT,                     -- ISO date, latest revision
    year                INTEGER,
    venue               TEXT,
    venue_type          TEXT,                     -- preprint | journal | working_paper | repository
    categories_json     TEXT NOT NULL DEFAULT '[]',  -- arXiv categories
    concepts_json       TEXT NOT NULL DEFAULT '[]',  -- OpenAlex topics/concepts
    citations           INTEGER NOT NULL DEFAULT 0,
    citations_updated_at TEXT,
    pdf_url             TEXT,                     -- best open-access PDF, may be null
    landing_url         TEXT,                     -- always present
    oa_status           TEXT,                     -- gold|green|hybrid|bronze|diamond|closed|unknown
    has_pdf             INTEGER NOT NULL DEFAULT 0,
    is_preprint         INTEGER NOT NULL DEFAULT 0,
    sources_json        TEXT NOT NULL DEFAULT '[]',
    hidden              INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_papers_title_key   ON papers(title_key);
CREATE INDEX IF NOT EXISTS ix_papers_published   ON papers(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_papers_citations   ON papers(citations DESC);
CREATE INDEX IF NOT EXISTS ix_papers_year        ON papers(year);

CREATE TABLE IF NOT EXISTS paper_sources (
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,                    -- arxiv | openalex | crossref | manual
    ext_id      TEXT NOT NULL,
    url         TEXT,
    pdf_url     TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, ext_id)
);
CREATE INDEX IF NOT EXISTS ix_paper_sources_pid ON paper_sources(paper_id);

CREATE TABLE IF NOT EXISTS paper_snapshots (
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    citations   INTEGER NOT NULL,
    PRIMARY KEY (paper_id, day)
);

CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, abstract, authors, venue,
    paper_id UNINDEXED,
    tokenize = 'porter unicode61'
);

-- ----------------------------------------------------------------- repos ---
CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY,          -- GitHub numeric id
    full_name       TEXT NOT NULL UNIQUE,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    homepage        TEXT,
    url             TEXT NOT NULL,
    language        TEXT,
    stars           INTEGER NOT NULL DEFAULT 0,
    forks           INTEGER NOT NULL DEFAULT 0,
    open_issues     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    pushed_at       TEXT,
    topics_json     TEXT NOT NULL DEFAULT '[]',
    license         TEXT,
    is_fork         INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0,
    readme_excerpt  TEXT,
    stars_7d        INTEGER,
    stars_30d       INTEGER,
    momentum        REAL,          -- real 30d growth; null until history exists
    stars_per_day   REAL,          -- lifetime average, the interim fallback
    spike           INTEGER NOT NULL DEFAULT 0,
    hidden          INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_repos_stars    ON repos(stars DESC);
CREATE INDEX IF NOT EXISTS ix_repos_momentum ON repos(momentum DESC);
CREATE INDEX IF NOT EXISTS ix_repos_pushed   ON repos(pushed_at DESC);

CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    stars   INTEGER NOT NULL,
    forks   INTEGER,
    PRIMARY KEY (repo_id, day)
);

CREATE VIRTUAL TABLE IF NOT EXISTS repos_fts USING fts5(
    full_name, description, topics, readme,
    repo_id UNINDEXED,
    tokenize = 'porter unicode61'
);

-- ---------------------------------------------------------------- themes ---
CREATE TABLE IF NOT EXISTS themes (
    slug            TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    ord             INTEGER NOT NULL DEFAULT 0,
    arxiv_cats_json TEXT NOT NULL DEFAULT '[]',
    gh_topics_json  TEXT NOT NULL DEFAULT '[]',
    include_regex   TEXT,
    exclude_regex   TEXT
);

CREATE TABLE IF NOT EXISTS item_themes (
    item_type   TEXT NOT NULL,                    -- paper | repo
    item_id     INTEGER NOT NULL,
    theme_slug  TEXT NOT NULL REFERENCES themes(slug) ON DELETE CASCADE,
    score       REAL NOT NULL DEFAULT 0,
    method      TEXT NOT NULL,                    -- category | topic | keyword
    PRIMARY KEY (item_type, item_id, theme_slug)
);
CREATE INDEX IF NOT EXISTS ix_item_themes_theme ON item_themes(theme_slug, item_type, score DESC);

-- ----------------------------------------------------------- cross-links ---
CREATE TABLE IF NOT EXISTS links (
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    confidence  REAL NOT NULL DEFAULT 0,
    method      TEXT NOT NULL,                    -- arxiv_id_in_readme | title_match
    PRIMARY KEY (paper_id, repo_id)
);

-- ------------------------------------------------------------ user state ---
CREATE TABLE IF NOT EXISTS saved (
    item_type   TEXT NOT NULL,
    item_id     INTEGER NOT NULL,
    note        TEXT,
    reel_flag   INTEGER NOT NULL DEFAULT 0,
    saved_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_type, item_id)
);

CREATE TABLE IF NOT EXISTS authors_watch (
    name        TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vectors (
    item_type   TEXT NOT NULL,
    item_id     INTEGER NOT NULL,
    model       TEXT NOT NULL,
    vec         BLOB NOT NULL,
    PRIMARY KEY (item_type, item_id, model)
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    merged      INTEGER NOT NULL DEFAULT 0,
    status      TEXT,
    detail      TEXT
);

-- Daily GitHub trending boards. One row per repository per board per day, so
-- the same repository appears several times over a week and the history of what
-- was hot when survives even after it leaves the board.
CREATE TABLE IF NOT EXISTS trending (
    day             TEXT NOT NULL,           -- board date, local
    window          TEXT NOT NULL,           -- daily | weekly | monthly
    board           TEXT NOT NULL DEFAULT '',-- '' = all languages, else the language board
    rank            INTEGER NOT NULL,
    full_name       TEXT NOT NULL,
    url             TEXT,
    description     TEXT,
    language        TEXT,
    stars           INTEGER,
    forks           INTEGER,
    stars_window    INTEGER,                 -- stars gained in the board's window
    topics_json     TEXT NOT NULL DEFAULT '[]',
    themes_json     TEXT NOT NULL DEFAULT '[]',
    repo_id         INTEGER,                 -- set when the repo is also tracked in `repos`
    first_seen      TEXT,                    -- first day this repo was ever seen trending
    days_seen       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, window, board, full_name)
);
CREATE INDEX IF NOT EXISTS idx_trending_day ON trending(day DESC, window, board, rank);
CREATE INDEX IF NOT EXISTS idx_trending_repo ON trending(full_name, day DESC);

-- Alert rules over the trending boards, plus what each one caught. Rules are
-- data, not code: the interface writes them and the daily run reads them, so a
-- new alert never needs a restart.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    theme           TEXT NOT NULL DEFAULT '',   -- trending theme slug, '' = any
    keyword         TEXT NOT NULL DEFAULT '',   -- substring of name or description
    min_stars       INTEGER NOT NULL DEFAULT 0, -- stars gained in the board's window
    board           TEXT NOT NULL DEFAULT '',   -- '' = any board
    desktop         INTEGER NOT NULL DEFAULT 1, -- also raise a Windows notification
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_hits (
    id              INTEGER PRIMARY KEY,
    alert_id        INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    url             TEXT,
    description     TEXT,
    stars_window    INTEGER,
    themes_json     TEXT NOT NULL DEFAULT '[]',
    seen            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (alert_id, day, full_name)
);
CREATE INDEX IF NOT EXISTS idx_alert_hits_new ON alert_hits(seen, day DESC);
