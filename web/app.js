/* Paper & Repo Tracker — front end.
   One state object drives one render. Filters live in the URL hash so a search
   can be bookmarked and reopened exactly as it was. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  module: 'papers',
  q: '',
  themes: new Set(),
  venue_type: new Set(),
  date_from: '', date_to: '',
  sort: { papers: 'recent', repos: 'popular', trending: 'rank' },
  window: 'daily', board: '', day: '', new_only: false,
  trendThemes: [], alerts: [], hits: [],
  pdf_only: false, saved_only: false,
  min_citations: '', min_stars: '', language: '',
  page: 1, per_page: 50,
  selected: null, items: [], total: 0, themeLabels: {},
};

/* Titles and abstracts come from third-party APIs, so nothing reaches the DOM
   as markup without passing through here first. */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* esc() stops markup injection but not a `javascript:` href. Repo homepages and
   paper landing pages are set by third parties and arrive verbatim from GitHub
   and OpenAlex, so every URL that reaches an href or window.open passes a
   scheme allowlist first. */
const safeUrl = (u) => {
  if (!u) return '';
  try {
    const parsed = new URL(String(u), location.origin);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? parsed.href : '';
  } catch {
    return '';
  }
};

const fmt = (n) => (n === null || n === undefined) ? '' : Number(n).toLocaleString('en-US');
const shortDate = (d) => d ? d.slice(0, 7) : '';

function authorLine(authors, max = 4) {
  if (!authors || !authors.length) return '';
  const shown = authors.slice(0, max).join(', ');
  return authors.length > max ? `${shown} +${authors.length - max}` : shown;
}

/* ------------------------------------------------------------------ url --- */
function writeHash() {
  const p = new URLSearchParams();
  p.set('m', state.module);
  if (state.q) p.set('q', state.q);
  if (state.themes.size) p.set('t', [...state.themes].join(','));
  if (state.venue_type.size) p.set('vt', [...state.venue_type].join(','));
  if (state.date_from) p.set('df', state.date_from);
  if (state.date_to) p.set('dt', state.date_to);
  if (state.pdf_only) p.set('pdf', '1');
  if (state.saved_only) p.set('sv', '1');
  if (state.min_citations) p.set('mc', state.min_citations);
  if (state.min_stars) p.set('ms', state.min_stars);
  if (state.language) p.set('lang', state.language);
  if (state.module === 'trending') {
    if (state.window !== 'daily') p.set('w', state.window);
    if (state.board) p.set('b', state.board);
    if (state.day) p.set('d', state.day);
    if (state.new_only) p.set('new', '1');
  }
  p.set('s', state.sort[state.module]);
  if (state.page > 1) p.set('p', state.page);
  history.replaceState(null, '', '#' + p.toString());
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (!p.toString()) return;
  state.module = ['repos', 'trending'].includes(p.get('m')) ? p.get('m') : 'papers';
  state.q = p.get('q') || '';
  state.themes = new Set((p.get('t') || '').split(',').filter(Boolean));
  state.venue_type = new Set((p.get('vt') || '').split(',').filter(Boolean));
  state.date_from = p.get('df') || '';
  state.date_to = p.get('dt') || '';
  state.pdf_only = p.get('pdf') === '1';
  state.saved_only = p.get('sv') === '1';
  state.min_citations = p.get('mc') || '';
  state.min_stars = p.get('ms') || '';
  state.language = p.get('lang') || '';
  state.window = p.get('w') === 'weekly' ? 'weekly' : 'daily';
  state.board = p.get('b') || '';
  state.day = p.get('d') || '';
  state.new_only = p.get('new') === '1';
  if (p.get('s')) state.sort[state.module] = p.get('s');
  state.page = parseInt(p.get('p') || '1', 10) || 1;
}

/* ------------------------------------------------------------------ api --- */
async function api(path, params) {
  const url = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined && v !== false) url.searchParams.set(k, v);
  });
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

const post = (path, body) => fetch(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
}).then((r) => r.json());

