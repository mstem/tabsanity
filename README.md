# TabSanity

A Claude Code skill that cuts down open Chrome tabs across every profile on a Mac. It finds duplicate tabs (including the same Google Doc open in two profiles), search pages, dead localhost pages, sign-in leftovers, tabs whose link is already saved in a Todoist task, tabs that belong on an existing Todoist task, and tabs open in the wrong profile. It then closes, attaches or moves them one approved batch at a time, after writing a restore page of every open tab.

It reads Chrome's own session files to tell which profile each window belongs to, since Chrome's AppleScript interface does not expose profiles.

## Install

1. Clone this repo.
2. Copy `skill/config.example.json` to `skill/config.json` and edit it.
3. Link the skill into Claude Code: `ln -s "$PWD/skill" ~/.claude/skills/tabsanity`
4. Optional, for the Todoist buckets: put `TODOIST_API_TOKEN=...` in `~/.config/secrets.env`.
5. Allow your terminal to control Chrome when macOS asks.

Then run `/tabsanity` in Claude Code. The full flow is in `skill/SKILL.md`.

Requires macOS, Google Chrome and Python 3. No third-party packages.
