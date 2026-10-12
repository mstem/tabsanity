#!/usr/bin/env python3
"""Snapshot every open Chrome tab, label each with its profile, and write a restore file.

Session-file window and tab ids are the same ids AppleScript reports, so the
profile of every live window is read off directly rather than inferred.
"""

import csv
import html
import shutil
import sys
import time
from datetime import datetime

import tt


def main():
    profiles = tt.profiles()
    by_window, session_tabs = {}, {}
    for prof in profiles:
        f = tt.newest_session(prof)
        if not f:
            continue
        for wid, w in tt.parse_session(f).items():
            by_window[wid] = prof
            for t in w["tabs"]:
                session_tabs[t["id"]] = t

    tabs = []
    for t in tt.live_tabs():
        if t["mode"] == "incognito":
            continue
        s = session_tabs.get(t["tab"], {})
        prof = by_window.get(t["window"], "unknown")
        t.update(profile=prof, email=profiles.get(prof), pinned=s.get("pinned", False),
                 last_active=tt.chrome_us_to_unix(s.get("last_active_us")))
        tabs.append(t)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tt.RUNS.mkdir(exist_ok=True)
    run = tt.RUNS / f"{stamp}.json"
    tt.save(run, {"taken": time.time(), "profiles": profiles, "tabs": tabs})

    backup = write_backup(stamp, tabs)

    hist = tt.RUNS / "history.csv"
    new = not hist.exists()
    with hist.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["when", "tabs", "windows"])
        w.writerow([stamp, len(tabs), len({t["window"] for t in tabs})])

    counts = {}
    for t in tabs:
        key = f'{t["profile"]} ({t["email"] or "no account"})'
        counts.setdefault(key, set()).add(t["window"])
        counts[key + "#n"] = counts.get(key + "#n", 0) + 1
    print(f"{len(tabs)} tabs in {len({t['window'] for t in tabs})} windows")
    for key in sorted(k for k in counts if not k.endswith("#n")):
        print(f"  {key}: {counts[key + '#n']} tabs, {len(counts[key])} windows")
    unknown = sum(1 for t in tabs if t["profile"] == "unknown")
    if unknown:
        print(f"  {unknown} tabs in windows with no session record yet (profile unknown; never moved or closed by profile rules)")
    print(f"run: {run}")
    print(f"backup: {backup}")


def write_backup(stamp, tabs):
    """An HTML page of every tab, grouped by profile and window. Opening it restores anything."""
    groups = {}
    for t in tabs:
        groups.setdefault(t["profile"], {}).setdefault((t["window"], t["window_name"]), []).append(t)
    parts = [f"<!doctype html><meta charset=utf-8><title>Tabs {stamp}</title>",
             "<style>body{font:15px/1.5 Georgia,serif;max-width:900px;margin:2em auto;padding:0 16px;background:#fff;color:#222}"
             "h2{margin-top:2em}h3{color:#555;font-weight:normal}a{color:#1a4c8b}</style>",
             f"<h1>Open tabs, {stamp.replace('_', ' ')}</h1>"]
    for prof, wins in groups.items():
        parts.append(f"<h2>{html.escape(prof)}</h2>")
        for (wid, wname), ts in wins.items():
            parts.append(f"<h3>{html.escape(wname)} ({len(ts)})</h3><ol>")
            parts += [f'<li><a href="{html.escape(t["url"])}">{html.escape(t["title"] or t["url"])}</a>' for t in ts]
            parts.append("</ol>")
    local = tt.RUNS / f"tabs-backup-{stamp}.html"
    local.write_text("\n".join(parts))
    dest = tt.DOWNLOADS / local.name
    if tt.DOWNLOADS.exists() and not dest.exists():
        shutil.copyfile(local, dest)
        return dest
    return local


if __name__ == "__main__":
    sys.exit(main())