/* --------------------------------------------------------------- render --- */
function sparkline(points, w = 96, h = 22) {
  if (!points || points.length < 2) return '';
  const ys = points.map((p) => p.v);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = hi - lo || 1;
  const step = w / (points.length - 1);
  const d = points.map((p, i) =>
    `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(h - ((p.v - lo) / span) * (h - 2) - 1).toFixed(1)}`
  ).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"
    fill="none" aria-hidden="true"><path d="${d}" stroke="var(--blue-500)"
    stroke-width="1.4" stroke-linejoin="round"/></svg>`;
}

function themeChips(slugs) {
  if (!slugs || !slugs.length) return '';
  return `<div class="tchips">${slugs.slice(0, 4).map((s) =>
    `<span class="tchip">${esc(state.themeLabels[s] || s)}</span>`).join('')}</div>`;
}

function paperRow(p, i) {
  const badge = p.has_pdf
    ? '<span class="badge pdf">PDF</span>'
    : '<span class="badge abs">ABS</span>';
  const cites = p.citations > 0
    ? `<span class="cites">${fmt(p.citations)}<small>CITED</small></span>` : '';
  return `<li class="row${state.selected === p.id ? ' is-sel' : ''}"
      data-id="${p.id}" data-i="${i}" data-url="${esc(safeUrl(p.pdf_url || p.landing_url))}">
    <div class="ttl">${esc(p.title)}</div>
    <div class="meta">
      <span class="au">${esc(authorLine(p.authors))}</span>
      <span class="fig">${esc(shortDate(p.published_at))}</span>
      ${p.venue ? `<span class="vn">${esc(p.venue)}</span>` : ''}
      ${p.is_saved ? '<span class="badge saved">SAVED</span>' : ''}
    </div>
    ${themeChips(p.themes)}
    <div class="right">${badge}${cites}</div>
  </li>`;
}

function repoRow(r, i) {
  // Real 30 day growth when history exists, otherwise the lifetime average,
  // labelled differently so the two are never mistaken for each other.
  let growth = '';
  if (r.stars_30d !== null && r.stars_30d !== undefined) {
    growth = `<span class="delta">+${fmt(r.stars_30d)}/30d</span>`;
  } else if (r.stars_per_day) {
    growth = `<span class="delta avg">~${r.stars_per_day.toFixed(1)}/day avg</span>`;
  }
  return `<li class="row${state.selected === r.id ? ' is-sel' : ''}"
      data-id="${r.id}" data-i="${i}" data-url="${esc(safeUrl(r.url))}">
    <div class="ttl">${esc(r.full_name)}</div>
    <div class="meta">
      <span class="au">${esc(r.description || '')}</span>
    </div>
    <div class="tchips">
      ${r.language ? `<span class="tchip k">${esc(r.language)}</span>` : ''}
      ${(r.themes || []).slice(0, 3).map((s) =>
        `<span class="tchip">${esc(state.themeLabels[s] || s)}</span>`).join('')}
    </div>
    <div class="right">
      ${r.spike ? '<span class="badge spike">SPIKE</span>' : ''}
      <span class="stars">${fmt(r.stars)}<small style="color:var(--fg-3)"> ★</small></span>
      ${growth}
    </div>
  </li>`;
}

function trendRow(t, i) {
  const gained = t.stars_window
    ? `<span class="gain">+${fmt(t.stars_window)}</span>` : '';
  const run = t.days_seen > 1 ? `<span class="badge run">${t.days_seen}d run</span>` : '';
  return `<li class="row${state.selected === t.rowid ? ' is-sel' : ''}"
      data-id="${t.rowid}" data-name="${esc(t.full_name)}" data-i="${i}"
      data-url="${esc(safeUrl(t.url))}">
    <div class="ttl"><span class="rank">${t.rank}</span>${esc(t.full_name)}</div>
    <div class="meta"><span class="au">${esc(t.description || '')}</span></div>
    <div class="tchips">
      ${t.language ? `<span class="tchip k">${esc(t.language)}</span>` : ''}
      ${(t.themes || []).slice(0, 3).map((x) =>
        `<span class="tchip">${esc(state.themeLabels[x] || x)}</span>`).join('')}
    </div>
    <div class="right">
      ${t.is_new ? '<span class="badge new">NEW</span>' : run}
      <span class="stars">${fmt(t.stars)}<small style="color:var(--fg-3)"> ★</small></span>
      ${gained}
    </div>
  </li>`;
}

async function renderTrendDetail(name) {
  const pane = $('#detail');
  const item = state.items.find((t) => t.full_name === name) || {};
  const { history } = await api('/api/trending/history', { full_name: name });
  pane.innerHTML = `
    <button class="closebtn" data-act="close" title="Close">&times;</button>
    <h1>${esc(name)}</h1>
    <div class="authors">${esc(item.description || '')}</div>
    <div class="actions">
      <a class="btn primary" href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener">Open on GitHub</a>
      <button class="btn" data-act="alert-theme">Alert me on this theme</button>
    </div>
    <dl class="facts">
      ${factRow('Board', esc(`${item.window || ''} ${item.board ? '· ' + item.board : '· all languages'}`))}
      ${factRow('Rank today', String(item.rank || ''))}
      ${factRow('Stars gained', item.stars_window ? '+' + fmt(item.stars_window) : '')}
      ${factRow('Stars total', fmt(item.stars))}
      ${factRow('Language', esc(item.language || ''))}
      ${factRow('Days on a board', String(item.days_seen || 1))}
      ${factRow('First seen', esc(item.first_seen || ''))}
      ${factRow('Topics', (item.topics || []).map(esc).join(', '))}
      ${factRow('Tracked as a finance repo', item.repo_id ? 'yes' : '')}
    </dl>
    <h4>Themes</h4>
    <div class="tchips">${(item.themes || []).map((x) =>
      `<span class="tchip">${esc(state.themeLabels[x] || x)}</span>`).join('')}</div>
    <h4>Every day it has trended</h4>
    <ul class="linklist">${history.map((h) =>
      `<li><span class="src">${esc(h.day)}</span><span class="muted">#${h.rank}
        · ${esc(h.window)}${h.board ? ' · ' + esc(h.board) : ''}
        ${h.stars_window ? ' · +' + fmt(h.stars_window) : ''}</span></li>`).join('')}</ul>
  `;
  pane.dataset.name = name;
  pane.hidden = false;
  pane.scrollTop = 0;
}

function renderList() {
  const list = $('#list');
  const empty = $('#empty');
  if (!state.items.length) {
    list.innerHTML = '';
    empty.hidden = false;
    empty.innerHTML = state.module === 'trending'
      ? `<h2>No board captured for this day</h2>
         <p>Capture today's trending boards, then come back.</p>
         <code>py -3 ingest/trending.py --daily</code>`
      : state.module === 'repos'
      ? `<h2>No repositories indexed yet</h2>
         <p>Run the GitHub ingest to build the watchlist.</p>
         <code>py -3 ingest/github.py --discover</code>`
      : `<h2>Nothing matches these filters</h2>
         <p>Widen the period, clear a theme, or search for something else.</p>`;
    return;
  }
  empty.hidden = true;
  const render = state.module === 'papers' ? paperRow
    : state.module === 'trending' ? trendRow : repoRow;
  list.innerHTML = state.items.map(render).join('');
}

function renderHead() {
  const noun = state.module === 'papers' ? 'papers'
    : state.module === 'trending' ? 'trending rows' : 'repos';
  $('#count').textContent = `${fmt(state.total)} ${noun}`;
  const pages = Math.max(1, Math.ceil(state.total / state.per_page));
  $('#pageinfo').textContent = `${state.page} / ${pages}`;
  $('#prev').disabled = state.page <= 1;
  $('#next').disabled = state.page >= pages;

  const bits = [];
  if (state.q) bits.push(`"${state.q}"`);
  if (state.themes.size) bits.push([...state.themes]
    .map((s) => state.themeLabels[s] || s).join(' + '));
  if (state.date_from || state.date_to) bits.push(`${state.date_from || '…'} to ${state.date_to || '…'}`);
  if (state.pdf_only) bits.push('open PDF');
  if (state.saved_only) bits.push('saved');
  $('#activefilters').textContent = bits.length ? '· ' + bits.join(' · ') : '';
}

/* --------------------------------------------------------------- detail --- */
function factRow(label, value) {
  return value ? `<dt>${esc(label)}</dt><dd>${value}</dd>` : '';
}

function renderPaperDetail(p) {
  const links = [];
  if (safeUrl(p.pdf_url)) links.push(['pdf', safeUrl(p.pdf_url)]);
  if (safeUrl(p.landing_url)) links.push(['page', safeUrl(p.landing_url)]);
  if (p.doi) links.push(['doi', safeUrl(`https://doi.org/${p.doi}`)]);
  if (p.arxiv_id) links.push(['arxiv', safeUrl(`https://arxiv.org/abs/${p.arxiv_id}`)]);

  const hist = (p.citation_history || []).map((h) => ({ v: h.citations }));
  const openPdf = safeUrl(p.pdf_url)
    ? `<a class="btn primary" href="${esc(safeUrl(p.pdf_url))}" target="_blank" rel="noopener">Open PDF</a>`
    : `<a class="btn" href="${esc(safeUrl(p.landing_url)) || '#'}" target="_blank" rel="noopener">Open page (no free PDF)</a>`;

  return `
    <button class="closebtn" data-act="close" title="Close">&times;</button>
    <h1>${esc(p.title)}</h1>
    <div class="authors">${esc((p.authors || []).join(', '))}</div>
    <div class="actions">
      ${openPdf}
      <button class="btn ${p.is_saved ? 'on' : ''}" data-act="save">${p.is_saved ? 'Saved' : 'Save'}</button>
      <button class="btn ${p.reel_flag ? 'on' : ''}" data-act="reel">Reel idea</button>
      <button class="btn" data-act="hide">Hide</button>
    </div>
    <dl class="facts">
      ${factRow('Published', esc(p.published_at || ''))}
      ${factRow('Revised', p.updated_at !== p.published_at ? esc(p.updated_at || '') : '')}
      ${factRow('Venue', esc(p.venue || '') + (p.venue_type ? ` <span class="muted">(${esc(p.venue_type)})</span>` : ''))}
      ${factRow('Citations', p.citations ? fmt(p.citations) + sparkline(hist) : '<span class="muted">not indexed</span>')}
      ${factRow('Access', esc(p.oa_status || 'unknown'))}
      ${factRow('arXiv', p.arxiv_id ? esc(p.arxiv_id) : '')}
      ${factRow('DOI', p.doi ? esc(p.doi) : '')}
      ${factRow('Categories', (p.categories || []).map(esc).join(', '))}
    </dl>
    <div class="abstract">${esc(p.abstract || 'No abstract recorded.')}</div>

    <h4>Themes</h4>
    <div class="tchips">${(p.themes || []).map((t) =>
      `<span class="tchip ${t.method === 'keyword' ? '' : 'k'}">${esc(t.label)} <span class="muted">${esc(t.method)}</span></span>`
    ).join('') || '<span class="muted">none matched</span>'}</div>

    <h4>Links</h4>
    <ul class="linklist">${links.map(([k, u]) =>
      `<li><span class="src">${esc(k)}</span><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></li>`
    ).join('')}</ul>

    <h4>Implementations</h4>
    ${(p.repos && p.repos.length)
      ? `<ul class="linklist">${p.repos.map((r) =>
          `<li><span class="src">${fmt(r.stars)} ★</span><a href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">${esc(r.full_name)}</a></li>`).join('')}</ul>`
      : '<p class="muted">No linked repository found yet.</p>'}

    <h4>Note</h4>
    <textarea class="note" data-act="note" placeholder="Why this matters…">${esc(p.note || '')}</textarea>

    <h4>Found in</h4>
    <ul class="linklist">${(p.source_links || []).map((s) =>
      `<li><span class="src">${esc(s.source)}</span><span class="muted">${esc(s.ext_id)}</span></li>`).join('')}</ul>
  `;
}

