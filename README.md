# token-checkup

A Claude Code skill that keeps your sessions from quietly getting expensive. It measures token use
from your own local transcripts, keeps what every session loads at the start lean, keeps Claude's
memory true, and can install two guard rails: earlier auto-compaction and a context-size warning.

> **Independent project.** Not affiliated with or endorsed by Anthropic.

## Why

Every tool call re-reads the whole session. These numbers are from one person's machine, over 14
days in October 2026:

- a session started at about 70k tokens before any work, yet the average model call re-read about
  300k, because 39 sessions grew past 400k and only 4 ever compacted (a 1M window rarely fills);
- the start itself grew from about 72k to 84k on the day of an app update, and a few thousand more
  with new connectors, skills and hook text, without anyone noticing.

Replaying those two weeks with compaction at 300k (`measure.py --simulate 300000`) cut the context
re-read by about 43%. That is 43% less context re-read, not 43% less cost: cache reads are billed at
a fraction of fresh input. It is an estimate, and your numbers will differ: the skill measures them.

## What it does

- Asks first: setup, a checkup now, or numbers only; which project; for setup, the compaction window
  (with an estimate on your own data), the context guard, whether weekly runs may fix facts in
  memory (off unless you say yes), and when the weekly run happens.
- Measures with stdlib-only Python scripts:

| Script | What it reports or does |
| --- | --- |
| `scripts/measure.py` | First-call context (also by Claude Code version), peak size, compactions, mean context per call, cache reads, tools that fill sessions. Week over week and against a saved baseline. `--simulate N` estimates what compaction at N tokens would save. |
| `scripts/audit_start.py` | What you control at session start (CLAUDE.md files, memory index, skill descriptions, hooks, compaction settings) and what grew in what transcripts record before the first answer. |
| `scripts/memory_check.py` | Memory index problems, missing paths, dead links, memories due for a fact check or waiting for your answer, archive and split candidates. It never edits the index. Optional backup, keeping the last four per project. |
| `scripts/context_guard.py` | `UserPromptSubmit` hook: the first time a session passes 250k, 400k, 550k, 700k and 850k tokens, it asks Claude to open its reply with one line (a request, not guaranteed). |
| `scripts/setup.py` | Installs or removes the hook and `"autoCompactWindow"` in your user-level settings, backing up settings and restoring your previous value on uninstall. |

- A weekly run uses the scripts first and sends at most two small subagents to fact-check only the
  memories that changed or are due, so the checkup costs far less than it saves.
- Hands over a one-page report with week-over-week numbers, what was fixed, and numbered proposals.

What it never does: change the memory index, archive, move or delete memories, edit a CLAUDE.md, or
change settings, hooks or connectors without your yes; fix facts in memory files unless you enabled
that at setup and the backup of that run worked; run git; block a prompt. The context guard asks
Claude to open its reply with one line; that is a request to the model, not guaranteed.

## Where it looks

Claude Code keeps one transcript per session in `~/.claude/projects/<key>/`, where `<key>` is the
project's absolute path with every character that is not a letter or a digit replaced by `-` (so
`/Users/me/my app` becomes `-Users-me-my-app`). Memory is in `~/.claude/projects/<key>/memory/`.
The `--days` window picks transcripts by file modification time, so a long session counts in the
window where it was last active. If `$CLAUDE_CONFIG_DIR` is set, the scripts look there instead of
`~/.claude`.

## The intake questions

1. Mode: setup, a checkup now, or numbers only.
2. Project: the folder you work in (default: the current one).
3. For setup: the compaction window (300000 recommended), whether to install the context guard,
   whether weekly runs may correct proved-wrong facts in memory files (off unless you say yes), and
   the day and time of the weekly run.

## Install

```bash
git clone https://github.com/Matteoikarieth96/token-checkup-skill ~/.claude/skills/token-checkup
```

Requirements:

- Claude Code (tested with 2.1.304 and 2.1.308), with auto-memory for the memory checks
- Python 3.9 or newer, standard library only
- `gh` is optional: the fact checks use it only in runs where you are present. The skill never
  runs git.
