#!/usr/bin/env python3
"""Sort the latest snapshot's tabs into buckets of things that can go.

    triage.py                 summary: each bucket's count and up to 5 examples
    triage.py --bucket NAME   every tab in one bucket, with ids for act.py
    triage.py --run PATH      use a specific snapshot instead of the latest

Each tab lands in at most one bucket, first match wins, in BUCKETS order.
"""

import argparse
import json
import math
import re
import socket
import sys
import time
import urllib.parse
from collections import defaultdict

import tt

BUCKETS = [
    ("duplicates", "Duplicates (another copy stays open)"),
    ("todoist_saved", "Already saved in a Todoist task (link is in the task)"),
    ("searches", "Search result pages"),
    ("dead_local", "Local dev pages whose server is no longer running"),
    ("auth", "Sign-in pages and finished OAuth callbacks"),
    ("browser", "Browser pages (new tab, What's New, blank)"),
    ("wrong_profile", "Open in the wrong profile (reopen in the right one)"),
    ("todoist_maybe", "Candidate Todoist tasks to check by hand (word overlap only; Claude judges each)"),
    ("todo_candidates", "Looks like a to-do (make it a Todoist task, then close)"),
]
CLOSE_BUCKETS = {"duplicates", "todoist_saved", "searches", "dead_local", "auth", "browser"}
STALE_DAYS = 30
MAYBE_THRESHOLD = 10.0

STOP = set("""the and for with from that this your you are was were have has into over about what when where
which will just more than then them they their there here also only into page home site html http https www com
google docs sheets slides drive github untitled document spreadsheet new edit view
claude artifact mail gmail inbox email copy notes""".split()) | set(tt.config().get("stop_words", []))


ACCOUNT_SCOPED = re.compile(
    r"^https://(mail|calendar|drive|photos|contacts|keep|myaccount|console\.cloud)\.google\.com/|/u/\d+/|[?&]authuser=")


def profile_rules():
    return tt.config().get("rules", [])


def right_profile(url, rules):
    host = urllib.parse.urlsplit(url).netloc.lower()
    for r in rules:
        if "host" in r and (host == r["host"] or host.endswith("." + r["host"])):
            return r["profile"]
        if "url_contains" in r and r["url_contains"] in url:
            return r["profile"]
    return None


def dup_key(t):
    u = urllib.parse.urlsplit(t["url"])
    if u.hostname in ("localhost", "127.0.0.1"):
        # Ports get reused by different projects, so the title has to agree too.
        return f"{u.netloc}{u.path.rstrip('/')}|{t['title']}"
    key = tt.normalize(t["url"])
    if not key.startswith(("gdoc:", "gdrive:")) and ACCOUNT_SCOPED.search(t["url"]):
        # /mail/u/0/ is a different mailbox in each profile, so only same-profile copies collide.
        return f"{t['profile']}|{key}"
    return key


def is_search(url):
    u = urllib.parse.urlsplit(url)
    host, path = u.netloc.lower(), u.path
    return (
        (host.endswith("google.com") and path in ("/search", "/travel/flights", "/travel/flights/search", "/travel/search"))
        or (host.endswith("booking.com") and "searchresults" in path)
        or (host in ("duckduckgo.com", "www.bing.com") and path in ("/", "/search") and "q=" in u.query)
        or (host.endswith("youtube.com") and path == "/results")
        or (host.endswith("amazon.com") and path == "/s")
    )


_port_cache = {}


def port_open(port):
    if port not in _port_cache:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                _port_cache[port] = True
        except OSError:
            _port_cache[port] = False
    return _port_cache[port]


def is_dead_local(url):
    u = urllib.parse.urlsplit(url)
    if u.hostname not in ("localhost", "127.0.0.1"):
        return False
    return not port_open(u.port or (443 if u.scheme == "https" else 80))


def is_auth(t):
    u = urllib.parse.urlsplit(t["url"])
    title = (t["title"] or "").lower()
    q = urllib.parse.parse_qs(u.query)
    return (
        (u.netloc == "accounts.google.com" and ("signin" in u.path or "oauth" in u.path))
        or ("code" in q and ("state" in q or "callback" in u.path))
        or re.search(r"/(login|signin|sign-in|sign_in)\b", u.path) is not None and ("sign in" in title or "log in" in title or "login" in title)
    )


def is_browser_page(url):
    return url.startswith(("chrome://newtab", "chrome://whats-new", "about:blank", "chrome://new-tab-page"))