function renderRepoDetail(r) {
  const hist = (r.star_history || []).map((h) => ({ v: h.stars }));
  return `
    <button class="closebtn" data-act="close" title="Close">&times;</button>
    <h1>${esc(r.full_name)}</h1>
    <div class="authors">${esc(r.description || '')}</div>
    <div class="actions">
      <a class="btn primary" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">Open on GitHub</a>
      ${safeUrl(r.homepage) ? `<a class="btn" href="${esc(safeUrl(r.homepage))}" target="_blank" rel="noopener">Homepage</a>` : ''}
      <button class="btn ${r.is_saved ? 'on' : ''}" data-act="save">${r.is_saved ? 'Saved' : 'Save'}</button>
      <button class="btn" data-act="hide">Hide</button>
    </div>
    <dl class="facts">
      ${factRow('Stars', fmt(r.stars) + sparkline(hist))}
      ${factRow('Growth 30d', r.stars_30d != null ? '+' + fmt(r.stars_30d)
        : '<span class="muted">no history yet, building from daily snapshots</span>')}
      ${factRow('Lifetime avg', r.stars_per_day != null ? r.stars_per_day.toFixed(2) + ' stars/day' : '')}
      ${factRow('Growth 7d', r.stars_7d != null ? '+' + fmt(r.stars_7d) : '')}
      ${factRow('Momentum', r.momentum != null ? r.momentum.toFixed(2) : '')}
      ${factRow('Language', esc(r.language || ''))}
      ${factRow('Created', esc((r.created_at || '').slice(0, 10)))}
      ${factRow('Last push', esc((r.pushed_at || '').slice(0, 10)))}
      ${factRow('License', esc(r.license || ''))}
      ${factRow('Topics', (r.topics || []).map(esc).join(', '))}
    </dl>
    ${r.readme_excerpt ? `<div class="abstract">${esc(r.readme_excerpt)}</div>` : ''}
    <h4>Papers behind it</h4>
    ${(r.papers && r.papers.length)
      ? `<ul class="linklist">${r.papers.map((p) =>
          `<li><span class="src">${esc(String(p.year || ''))}</span><a href="${esc(safeUrl(p.landing_url))}" target="_blank" rel="noopener">${esc(p.title)}</a></li>`).join('')}</ul>`
      : '<p class="muted">No linked paper found yet.</p>'}
    <h4>Note</h4>
    <textarea class="note" data-act="note" placeholder="Why this matters…">${esc(r.note || '')}</textarea>
  `;
}

