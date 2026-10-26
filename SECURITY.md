# Security

## Threat model

What the skill reads, runs and writes, and what could be hostile:

- **Inputs**: Claude Code session transcripts (`~/.claude/projects/*/*.jsonl`), memory files
  (`<project>/memory/*.md`), `~/.claude/settings.json`, skill descriptions and CLAUDE.md files.
  Transcripts and memory hold text from web pages, tool output and other people, so they are
  untrusted.
- **Instructions hidden in inputs** (a memory file or transcript line that tells Claude to do
  something) are data, not commands. SKILL.md and the fact-check agent prompt say so.
- **Commands** the skill runs: its own Python scripts. Fact-check subagents run read-only commands
  (`ls`, `test -e`, `git log`, `git status`, optionally `gh repo view`) and may edit memory files.
- **Network**: none from the scripts. Subagents may call `gh repo view` with the user's own login,
  read-only.
- **Outputs**: `~/.claude/token-checkup/` (history of numbers, reports, backups of the memory
  folder and of settings, hook state, config), memory files (fact fixes and index fixes), and on
  install or uninstall `~/.claude/settings.json`.

## Mitigations

| Risk | Mitigation | File |
| --- | --- | --- |
| A prompt injection in a transcript reaches Claude through the hook | The hook prints a fixed sentence with one number; it never echoes transcript text | `scripts/context_guard.py` |
| The hook breaks or slows every prompt | Any error means no output and exit 0; it reads only the last 4 MB of the transcript; 5 s timeout in settings | `scripts/context_guard.py`, `scripts/setup.py` |
| A malformed session id writes outside the state folder | The id is reduced to letters, digits, `-` and `_` before it becomes a file name | `scripts/context_guard.py` |
| Corrupting `settings.json` | Read as JSON, timestamped backup before every write, write to a temp file then atomic replace | `scripts/setup.py` |
| Losing the user's own compaction setting | The value from before the first install is recorded and restored on uninstall | `scripts/setup.py` |
| Shell injection through the install path | Paths in the hook command are quoted with `shlex.quote` | `scripts/setup.py` |
| Memory edits go wrong | `memory_check.py --backup` copies the folder first (last four kept); agents may only fix proved-wrong facts, never rewrite, delete or move; `auto_fix_memory: false` turns fixes into proposals | `scripts/memory_check.py`, `SKILL.md`, `references/verify-agent.md` |
| Destructive or broad changes without consent | Archiving, splitting, CLAUDE.md, settings, hooks and connectors are proposals only, applied after the user's yes | `SKILL.md` |
| Secrets in transcripts or memory end up in a report | Reports carry numbers and file names; SKILL.md and the agent prompt forbid quoting secrets | `SKILL.md`, `references/verify-agent.md` |
| Reading huge transcripts exhausts memory | Files are read line by line, except the bounded hook tail and the memory-mention search, which reads one transcript at a time | `scripts/common.py`, `scripts/memory_check.py` |

Tests for these cases: `tests/test_token_checkup.py` (hook once per band, reset after compaction,
silent on bad input; install and uninstall round trip keeping other hooks; memory checks on a
synthetic folder).

## Residual risks

- Memory backups are full copies of private memory files, kept under `~/.claude/token-checkup/backups/`.
- Fact-check agents run with the user's permissions and `gh` login. Their prompt limits them to
  read-only commands and memory edits, but a model can still make a wrong edit; the backup is the
  safety net.
- Earlier compaction loses detail of early turns in long sessions.
- The transcript format is undocumented; a format change could make numbers wrong before they go
  missing. Compare with what you see in the app when a number looks odd.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for security problems.
