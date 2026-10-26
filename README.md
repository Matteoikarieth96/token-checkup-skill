# token-checkup

A Claude Code skill that keeps your sessions from quietly getting expensive. It measures token use
from your own local transcripts, keeps what every session loads at the start lean, keeps Claude's
memory true, and can install two guard rails: earlier auto-compaction and a context-size warning.

> **Independent project.** Not affiliated with or endorsed by Anthropic.

## Why

Every tool call re-reads the whole session. On the machine this was built on, over 14 days in
October 2026:

- a session started at about 68k tokens before any work, yet the average model call re-read about
  300k, because 39 sessions grew past 400k and only 4 ever compacted (a 1M window rarely fills);
- the start itself grew from about 72k to 84k on the day of an app update, and a few thousand more
  with new connectors, skills and hook text, without anyone noticing.

Replaying those two weeks with compaction at 300k (`measure.py --simulate 300000`) cut the context
re-read by about 43%. That is an estimate, and your numbers will differ: the skill measures them.

## What it does

- Asks first: setup, a checkup now, or numbers only; which project; for setup, the compaction window
  (with an estimate on your own data), the context guard, whether weekly runs may fix facts in
  memory, and when the weekly run happens.
- Measures with stdlib-only Python scripts:

| Script | What it reports or does |
| --- | --- |
| `scripts/measure.py` | First-call context (also by Claude Code version), peak size, compactions, mean context per call, cache reads, tools that fill sessions. Week over week and against a saved baseline. `--simulate N` estimates what compaction at N tokens would save. |
| `scripts/audit_start.py` | What you control at session start (CLAUDE.md files, memory index, skill descriptions, hooks, compaction settings) and what grew in what transcripts record before the first answer. |
| `scripts/memory_check.py` | Memory index problems, missing paths, dead links, memories due for a fact check, archive and split candidates. Optional backup. |
| `scripts/context_guard.py` | `UserPromptSubmit` hook: one line from Claude the first time a session passes 250k, 400k, 550k, 700k and 850k tokens. |
| `scripts/setup.py` | Installs or removes the hook and `"autoCompactWindow"`, backing up settings and restoring your previous value on uninstall. |

- A weekly run uses the scripts first and sends at most two small subagents to fact-check only the
  memories that changed or are due, so the checkup costs far less than it saves.
- Hands over a one-page report with week-over-week numbers, what was fixed, and numbered proposals.

What it never does: upload anything; archive, move or delete memories, edit a CLAUDE.md, or change
settings, hooks or connectors without your yes; block a prompt.

## The intake questions

1. Mode: setup, a checkup now, or numbers only.
2. Project: the folder you work in (default: the current one).
3. For setup: the compaction window (300000 recommended), whether to install the context guard,
   whether weekly runs may correct proved-wrong facts in memory files, and the day and time of the
   weekly run.

## Install

```bash
git clone https://github.com/Matteoikarieth96/token-checkup-skill ~/.claude/skills/token-checkup
```

Requirements:

- Claude Code (tested with 2.1.308)
- Python 3.9 or newer, standard library only
- macOS or Linux

## Usage

In Claude Code, just ask:

> set up token-checkup

> run the token checkup

> how much would compaction at 300k have saved me?

## Security

Everything runs locally. The scripts read transcripts and memory under `~/.claude` (or
`$CLAUDE_CONFIG_DIR`), write only to `~/.claude/token-checkup/` and, on install, to
`~/.claude/settings.json`, and never touch the network. Reports hold numbers and file names, not
transcript text. Details and residual risks: [SECURITY.md](SECURITY.md).

## Limitations

- It parses Claude Code's transcript files, whose format is not documented and may change. The
  scripts skip what they cannot read, so a format change shows up as missing numbers.
- The system prompt and tool definitions are not in transcripts. Growth there shows only as
  first-call context by Claude Code version.
- Token counts are estimates: characters divided by 4, about 1,600 per image.
- Compaction keeps a summary, not the full early conversation.
- The weekly run depends on your scheduler. Scheduled tasks in the Claude desktop app run while the
  app is open, or at the next launch.
- Windows is untested.

## Uninstall

```bash
python3 ~/.claude/skills/token-checkup/scripts/setup.py uninstall
rm -rf ~/.claude/skills/token-checkup ~/.claude/token-checkup
```

## Tests

```bash
python3 -m unittest discover -s tests
```

CI runs them on every push and pull request.

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