async function openDetail(id) {
  state.selected = id;
  $$('.row').forEach((el) => el.classList.toggle('is-sel', +el.dataset.id === id));
  const pane = $('#detail');
  pane.hidden = false;
  pane.innerHTML = '<p class="muted">Loading…</p>';
  const item = state.module === 'papers'
    ? await api(`/api/papers/${id}`)
    : await api(`/api/repos/${id}`);
  pane.innerHTML = state.module === 'papers'
    ? renderPaperDetail(item) : renderRepoDetail(item);
  pane.dataset.id = id;
  pane.scrollTop = 0;
}

function closeDetail() {
  $('#detail').hidden = true;
  state.selected = null;
  $$('.row').forEach((el) => el.classList.remove('is-sel'));
}

/* ---------------------------------------------------------------- load ---- */
let loadToken = 0;
async function load() {
  const mine = ++loadToken;
  writeHash();
  const common = {
    q: state.q,
    themes: [...state.themes].join(','),
    saved_only: state.saved_only,
    page: state.page,
    per_page: state.per_page,
    sort: state.sort[state.module],
  };
  if (state.module === 'trending') {
    const data = await api('/api/trending', {
      q: state.q, theme: [...state.themes][0] || '', window: state.window,
      board: state.board, day: state.day, sort: state.sort.trending,
      new_only: state.new_only, page: state.page, per_page: state.per_page,
    });
    if (mine !== loadToken) return;
    // Board rows carry no id of their own, so a row's identity is its position.
    state.items = data.items.map((t, i) => ({ ...t, rowid: i + 1 }));
    state.total = data.total;
    state.day = data.day || state.day;
    renderHead();
    renderList();
    return;
  }
  const data = state.module === 'papers'
    ? await api('/api/papers', {
      ...common,
      date_from: state.date_from, date_to: state.date_to,
      pdf_only: state.pdf_only,
      venue_type: [...state.venue_type].join(','),
      min_citations: state.min_citations,
    })
    : await api('/api/repos', {
      ...common, language: state.language, min_stars: state.min_stars,
    });
  if (mine !== loadToken) return;   // a newer request already went out
  state.items = data.items;
  state.total = data.total;
  renderHead();
  renderList();
}

