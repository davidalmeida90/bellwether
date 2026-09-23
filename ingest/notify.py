"""Alerts over the trending boards.

A rule is a row in `alerts`: a theme, a keyword, a minimum number of stars
gained, and whether a hit should also raise a Windows notification. Rules are
written from the interface, so adding one never needs a restart or a code edit.

Evaluation runs right after the boards are captured. Each hit is recorded once
per rule per repository per day, which is what keeps a repository that trends
for a week from notifying every morning.

  py -3 ingest/notify.py --run            evaluate today's boards
  py -3 ingest/notify.py --list           show the rules and their unread hits
  py -3 ingest/notify.py --add "AI agents on the board" --theme ai-agents --min-stars 300
  py -3 ingest/notify.py --test           send one desktop notification
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.common import connect, today, utf8_stdout  # noqa: E402

APP_ID = "Microsoft.Windows.Explorer"   # a registered id, so the toast is allowed to show
MAX_TOASTS = 5                          # per run, so a busy morning stays readable


# ------------------------------------------------------------------- toast ---
def toast(title: str, body: str, timeout: int = 20) -> bool:
    """Raise a Windows notification, returning whether it went out.

    WinRT is reached through PowerShell rather than a Python package, so the
    project keeps its dependency list to what the ingest already needs. Any
    failure is reported by the return value: an alert that cannot pop is still
    recorded in the database and shown in the interface.
    """
    if sys.platform != "win32":
        return False
    script = f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
<toast><visual><binding template="ToastGeneric">
<text>{escape(title)}</text><text>{escape(body)}</text>
</binding></visual></toast>
'@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($toast)
"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------- rules ---
def add_rule(con, label: str, theme: str = "", keyword: str = "", min_stars: int = 0,
             board: str = "", desktop: bool = True) -> int:
    cur = con.execute(
        "INSERT INTO alerts(label, theme, keyword, min_stars, board, desktop) "
        "VALUES(?,?,?,?,?,?)",
        (label, theme or "", keyword or "", int(min_stars), board or "", int(desktop)))
    con.commit()
    return cur.lastrowid


def rules(con, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM alerts" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    out = []
    for r in con.execute(sql):
        d = dict(r)
        d["unread"] = con.execute(
            "SELECT count(*) FROM alert_hits WHERE alert_id=? AND seen=0", (r["id"],)
        ).fetchone()[0]
        out.append(d)
    return out


def _matches(rule: dict, row) -> bool:
    if rule["board"] and row["board"] != rule["board"]:
        return False
    if rule["min_stars"] and (row["stars_window"] or 0) < rule["min_stars"]:
        return False
    if rule["theme"] and rule["theme"] not in json.loads(row["themes_json"] or "[]"):
        return False
    if rule["keyword"]:
        hay = f"{row['full_name']} {row['description'] or ''}".lower()
        if rule["keyword"].lower() not in hay:
            return False
    return True


def run(con, day: str | None = None, verbose: bool = True) -> dict:
    """Evaluate every active rule against one day of boards."""
    day = day or today()
    board_rows = con.execute(
        "SELECT * FROM trending WHERE day=? ORDER BY rank", (day,)).fetchall()
    stats = {"rules": 0, "hits": 0, "toasts": 0}
    toasts = []
    for rule in rules(con):
        stats["rules"] += 1
        for row in board_rows:
            if not _matches(rule, row):
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO alert_hits(alert_id, day, full_name, url, "
                "description, stars_window, themes_json) VALUES(?,?,?,?,?,?,?)",
                (rule["id"], day, row["full_name"], row["url"], row["description"],
                 row["stars_window"], row["themes_json"]))
            if cur.rowcount:
                stats["hits"] += 1
                if rule["desktop"]:
                    toasts.append((rule["label"], row["full_name"], row["stars_window"]))
    con.commit()

    for label, name, gained in toasts[:MAX_TOASTS]:
        plus = f" +{gained} stars today" if gained else ""
        if toast(f"Trending: {label}", f"{name}{plus}"):
            stats["toasts"] += 1
    if len(toasts) > MAX_TOASTS:
        extra = len(toasts) - MAX_TOASTS
        if toast("Trending alerts", f"{extra} more repositories matched your rules"):
            stats["toasts"] += 1
    if verbose:
        print(f"  {stats['rules']} rules, {stats['hits']} new hits, "
              f"{stats['toasts']} notifications")
    return stats


def mark_seen(con, alert_id: int | None = None) -> int:
    sql = "UPDATE alert_hits SET seen=1 WHERE seen=0"
    params = ()
    if alert_id:
        sql += " AND alert_id=?"
        params = (alert_id,)
    n = con.execute(sql, params).rowcount
    con.commit()
    return n


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Trending alerts")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", metavar="LABEL")
    ap.add_argument("--theme", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--min-stars", type=int, default=0)
    ap.add_argument("--board", default="")
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--day")
    args = ap.parse_args()

    con = connect()
    if args.add:
        rid = add_rule(con, args.add, args.theme, args.keyword, args.min_stars,
                       args.board, not args.no_desktop)
        print(f"alert {rid}: {args.add}")
    if args.run:
        print("Trending alerts", flush=True)
        run(con, args.day)
    if args.list:
        for r in rules(con, active_only=False):
            state = "on " if r["active"] else "off"
            print(f"  [{state}] {r['id']:>2} {r['label']:<34} theme={r['theme'] or '-':<14} "
                  f"keyword={r['keyword'] or '-':<12} min={r['min_stars']:<5} "
                  f"unread={r['unread']}")
    if args.test:
        ok = toast("Bellwether", "Desktop notifications are working")
        print("notification sent" if ok else "notification could not be sent")
    if not any([args.add, args.run, args.list, args.test]):
        print("nothing to do; pass --run, --list, --add or --test", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
