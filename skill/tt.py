"""Shared helpers for TabSanity: Chrome profiles, session files, AppleScript, Todoist."""

import json
import os
import re
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
SKILL = Path(__file__).resolve().parent


def config():
    """config.json (personal, gitignored) if present, else the shipped example."""
    for name in ("config.json", "config.example.json"):
        f = SKILL / name
        if f.exists():
            return json.loads(f.read_text())
    return {}


DOWNLOADS = Path(os.path.expanduser(config().get("backup_dir", "~/Downloads")))

# Chrome stores times as microseconds since 1601-01-01.
WINDOWS_EPOCH_OFFSET_US = 11644473600 * 1_000_000


# ---------------------------------------------------------------- profiles

def profiles():
    """Every profile directory with its signed-in email (or None)."""
    out = {}
    for d in sorted(CHROME_DIR.iterdir()):
        if not (d.name == "Default" or d.name.startswith("Profile ")):
            continue
        email = None
        try:
            prefs = json.loads((d / "Preferences").read_text())
            info = prefs.get("account_info") or []
            if info:
                email = info[0].get("email")
        except (OSError, ValueError):
            pass
        out[d.name] = email
    return out


# ---------------------------------------------------------------- session files (SNSS)

def _pickle_string(buf, off):
    n = struct.unpack_from("<i", buf, off)[0]
    off += 4
    s = buf[off:off + n].decode("utf-8", "replace")
    return s, off + ((n + 3) & ~3)


def _pickle_string16(buf, off):
    n = struct.unpack_from("<i", buf, off)[0]
    off += 4
    s = buf[off:off + n * 2].decode("utf-16-le", "replace")
    return s, off + ((n * 2 + 3) & ~3)


def parse_session(path):
    """Replay a Chrome SNSS session file into its open windows and tabs.

    Returns {window_id: {"title": str|None, "tabs": [tab, ...]}} where each tab is
    {"id", "index", "url", "title", "pinned", "last_active_us"}.
    """
    data = Path(path).read_bytes()
    if data[:4] != b"SNSS":
        raise ValueError(f"{path} is not a session file")
    off = 8
    tab_window, tab_index, tab_nav, tab_sel = {}, {}, {}, {}
    pinned, last_active, closed_tabs, closed_windows = {}, {}, set(), set()
    window_titles = {}
    while off + 2 <= len(data):
        size = struct.unpack_from("<H", data, off)[0]
        off += 2
        if size == 0 or off + size > len(data):
            break
        cmd, p = data[off], data[off + 1:off + size]
        off += size
        try:
            if cmd == 0:  # SetTabWindow
                w, t = struct.unpack_from("<ii", p)
                tab_window[t] = w
                closed_tabs.discard(t)
            elif cmd == 2:  # SetTabIndexInWindow
                t, i = struct.unpack_from("<ii", p)
                tab_index[t] = i
            elif cmd == 6:  # UpdateTabNavigation (pickled)
                t, i = struct.unpack_from("<ii", p, 4)
                url, o = _pickle_string(p, 12)
                title, _ = _pickle_string16(p, o)
                tab_nav.setdefault(t, {})[i] = (url, title)
            elif cmd == 7:  # SetSelectedNavigationIndex
                t, i = struct.unpack_from("<ii", p)
                tab_sel[t] = i
            elif cmd == 12:  # SetPinnedState
                t = struct.unpack_from("<i", p)[0]
                pinned[t] = bool(p[4])
            elif cmd == 16:  # TabClosed
                closed_tabs.add(struct.unpack_from("<i", p)[0])
            elif cmd == 17:  # WindowClosed
                closed_windows.add(struct.unpack_from("<i", p)[0])
            elif cmd == 21:  # LastActiveTime
                t = struct.unpack_from("<i", p)[0]
                last_active[t] = struct.unpack_from("<q", p, 8)[0]
            elif cmd == 24:  # TabNavigationPathPruned
                t, i, n = struct.unpack_from("<iii", p)
                navs = tab_nav.get(t, {})
                tab_nav[t] = {(k if k < i else k - n): v for k, v in navs.items()
                              if not (i <= k < i + n)}
            elif cmd == 11:  # TabNavigationPathPrunedFromFront
                t, n = struct.unpack_from("<ii", p)
                navs = tab_nav.get(t, {})
                tab_nav[t] = {k - n: v for k, v in navs.items() if k >= n}
            elif cmd == 5:  # TabNavigationPathPrunedFromBack
                t, i = struct.unpack_from("<ii", p)
                tab_nav[t] = {k: v for k, v in tab_nav.get(t, {}).items() if k < i}
            elif cmd == 31:  # SetWindowUserTitle (pickled)
                w = struct.unpack_from("<i", p, 4)[0]
                window_titles[w], _ = _pickle_string(p, 8)
        except struct.error:
            continue

    windows = {}
    for t, w in tab_window.items():
        if t in closed_tabs or w in closed_windows:
            continue
        navs = tab_nav.get(t)
        if not navs:
            continue
        sel = tab_sel.get(t, max(navs))
        url, title = navs.get(sel) or navs[max(navs)]
        la = last_active.get(t)
        windows.setdefault(w, {"title": window_titles.get(w), "tabs": []})["tabs"].append({
            "id": t, "index": tab_index.get(t, 0), "url": url, "title": title,
            "pinned": pinned.get(t, False),
            "last_active_us": la,
        })
    for w in windows.values():
        w["tabs"].sort(key=lambda x: x["index"])
    return windows


def newest_session(profile):
    d = CHROME_DIR / profile / "Sessions"
    files = sorted(d.glob("Session_*"), key=lambda f: f.stat().st_mtime, reverse=True) if d.exists() else []
    return files[0] if files else None