async function loadThemes() {
  if (state.module === 'trending') return loadTrendThemes();
  const themes = await api('/api/themes');
  themes.forEach((t) => { state.themeLabels[t.slug] = t.label; });
  $('#themes').innerHTML = themes.map((t) => {
    const n = state.module === 'papers' ? t.papers : t.repos;
    const on = state.themes.has(t.slug);
    return `<button type="button" class="theme${on ? ' is-on' : ''}"
      data-slug="${esc(t.slug)}" aria-pressed="${on}">
      <span class="nm">${esc(t.label)}</span>
      <span class="ct">${fmt(n)}</span>
    </button>`;
  }).join('');
  $('#clear-themes').hidden = state.themes.size === 0;
}

async function loadTrendThemes() {
  const meta = await api('/api/trending/meta');
  state.trendThemes = meta.themes || [];
  state.trendThemes.forEach((t) => { state.themeLabels[t.slug] = t.label; });
  const counts = meta.theme_counts || {};
  $('#themes').innerHTML = state.trendThemes.map((t) => {
    const on = state.themes.has(t.slug);
    return `<button type="button" class="theme${on ? ' is-on' : ''}"
      data-slug="${esc(t.slug)}" aria-pressed="${on}">
      <span class="nm">${esc(t.label)}</span>
      <span class="ct">${fmt(counts[t.slug] || 0)}</span>
    </button>`;
  }).join('');
  $('#clear-themes').hidden = state.themes.size === 0;
  $('#trendday').innerHTML = '<option value="">Latest captured</option>' +
    (meta.days || []).map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  if (state.day) $('#trendday').value = state.day;
  $('#a_theme').innerHTML = '<option value="">Any theme</option>' +
    state.trendThemes.map((t) => `<option value="${esc(t.slug)}">${esc(t.label)}</option>`).join('');
}

