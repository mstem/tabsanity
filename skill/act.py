#!/usr/bin/env python3
"""Act on a triaged snapshot. Every command re-reads the live browser first and skips any tab
whose id is gone or whose URL has changed since the snapshot.

    act.py close --bucket searches [--except ID ...]   close a whole bucket
    act.py close ID [ID ...]                           close specific tabs
    act.py close-window WINDOW_ID                      close every tab in a window
    act.py attach TAB_ID:TASK_ID [...]                 comment the tab's link on a task, then close it
    act.py new-task TAB_ID:PROJECT_ID [...]            make a task holding the link in that project, then close it
    act.py projects [WORD ...]                         list open projects (id, path, task count) to pick from
    act.py move ID [...] | --bucket wrong_profile      reopen in the right profile, then close the original

All commands take --dry-run and --run PATH (default: the latest snapshot).
"""

import argparse
import json
import sys
import time
from datetime import datetime

import tt


def log(action, t, extra=""):
    with (tt.RUNS / "actions.log").open("a") as fh:
        fh.write(f'{datetime.now():%Y-%m-%d %H:%M:%S}\t{action}\t{t["profile"]}\t{t["url"]}\t{extra}\n')


def resolve(run, ids, bucket, excluded):
    tabs = {t["tab"]: t for t in run["tabs"]}
    if bucket:
        if "triaged" not in run:
            raise SystemExit("Snapshot has no buckets yet. Run triage.py first.")
        chosen = [t for t in run["tabs"] if t.get("bucket") == bucket]
    else:
        missing = [i for i in ids if i not in tabs]
        if missing:
            raise SystemExit(f"Not in the snapshot: {missing}")
        chosen = [tabs[i] for i in ids]
    return [t for t in chosen if t["tab"] not in excluded]


def still_there(chosen):
    """Split tabs into (live, skipped) by checking id and URL against the running browser."""
    live = {t["tab"]: t for t in tt.live_tabs()}
    ok, skipped = [], []
    for t in chosen:
        now = live.get(t["tab"])
        if not now:
            skipped.append((t, "already closed"))
        elif tt.normalize(now["url"]) != tt.normalize(t["url"]):
            skipped.append((t, f"now showing {now['url'][:80]}"))
        else:
            t["window"] = now["window"]
            ok.append(t)
    return ok, skipped


def report(verb, done, skipped, dry):
    word = f"dry run, would be {verb}" if dry else verb
    print(f"{word}: {len(done)}")
    for t in done:
        print(f'  {t["profile"]}  {(t["title"] or "")[:70]}\n    {t["url"][:120]}')
    for t, why in skipped:
        print(f'  skipped ({why}): {(t["title"] or t["url"])[:70]}')


def cmd_close(run, args):
    chosen = resolve(run, args.ids, args.bucket, set(args.exclude))
    ok, skipped = still_there(chosen)
    if not args.dry_run:
        tt.close_tabs([(t["window"], t["tab"]) for t in ok])
        for t in ok:
            log("close", t, args.bucket or "")
    report("closed", ok, skipped, args.dry_run)


def cmd_close_window(run, args):
    wid = int(args.ids[0])
    chosen = [t for t in run["tabs"] if t["window"] == wid]
    if not chosen:
        raise SystemExit(f"No window {wid} in the snapshot")
    ok, skipped = still_there(chosen)
    if not args.dry_run:
        tt.close_tabs([(t["window"], t["tab"]) for t in ok])
        for t in ok:
            log("close-window", t, str(wid))
    report("closed", ok, skipped, args.dry_run)


def link(t):
    title = (t["title"] or t["url"]).replace("[", "(").replace("]", ")")
    return f'[{title}]({t["url"]})'


def cmd_attach(run, args):
    pairs = []
    for p in args.ids:
        tab_id, task_id = p.split(":", 1)
        pairs.append((int(tab_id), task_id))
    tabs = {t["tab"]: t for t in run["tabs"]}
    ok, skipped = still_there([tabs[i] for i, _ in pairs])
    task_for = dict(pairs)
    done = []
    for t in ok:
        task_id = task_for[t["tab"]]
        if args.dry_run:
            done.append(t)
            continue
        tt.todoist("POST", "/comments", {"task_id": task_id, "content": f"Was open as a tab: {link(t)}"})
        tt.close_tabs([(t["window"], t["tab"])])
        log("attach", t, task_id)
        done.append(t)
    report("attached and closed", done, skipped, args.dry_run)
    for t in done:
        print(f'    -> https://app.todoist.com/app/task/{task_for[t["tab"]]}')