def is_todo(t):
    u, title = t["url"], (t["title"] or "").lower()
    return (
        "/forms/d/" in u and "viewform" in u
        or re.search(r"/(checkout|cart|basket|payment|pay)(/|$|\?)", u) is not None
        or any(w in title for w in ("checkout", "complete your", "finish setting up", "verify your", "confirm your"))
    )


def words(text):
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in STOP and not w.isdigit()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--bucket")
    ap.add_argument("--no-todoist", action="store_true")
    args = ap.parse_args()

    run_path, run = tt.load(args.run)
    if args.bucket:
        return show_bucket(run, args.bucket)

    tabs = run["tabs"]
    rules = profile_rules()
    now = time.time()
    assigned = {}  # tab id -> bucket record

    def put(t, bucket, **extra):
        if t["tab"] not in assigned:
            assigned[t["tab"]] = {"bucket": bucket, **extra}

    protected = {t["tab"] for t in tabs if t.get("pinned") or t.get("active")}

    # 1. duplicates: keep the copy in the right profile, else the active one, else the most recent
    groups = defaultdict(list)
    for t in tabs:
        if not is_browser_page(t["url"]):
            groups[dup_key(t)].append(t)
    for key, ts in groups.items():
        if len(ts) < 2:
            continue
        want = right_profile(ts[0]["url"], rules)
        keep = max(ts, key=lambda t: (t["profile"] == want, t["tab"] in protected, t.get("last_active") or 0))
        # The surviving copy must not be swept up by a later bucket, or nothing survives.
        protected.add(keep["tab"])
        for t in ts:
            if t is not keep and t["tab"] not in protected:
                put(t, "duplicates", keeps=f'{keep["profile"]} / {keep["window_name"]} (tab {keep["tab"]})')

    # 2. Todoist
    tasks = []
    if not args.no_todoist:
        data = tt.todoist_sync()
        projects = {p["id"]: p["name"] for p in data["projects"]}
        notes = defaultdict(str)
        for n in data["notes"]:
            if not n.get("is_deleted"):
                notes[n["item_id"]] += "\n" + (n.get("content") or "")
        for it in data["items"]:
            if it.get("checked") or it.get("is_deleted"):
                continue
            text = f'{it["content"]}\n{it.get("description") or ""}{notes.get(it["id"], "")}'
            tasks.append({"id": it["id"], "content": it["content"], "project": projects.get(it["project_id"], ""),
                          "keys": tt.ids_in_text(text), "words": words(it["content"] + " " + (it.get("description") or "")),
                          "hosts": {urllib.parse.urlsplit(m).netloc for m in re.findall(r"https?://[^\s)\]>\"']+", text)}})
        by_key = defaultdict(list)
        for task in tasks:
            for k in task["keys"]:
                by_key[k].append(task)
        for t in tabs:
            path = urllib.parse.urlsplit(t["url"]).path.strip("/")
            hits = by_key.get(tt.normalize(t["url"])) if path else None
            if hits and t["tab"] not in protected:
                put(t, "todoist_saved", task=brief(hits[0]))

    # 3. cheap closes
    for t in tabs:
        if t["tab"] in protected:
            continue
        if is_search(t["url"]):
            put(t, "searches")
        elif is_dead_local(t["url"]):
            put(t, "dead_local")
        elif is_auth(t):
            put(t, "auth")
        elif is_browser_page(t["url"]):
            put(t, "browser")

    # 4. wrong profile
    for t in tabs:
        want = right_profile(t["url"], rules)
        if want and t["profile"] not in ("unknown", want):
            put(t, "wrong_profile", move_to=want, move_to_email=run["profiles"].get(want))

    # 5. weak Todoist matches: rare title words shared with a task. Words are weighted by how
    #    few tasks use them, so "report" or "august" alone never pairs a tab with a task.
    if tasks:
        df = defaultdict(int)
        for task in tasks:
            for w in task["words"]:
                df[w] += 1
        n = len(tasks)

        def idf(w):
            return math.log(n / (1 + df[w]))

        for t in tabs:
            if t["tab"] in assigned or t["tab"] in protected:
                continue
            tw = words(t["title"])
            if len(tw) < 2:
                continue
            host = urllib.parse.urlsplit(t["url"]).netloc
            scored = []
            for task in tasks:
                shared = tw & task["words"]
                if len(shared) < 2:
                    continue
                sc = sum(idf(w) for w in shared) + (4 if host and host in task["hosts"] else 0)
                if sc >= MAYBE_THRESHOLD:
                    scored.append((sc, task, shared))
            if scored:
                scored.sort(key=lambda x: -x[0])
                put(t, "todoist_maybe", candidates=[
                    dict(brief(task), shared=sorted(shared), score=round(sc, 1)) for sc, task, shared in scored[:3]])

    # 6. to-do candidates
    for t in tabs:
        if t["tab"] not in protected and is_todo(t):
            put(t, "todo_candidates")

    for t in tabs:
        rec = assigned.get(t["tab"])
        t["bucket"] = rec["bucket"] if rec else ("keep" if t["tab"] in protected else None)
        t["why"] = {k: v for k, v in (rec or {}).items() if k != "bucket"}
        la = t.get("last_active")
        t["stale_days"] = int((now - la) / 86400) if la and now - la > STALE_DAYS * 86400 else None

    # whole windows where every tab is in a close bucket
    by_win = defaultdict(list)
    for t in tabs:
        by_win[t["window"]].append(t)
    run["whole_windows"] = [
        {"window": w, "name": ts[0]["window_name"], "profile": ts[0]["profile"], "tabs": len(ts)}
        for w, ts in by_win.items()
        if all(t["bucket"] in CLOSE_BUCKETS or (t["tab"] in protected and len(ts) == 1) for t in ts)
        and any(t["bucket"] in CLOSE_BUCKETS for t in ts)
    ]
    run["triaged"] = now
    tt.save(run_path, run)
    summary(run)