/* --------------------------------------------------------------- alerts --- */
function renderAlerts() {
  const unread = state.alerts.reduce((n, a) => n + a.unread, 0);
  $('#bellcount').textContent = fmt(unread);
  $('#bellcount').hidden = unread === 0;

  $('#rulelist').innerHTML = state.alerts.map((a) => {
    const cond = [a.theme ? state.themeLabels[a.theme] || a.theme : 'any theme',
      a.keyword ? `"${a.keyword}"` : '', a.min_stars ? `+${a.min_stars} stars` : '',
      a.desktop ? 'desktop' : 'in app'].filter(Boolean).join(' · ');
    return `<li data-id="${a.id}">
      <span class="acts">
        <button class="linkish" data-aact="toggle">${a.active ? 'pause' : 'resume'}</button>
        <button class="linkish" data-aact="delete">delete</button>
      </span>
      <div class="lb">${esc(a.label)}${a.unread ? ` <span class="badge new">${a.unread}</span>` : ''}</div>
      <div class="cond">${esc(cond)}</div>
    </li>`;
  }).join('') || '<li class="muted">No alerts yet.</li>';

  $('#hitlist').innerHTML = state.hits.map((h) =>
    `<li class="${h.seen ? '' : 'unseen'}">
      <a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener">${esc(h.full_name)}</a>
      <div class="sub">${esc(h.label)} · ${esc(h.day)}${h.stars_window ? ' · +' + fmt(h.stars_window) : ''}</div>
    </li>`).join('') || '<li class="muted">Nothing caught yet.</li>';
}

async function loadAlerts() {
  try {
    const data = await api('/api/alerts');
    state.alerts = data.alerts;
    state.hits = data.hits;
    renderAlerts();
  } catch (err) {
    console.warn('alerts failed to load', err);
  }
}

async function loadChrome() {
  // Header counts and the language list are decoration. If either call fails
  // the results still have to render, so this never throws upward.
  let stats, langs;
  try {
    [stats, langs] = await Promise.all([api('/api/stats'), api('/api/languages')]);
  } catch (err) {
    console.warn('chrome failed to load', err);
    $('#dbstat').textContent = 'stats unavailable';
    return;
  }
  $('#dbstat').textContent =
    `${fmt(stats.papers)} papers · ${fmt(stats.repos)} repos`;
  const last = stats.last_ingest && stats.last_ingest[0];
  $('#ingestinfo').textContent = last
    ? `last ingest\n${last.source}\n${(last.finished_at || '').replace('T', ' ')}`
    : 'no ingest recorded';
  $('#language').innerHTML = '<option value="">Any language</option>' +
    langs.map((l) => `<option value="${esc(l.language)}">${esc(l.language)} (${fmt(l.n)})</option>`).join('');
  if (state.language) $('#language').value = state.language;
}