- macOS or Linux

## Usage

In Claude Code, just ask:

> set up token-checkup

> run the token checkup

> how much would compaction at 300k have saved me?

## Security

Everything runs locally. The scripts read transcripts and memory under `~/.claude` (or
`$CLAUDE_CONFIG_DIR`) and write only to `~/.claude/token-checkup/` and, on install or uninstall, to
`~/.claude/settings.json`. The scripts never touch the network. Claude and its subagents read the
files they check, as in any Claude Code session. No run uses git: for a local repo, the fact checks
only read the plain file `.git/HEAD` for the branch name. Only when you are present, they may run
one `gh repo view --json` command; scheduled runs make no gh calls. Path text with a `..` component
or under `/net/`, `/Network/`, `/Volumes/` or `/mnt/` is reported, never checked. A scheduled
`claude -p` run is allowed to read only the project's transcript folder, the data folder and the
skill folder. Reports hold numbers, file names and questions from the fact checks, never secrets.
Details and residual risks: [SECURITY.md](SECURITY.md).

## Limitations

- It parses Claude Code's transcript files, whose format is not documented and may change. The
  scripts skip records they cannot read, so a format change shows up as missing numbers.
- The system prompt and tool definitions are not in transcripts. Growth there shows only as
  first-call context by Claude Code version.
- Token counts are estimates: characters divided by 4, about 1,600 per image.
- `autoCompactWindow` only matters for models with a context window larger than the value: in
  practice the 1M-window models.
- The hook and `autoCompactWindow` are user-level settings, so they apply to every project.
- The guard bands start at 250k, so sessions on 200k-window models never reach them.
- Compaction keeps a summary, not the full early conversation.
- The weekly run depends on your scheduler. Scheduled tasks in the Claude desktop app run while the
  app is open and the computer is awake.
- The memory checks read verification dates written as "Verified" or the Italian "Verificato".
- Windows is untested.

## Uninstall

```bash
python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/token-checkup/scripts/setup.py" uninstall
rm -rf "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/token-checkup"
```

The data folder holds your reports and the backups of your memory and settings. Deleting it removes
them, so copy what you want to keep first:

```bash
cp -R "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/token-checkup" ~/token-checkup-copy
rm -rf "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/token-checkup"
```

## Tests

```bash
python3 -m unittest discover -s tests
```

CI runs them on Python 3.9 and 3.12 on every push and pull request.

## More skills

- [claude-time-is-relative](https://github.com/Matteoikarieth96/claude-time-is-relative): time estimates for Claude Code tasks, learned from your own past sessions
- [adversarial-review-skill](https://github.com/Matteoikarieth96/adversarial-review-skill): a panel of independent reviewer agents challenges finished work
- [skill-release-skill](https://github.com/Matteoikarieth96/skill-release-skill): take a skill to a public GitHub repo without leaking anything
- [ai-privacy-check-skill](https://github.com/Matteoikarieth96/ai-privacy-check-skill): privacy and AI rules check for a use case in Italy and the EU (in Italian)
- [hiring-prep-skill](https://github.com/Matteoikarieth96/hiring-prep-skill): one interview prep page from a company, a role and your resume
- [evm-dd-skill](https://github.com/Matteoikarieth96/evm-dd-skill): investor-angle due diligence on crypto and EVM projects
- [whiteboard-video-skill](https://github.com/Matteoikarieth96/whiteboard-video-skill): hand-drawn whiteboard explainer videos with voice-over
- [3d-print-design-skill](https://github.com/Matteoikarieth96/3d-print-design-skill): FDM 3D-printable parts in code, from questions to a checked STL
- [beer-can-label-skill](https://github.com/Matteoikarieth96/beer-can-label-skill): print-ready beer can labels with a 3D preview
- [breadboard-wiring-guide-skill](https://github.com/Matteoikarieth96/breadboard-wiring-guide-skill): step-by-step breadboard wiring guides for Arduino UNO

## Licence

MIT, see [LICENSE](LICENSE).