def brief(task):
    return {"id": task["id"], "content": task["content"], "project": task["project"],
            "url": f"https://app.todoist.com/app/task/{task['id']}"}


def line(t):
    extra = ""
    w = t.get("why") or {}
    if "keeps" in w:
        extra = f"  [kept: {w['keeps']}]"
    elif "task" in w:
        extra = f"  [task: {w['task']['content'][:60]}  ({w['task']['project']})]"
    elif "candidates" in w:
        extra = "".join(f"\n      ? {c['content'][:70]}  ({c['project']}) [{', '.join(c['shared'])}] {c['url']}"
                        for c in w["candidates"])
    elif "move_to" in w:
        extra = f"  [-> {w['move_to']} {w.get('move_to_email') or ''}]"
    stale = f"  [not viewed in {t['stale_days']}d]" if t.get("stale_days") else ""
    return f'{t["tab"]}  {t["profile"]}  {(t["title"] or "")[:70]}\n    {t["url"][:140]}{extra}{stale}'


def summary(run):
    tabs = run["tabs"]
    print(f"{len(tabs)} tabs. Buckets, in the order to work them:\n")
    for name, label in BUCKETS:
        ts = [t for t in tabs if t["bucket"] == name]
        if not ts:
            continue
        print(f"## {name}: {len(ts)}  {label}")
        for t in ts[:5]:
            print("  " + line(t).replace("\n", "\n  "))
        if len(ts) > 5:
            print(f"  ...and {len(ts) - 5} more  (triage.py --bucket {name})")
        print()
    stale = [t for t in tabs if t.get("stale_days") and t["bucket"] in (None,)]
    if stale:
        print(f"## stale: {len(stale)} unbucketed tabs not viewed in {STALE_DAYS}+ days  (triage.py --bucket stale)\n")
    for w in run.get("whole_windows", []):
        print(f"## whole window can go: {w['name']} ({w['profile']}, {w['tabs']} tabs, window {w['window']})")
    emails = defaultdict(list)
    for p, e in run["profiles"].items():
        if e:
            emails[e].append(p)
    for e, ps in emails.items():
        if len(ps) > 1:
            print(f"note: {', '.join(ps)} are both signed in as {e}")
    kept = sum(1 for t in tabs if t["bucket"] in (None, "keep"))
    closable = sum(1 for t in tabs if t["bucket"] in CLOSE_BUCKETS)
    print(f"\n{closable} tabs in close buckets. {kept} left untouched.")


def show_bucket(run, name):
    if name == "stale":
        ts = [t for t in run["tabs"] if t.get("stale_days") and t.get("bucket") is None]
        ts.sort(key=lambda t: -t["stale_days"])
    else:
        ts = [t for t in run["tabs"] if t.get("bucket") == name]
    print(f"{name}: {len(ts)}")
    for t in ts:
        print(line(t))


if __name__ == "__main__":
    sys.exit(main())
