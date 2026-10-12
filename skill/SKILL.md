---
name: tabsanity
description: Cut down the user's open Chrome tabs across all their profiles. Finds duplicate tabs (including the same Google Doc open in two profiles), search pages, dead localhost pages, sign-in leftovers, tabs whose link is already saved in a Todoist task, tabs that belong on an existing Todoist task, and tabs open in the wrong profile, then closes, attaches or moves them one approved bucket at a time. Use when asked to "triage my tabs", "tab triage", "/tabsanity", "tabsanity", "close duplicate tabs", "I have too many tabs", "clean up Chrome", or "which tabs can I close".
---

# TabSanity

macOS and Google Chrome only. Python 3 standard library only. `SKILL_DIR` below means this skill's directory. Runs, backups and the action log go in `runs/` next to it.

Setup: copy `config.example.json` to `config.json` and fill in where the restore page goes, which domains belong in which profile, and your own name as stop words. Todoist features need `TODOIST_API_TOKEN` in the environment or in `~/.config/secrets.env`; without it, run `triage.py --no-todoist`.

How it knows the profile: Chrome's AppleScript has no profile property, but the window and tab ids in each profile's `Sessions/Session_*` file are the same ids AppleScript reports. `snapshot.py` reads those files, so every window's profile is a lookup, never a guess. A window opened in the last few seconds may not be in the session file yet and shows as `unknown`. Nothing is closed or moved by profile rules while it is unknown.

## The flow

1. Snapshot. This also writes a restore page of every open tab to `backup_dir` before anything is touched:
   ```sh
   python3 SKILL_DIR/snapshot.py
   ```
   Report the total, the per-profile counts and the backup path. Say that opening the backup file restores any tab.

2. Triage:
   ```sh
   python3 SKILL_DIR/triage.py
   ```
   This prints every bucket with its count and up to 5 examples. Pull the whole of a bucket with `--bucket NAME`.

3. Work the buckets **one per question**, in this order. For each one, show the count and at least 5 real examples (title + URL, the kept copy for duplicates, the task for Todoist ones), say whether it can be undone, then ask yes/no. Never close a bucket the user has not seen. If they want some tabs spared, use `--except ID ...`.

   - `duplicates`: `act.py close --bucket duplicates`. The kept copy is the one in the right profile, then the active tab, then the most recently viewed. Two copies of the same Google Doc on different doc tabs (`?tab=t.…`) count as duplicates, so mention it if the examples include some.
   - `todoist_saved`: the exact link is already in a task's title, description or comments. Show the task. `act.py close --bucket todoist_saved`.
   - `searches`, `dead_local`, `auth`, `browser`: cheap closes. These can be offered together in one question if each is listed with its own count and examples.
   - `wrong_profile`: `act.py move --bucket wrong_profile`. It opens the URL in the right profile, waits until the session file confirms the new tab landed there, and only then closes the original. If it cannot confirm, it leaves the original open and says so. Rules are in `config.json`; add a rule when the user says a domain belongs elsewhere.
   - `todoist_maybe`: see below.
   - `todo_candidates`: checkouts, forms and "finish setting up" pages. Offer to make each one a task in the right project (see "Picking a project" below), then `act.py new-task TAB_ID:PROJECT_ID ...` creates it and closes the tab.
   - Whole windows: triage lists any window where every tab is in a close bucket. Offer `act.py close-window WINDOW_ID`.
   - `stale` (`--bucket stale`): unbucketed tabs not viewed in 30+ days. Show them as a list the user picks from. Age alone is never a reason to close.

4. Finish with before and after counts. Re-run `snapshot.py`; it appends to `runs/history.csv`, so the trend is visible across runs.

## Judging `todoist_maybe`

The script only proposes pairings from shared rare words, and most of them are wrong ("LM Studio" vs a Telegram task, "August report" vs a different August report). Read every candidate yourself before showing anything:

- Keep a pairing only when the tab is plainly material for that specific task: the same named project, person, event or document. Examples that pass: a "Costa Rica budget" sheet → "Book Costa Rica trip"; a conference's own site → "Register for the conference?".
- Drop pairings that rest on generic words (election, data, report, guide, vision) or on email-subject tasks whose words overlap by chance.
- Present the survivors as tab title + URL → task text + project + task link, in one list, and say how many candidates were dropped.
- On a yes: `act.py attach TAB_ID:TASK_ID ...` posts the link as a comment on the task ("Was open as a tab: [title](url)") and closes the tab.

If there are more than ~60 candidates, hand the judging to a subagent with the `--bucket todoist_maybe` output and these criteria, and present only what it keeps.

## Picking a project

Every new task goes into the project it belongs to, never into Inbox by default. A flight checkout belongs in travel, a tax form in the tax project, a work doc in a work project.

1. List candidates: `act.py projects WORD` (substring match on the full path, e.g. `act.py projects trav`). With no word it lists all of them.
2. Pick the project whose existing tasks look like this tab. When two names fit, prefer the one with real tasks in it: a "travel" project with dozens of open tasks beats a "trips" project holding one unrelated task.
3. Show the choice in the confirmation as its full path ("Life › travel"), next to the tab title and URL, so the user can redirect it before anything is written.
4. Use `TAB_ID:inbox` only when the user says Inbox or nothing fits, and say that nothing fit.

## Safety

- Every command re-reads the live browser first and skips any tab that has closed or now shows a different URL. Closes go by window id + tab id, never by position.
- `--dry-run` on every `act.py` command. Use it when unsure what a bucket holds.
- Every action is appended to `runs/actions.log` (time, action, profile, URL, task or bucket).
- Pinned tabs and each window's active tab are never put in a close bucket. Incognito windows are skipped.
- When two profiles are signed in to the same account, triage notes it; nothing acts on it.

## Gotchas

- Todoist reads use the sync endpoint (`POST /api/v1/sync`, items + notes + projects): one call, a few seconds. Listing `/api/v1/tasks` page by page takes 40+ seconds for an account with a few thousand tasks.
- Ports on localhost get reused by different projects, so localhost duplicates also have to match on title.
- Bare homepages (`app.example.com/`) are too generic to count as "already saved in Todoist" and are skipped for that bucket.
- If `live_tabs` fails with an AppleScript error, it retries; a persistent failure usually means Chrome is showing a modal dialog.