/* --------------------------------------------------------------- events --- */
function setModule(mod) {
  state.module = mod;
  state.page = 1;
  state.selected = null;
  document.documentElement.dataset.module = mod;
  $$('.mod').forEach((b) => b.classList.toggle('is-on', b.dataset.mod === mod));
  $('#detail').hidden = true;
  loadThemes().then(load);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function syncControlsFromState() {
  document.documentElement.dataset.module = state.module;
  $$('.mod').forEach((b) => b.classList.toggle('is-on', b.dataset.mod === state.module));
  $('#search').value = state.q;
  $('#date_from').value = state.date_from;
  $('#date_to').value = state.date_to;
  $('#pdf_only').checked = state.pdf_only;
  $('#saved_only').checked = state.saved_only;
  $('#min_citations').value = state.min_citations;
  $('#min_stars').value = state.min_stars;
  const sel = $(`#paper-sort input[value="${state.sort.papers}"]`);
  if (sel) sel.checked = true;
  const rsel = $(`#repo-sort input[value="${state.sort.repos}"]`);
  if (rsel) rsel.checked = true;
  $$('#venuetypes .chip').forEach((c) =>
    c.classList.toggle('is-on', state.venue_type.has(c.dataset.vt)));
  $$('#windows .chip').forEach((c) =>
    c.classList.toggle('is-on', c.dataset.window === state.window));
  $('#board').value = state.board;
  $('#new_only').checked = state.new_only;
  const tsel = $(`#trend-sort input[value="${state.sort.trending}"]`);
  if (tsel) tsel.checked = true;
}

function wireTrending() {
  $('#windows').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $$('#windows .chip').forEach((c) => c.classList.toggle('is-on', c === chip));
    state.window = chip.dataset.window;
    state.page = 1;
    load();
  });
  $('#board').addEventListener('change', (e) => {
    state.board = e.target.value; state.page = 1; load();
  });
  $('#trendday').addEventListener('change', (e) => {
    state.day = e.target.value; state.page = 1; load();
  });
  $('#trend-sort').addEventListener('change', (e) => {
    state.sort.trending = e.target.value; state.page = 1; load();
  });
  $('#new_only').addEventListener('change', (e) => {
    state.new_only = e.target.checked; state.page = 1; load();
  });

  const panel = $('#alerts');
  $('#bell').addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) loadAlerts();
  });
  $('#alerts-close').addEventListener('click', () => { panel.hidden = true; });
  $('#alerts-seen').addEventListener('click', async () => {
    await post('/api/alerts/seen', {});
    loadAlerts();
  });
  $('#alertform').addEventListener('submit', async (e) => {
    e.preventDefault();
    const label = $('#a_label').value.trim();
    if (!label) return;
    await post('/api/alerts', {
      label, theme: $('#a_theme').value, keyword: $('#a_keyword').value.trim(),
      min_stars: +$('#a_min').value || 0, desktop: $('#a_desktop').checked,
    });
    $('#alertform').reset();
    $('#a_desktop').checked = true;
    loadAlerts();
  });
  $('#rulelist').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-aact]');
    if (!btn) return;
    const id = +btn.closest('li').dataset.id;
    const rule = state.alerts.find((a) => a.id === id);
    await post(`/api/alerts/${id}`, btn.dataset.aact === 'delete'
      ? { delete: true } : { active: rule && !rule.active });
    loadAlerts();
  });
}