def chrome_us_to_unix(us):
    """Session timestamps: Windows-epoch microseconds. Returns unix seconds or None."""
    if not us or us < WINDOWS_EPOCH_OFFSET_US:
        return None
    return (us - WINDOWS_EPOCH_OFFSET_US) / 1_000_000


# ---------------------------------------------------------------- live tabs via AppleScript

_LIST_SCRIPT = r'''
set sep to (ASCII character 31)
set out to ""
tell application "Google Chrome"
  repeat with w in windows
    set wid to id of w
    set wname to name of w
    set wmode to mode of w
    set ai to active tab index of w
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      set out to out & wid & sep & wmode & sep & ai & sep & i & sep & (id of t) & sep & (URL of t) & sep & (title of t) & sep & wname & linefeed
    end repeat
  end repeat
end tell
return out
'''


def live_tabs():
    """Every tab in every Chrome window, straight from the running browser."""
    # Chrome can refuse a read while a tab is mid-close or mid-load, so retry before giving up.
    for attempt in range(4):
        r = subprocess.run(["osascript", "-e", _LIST_SCRIPT], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(0.5 * (attempt + 1))
    else:
        raise SystemExit(f"Could not list Chrome tabs: {r.stderr.strip()}")
    out = r.stdout
    tabs = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 8:
            continue
        wid, mode, active, idx, tid, url, title = parts[:7]
        wname = "\x1f".join(parts[7:])
        tabs.append({
            "window": int(wid), "window_name": wname, "mode": mode,
            "index": int(idx), "active": int(idx) == int(active),
            "tab": int(tid), "url": url, "title": title,
        })
    return tabs


def close_tabs(pairs):
    """Close tabs by (window id, tab id). Ids are stable, so order does not matter."""
    if not pairs:
        return
    lines = ['tell application "Google Chrome"']
    for w, t in pairs:
        lines.append(f'  try\n    close (first tab of window id {int(w)} whose id is {int(t)})\n  end try')
    lines.append("end tell")
    subprocess.run(["osascript", "-e", "\n".join(lines)], check=True, capture_output=True, text=True)


def open_in_profile(profile, url):
    subprocess.Popen([CHROME_BIN, f"--profile-directory={profile}", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- URLs

_TRACKING = re.compile(r"^(utm_.*|gclid|fbclid|mc_cid|mc_eid|ref_src|si|igshid|_hsenc|_hsmi)$")
_GDOC = re.compile(r"https://docs\.google\.com/(document|spreadsheets|presentation|forms)/d/([\w-]+)")
_GDRIVE = re.compile(r"https://drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)")


def normalize(url):
    """A key under which two tabs showing the same thing collide."""
    m = _GDOC.match(url)
    if m:
        return f"gdoc:{m.group(2)}"
    m = _GDRIVE.match(url)
    if m:
        return f"gdrive:{m.group(1)}"
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)]
    host = u.netloc.lower()
    path = u.path.rstrip("/") or "/"
    if host in ("mail.google.com",):
        return f"{host}{path}?{urllib.parse.urlencode(q)}#{u.fragment}"
    return f"{u.scheme}://{host}{path}" + (f"?{urllib.parse.urlencode(sorted(q))}" if q else "")


def ids_in_text(text):
    """Normalised keys for every URL mentioned in a block of text."""
    return {normalize(m.group(0).rstrip(").,>]")) for m in re.finditer(r"https?://[^\s)\]>\"']+", text or "")}


# ---------------------------------------------------------------- Todoist

def _read_env(file, key):
    try:
        text = Path(file).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) == key:
            return m.group(2).strip().strip("'\"")
    return None


def todoist_token():
    tok = os.environ.get("TODOIST_API_TOKEN")
    tok = tok or _read_env(Path.home() / ".config/secrets.env", "TODOIST_API_TOKEN")
    if not tok:
        raise SystemExit("TODOIST_API_TOKEN not found. Add it to ~/.config/secrets.env")
    return tok


def todoist(method, path, body=None, params=None):
    url = "https://api.todoist.com/api/v1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {todoist_token()}"})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def todoist_all(path, params=None):
    params = dict(params or {}, limit=200)
    items = []
    while True:
        page = todoist("GET", path, params=params)
        items += page.get("results", [])
        if not page.get("next_cursor"):
            return items
        params["cursor"] = page["next_cursor"]


def todoist_sync():
    """Every open task, comment and project in one call (the REST list endpoints page slowly)."""
    body = urllib.parse.urlencode({"sync_token": "*",
                                   "resource_types": json.dumps(["items", "notes", "projects"])}).encode()
    req = urllib.request.Request("https://api.todoist.com/api/v1/sync", data=body,
                                 headers={"Authorization": f"Bearer {todoist_token()}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def project_paths(data=None):
    """Open projects as {id: "Parent › child"}, plus each one's open task count."""
    data = data or todoist_sync()
    live = {p["id"]: p for p in data["projects"] if not p.get("is_archived") and not p.get("is_deleted")}
    counts = {}
    for it in data["items"]:
        if not it.get("checked") and not it.get("is_deleted"):
            counts[it["project_id"]] = counts.get(it["project_id"], 0) + 1

    def path(pid):
        p = live[pid]
        parent = p.get("parent_id")
        return (path(parent) + " › " if parent in live else "") + p["name"]

    return {pid: {"path": path(pid), "tasks": counts.get(pid, 0), "inbox": bool(p.get("inbox_project"))}
            for pid, p in live.items()}


# ---------------------------------------------------------------- runs

def latest_run():
    runs = sorted(RUNS.glob("*.json"))
    if not runs:
        raise SystemExit("No snapshot yet. Run snapshot.py first.")
    return runs[-1]


def load(path=None):
    p = Path(path) if path else latest_run()
    return p, json.loads(p.read_text())


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False))