def cmd_new_task(run, args):
    """Every new task goes into a project. Inbox only when it is named explicitly."""
    projects = tt.project_paths()
    inbox = next(pid for pid, p in projects.items() if p["inbox"])
    pairs = []
    for p in args.ids:
        if ":" not in p:
            raise SystemExit(f"{p}: give TAB_ID:PROJECT_ID (find one with `act.py projects WORD`, or use :inbox)")
        tab_id, pid = p.split(":", 1)
        pid = inbox if pid == "inbox" else pid
        if pid not in projects:
            raise SystemExit(f"No open project with id {pid}")
        pairs.append((int(tab_id), pid))
    tabs = {t["tab"]: t for t in run["tabs"]}
    ok, skipped = still_there([tabs[i] for i, _ in pairs])
    project_for = dict(pairs)
    done = []
    for t in ok:
        pid = project_for[t["tab"]]
        task = None
        if not args.dry_run:
            task = tt.todoist("POST", "/tasks", {"content": link(t), "project_id": pid})
            tt.close_tabs([(t["window"], t["tab"])])
            log("new-task", t, f'{task["id"]} {projects[pid]["path"]}')
        done.append((t, pid, task))
    report("made into tasks and closed", [t for t, _, _ in done], skipped, args.dry_run)
    for t, pid, task in done:
        where = f"https://app.todoist.com/app/task/{task['id']}" if task else ""
        print(f'    -> {projects[pid]["path"]}  {where}')


def cmd_projects(run, args):
    words = [w.lower() for w in args.ids]
    rows = sorted(tt.project_paths().items(), key=lambda kv: kv[1]["path"].lower())
    for pid, p in rows:
        if all(w in p["path"].lower() for w in words):
            print(f'{pid}  {p["path"]}  ({p["tasks"]} open)')


def window_profiles():
    out = {}
    for prof in tt.profiles():
        f = tt.newest_session(prof)
        if f:
            for wid in tt.parse_session(f):
                out[wid] = prof
    return out


def cmd_move(run, args):
    chosen = resolve(run, args.ids, args.bucket, set(args.exclude))
    for t in chosen:
        if not t.get("why", {}).get("move_to"):
            raise SystemExit(f'Tab {t["tab"]} has no target profile; run triage.py or pick from wrong_profile')
    ok, skipped = still_there(chosen)
    moved = []
    for t in ok:
        target = t["why"]["move_to"]
        if args.dry_run:
            moved.append(t)
            continue
        before = {x["tab"] for x in tt.live_tabs()}
        tt.open_in_profile(target, t["url"])
        # Chrome writes the session file a few seconds after the tab opens, so poll for it.
        new_tab, verified = None, False
        for _ in range(20):
            time.sleep(1)
            fresh = [x for x in tt.live_tabs() if x["tab"] not in before
                     and tt.normalize(x["url"]) == tt.normalize(t["url"])]
            if fresh:
                new_tab = fresh[0]
                if window_profiles().get(new_tab["window"]) == target:
                    verified = True
                    break
        if verified:
            tt.close_tabs([(t["window"], t["tab"])])
            log("move", t, target)
            moved.append(t)
        elif new_tab:
            skipped.append((t, f"opened a new copy but could not confirm it landed in {target}; original left open"))
        else:
            skipped.append((t, f"no new tab appeared in {target}; original left open"))
    report("reopened in the right profile and closed the original", moved, skipped, args.dry_run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["close", "close-window", "attach", "new-task", "move", "projects"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--bucket")
    ap.add_argument("--except", dest="exclude", nargs="*", type=int, default=[])
    ap.add_argument("--run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.command in ("close", "move"):
        args.ids = [int(i) for i in args.ids]
        if not args.ids and not args.bucket:
            raise SystemExit("Give tab ids or --bucket")
    elif not args.ids and args.command != "projects":
        raise SystemExit("Give ids")
    _, run = tt.load(args.run)
    {"close": cmd_close, "close-window": cmd_close_window, "attach": cmd_attach,
     "new-task": cmd_new_task, "move": cmd_move, "projects": cmd_projects}[args.command](run, args)


if __name__ == "__main__":
    sys.exit(main())