function wire() {
  wireTrending();
  $$('.mod').forEach((b) => b.addEventListener('click', () => setModule(b.dataset.mod)));

  $('#search').addEventListener('input', debounce((e) => {
    state.q = e.target.value.trim();
    state.page = 1;
    load();
  }, 220));

  $('#themes').addEventListener('click', (e) => {
    const btn = e.target.closest('.theme');
    if (!btn) return;
    const slug = btn.dataset.slug;
    const on = !state.themes.has(slug);
    if (on) state.themes.add(slug); else state.themes.delete(slug);
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', String(on));
    $('#clear-themes').hidden = state.themes.size === 0;
    state.page = 1;
    load();
  });

  $('#clear-themes').addEventListener('click', () => {
    state.themes.clear();
    $$('#themes .theme').forEach((b) => {
      b.classList.remove('is-on');
      b.setAttribute('aria-pressed', 'false');
    });
    $('#clear-themes').hidden = true;
    state.page = 1;
    load();
  });

  $('#periods').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $$('#periods .chip').forEach((c) => c.classList.toggle('is-on', c === chip));
    const months = +chip.dataset.months;
    if (!months) {
      state.date_from = '';
    } else {
      const d = new Date();
      d.setMonth(d.getMonth() - months);
      state.date_from = d.toISOString().slice(0, 10);
    }
    state.date_to = '';
    $('#date_from').value = state.date_from;
    $('#date_to').value = '';
    state.page = 1;
    load();
  });

  ['date_from', 'date_to'].forEach((id) => $(`#${id}`).addEventListener('change', (e) => {
    state[id] = e.target.value;
    $$('#periods .chip').forEach((c) => c.classList.remove('is-on'));
    state.page = 1;
    load();
  }));

  $('#venuetypes').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const vt = chip.dataset.vt;
    if (state.venue_type.has(vt)) state.venue_type.delete(vt); else state.venue_type.add(vt);
    chip.classList.toggle('is-on');
    state.page = 1;
    load();
  });

  $('#paper-sort').addEventListener('change', (e) => {
    state.sort.papers = e.target.value; state.page = 1; load();
  });
  $('#repo-sort').addEventListener('change', (e) => {
    state.sort.repos = e.target.value; state.page = 1; load();
  });

  $('#pdf_only').addEventListener('change', (e) => {
    state.pdf_only = e.target.checked; state.page = 1; load();
  });
  $('#saved_only').addEventListener('change', (e) => {
    state.saved_only = e.target.checked; state.page = 1; load();
  });
  $('#min_citations').addEventListener('change', debounce((e) => {
    state.min_citations = e.target.value; state.page = 1; load();
  }, 200));
  $('#min_stars').addEventListener('change', debounce((e) => {
    state.min_stars = e.target.value; state.page = 1; load();
  }, 200));
  $('#language').addEventListener('change', (e) => {
    state.language = e.target.value; state.page = 1; load();
  });

  $('#prev').addEventListener('click', () => {
    if (state.page > 1) { state.page--; load(); $('#list').scrollTop = 0; }
  });
  $('#next').addEventListener('click', () => {
    state.page++; load(); $('#list').scrollTop = 0;
  });

  $('#list').addEventListener('click', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    if (state.module === 'trending') {
      state.selected = +row.dataset.id;
      $$('.row').forEach((el) => el.classList.toggle('is-sel', el === row));
      renderTrendDetail(row.dataset.name);
      return;
    }
    openDetail(+row.dataset.id);
  });

  $('#detail').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = +$('#detail').dataset.id;
    const act = btn.dataset.act;
    if (act === 'close') return closeDetail();
    if (act === 'alert-theme') {
      const item = state.items.find((t) => t.full_name === $('#detail').dataset.name);
      const theme = item && item.themes && item.themes[0];
      $('#alerts').hidden = false;
      await loadAlerts();
      $('#a_label').value = `${state.themeLabels[theme] || theme || 'Trending'} on the board`;
      $('#a_theme').value = theme || '';
      $('#a_label').focus();
      return;
    }
    if (act === 'save') {
      const on = btn.classList.contains('on');
      await post('/api/saved', { item_type: itemType(), item_id: id, saved: !on });
      openDetail(id); load();
    }
    if (act === 'reel') {
      const on = btn.classList.contains('on');
      await post('/api/saved', { item_type: itemType(), item_id: id, saved: true, reel_flag: !on });
      openDetail(id);
    }
    if (act === 'hide') {
      await post('/api/hidden', { item_type: itemType(), item_id: id, hidden: true });
      closeDetail(); load();
    }
  });

  $('#detail').addEventListener('change', async (e) => {
    if (e.target.dataset.act !== 'note') return;
    await post('/api/saved', {
      item_type: itemType(), item_id: +$('#detail').dataset.id,
      saved: true, note: e.target.value,
    });
    load();
  });

  // Back, forward, and a hand-edited URL all have to re-render. Without this
  // the hash is written but never read again after first load.
  window.addEventListener('hashchange', () => {
    const before = JSON.stringify([state.module, state.q, [...state.themes],
      state.sort, state.page, state.date_from, state.date_to]);
    readHash();
    const after = JSON.stringify([state.module, state.q, [...state.themes],
      state.sort, state.page, state.date_from, state.date_to]);
    if (before === after) return;
    syncControlsFromState();
    closeDetail();
    loadThemes().then(load);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== $('#search')) {
      e.preventDefault(); $('#search').focus(); $('#search').select(); return;
    }
    if (e.key === 'Escape') {
      if (document.activeElement === $('#search')) $('#search').blur();
      else closeDetail();
      return;
    }
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (typing) return;

    const idx = state.items.findIndex((it) => it.id === state.selected);
    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      const next = e.key === 'j'
        ? Math.min(state.items.length - 1, idx + 1)
        : Math.max(0, idx - 1);
      const item = state.items[next];
      if (!item) return;
      state.selected = item.id;
      $$('.row').forEach((el) => el.classList.toggle('is-sel', +el.dataset.id === item.id));
      const el = $(`.row[data-id="${item.id}"]`);
      if (el) el.scrollIntoView({ block: 'nearest' });
      if (!$('#detail').hidden) openDetail(item.id);
    }
    if (e.key === 'Enter' && state.selected) openDetail(state.selected);
    if (e.key === 'o' && state.selected) {
      const el = $(`.row[data-id="${state.selected}"]`);
      if (el && el.dataset.url) window.open(el.dataset.url, '_blank', 'noopener');
    }
    if (e.key === 's' && state.selected) {
      const item = state.items.find((it) => it.id === state.selected);
      post('/api/saved', {
        item_type: itemType(), item_id: state.selected, saved: !(item && item.is_saved),
      }).then(load);
    }
  });
}

const itemType = () => (state.module === 'papers' ? 'paper' : 'repo');

(async function init() {
  readHash();
  syncControlsFromState();
  wire();
  await loadChrome();
  loadAlerts();
  try {
    await loadThemes();
  } catch (err) {
    console.warn('themes failed to load', err);
  }
  try {
    await load();
  } catch (err) {
    $('#empty').hidden = false;
    $('#empty').innerHTML = '<h2>Could not reach the API</h2>' +
      '<p>Is the server still running?</p><code>py -3 serve.py</code>';
  }
})();
