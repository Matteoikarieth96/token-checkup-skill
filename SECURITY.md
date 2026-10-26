# Security

## Threat model

What the skill reads, runs and writes, and what could be hostile:

- **Inputs**: Claude Code session transcripts (`~/.claude/projects/<key>/*.jsonl`), memory files
  (`~/.claude/projects/<key>/memory/*.md`), `~/.claude/settings.json`, skill descriptions and
  CLAUDE.md files. Transcripts and memory hold text from web pages, tool output and other people,
  so they are untrusted.
- **Instructions hidden in inputs** (a memory file or transcript line that tells Claude to do
  something) are data, not commands. SKILL.md and the fact-check agent prompt say so, and the agent
  prompt puts the file list in a fenced block labelled as data.
- **Commands** the skill runs: its own Python scripts. Fact-check subagents run only read-only
  commands: `ls`, `test -e`, two fixed git commands (`status` and `log`, with `core.fsmonitor`
  disabled), `grep -l` on transcripts followed by at most a few matching lines of up to 200
  characters each, and optionally `gh repo view`. They edit memory files only when
  `auto_fix_memory` is true.
- **Network**: none from the scripts. Subagents may call `gh repo view` (optional) with the user's
  own login, read-only.
- **Outputs**: `~/.claude/token-checkup/` (history of numbers, reports, backups of the memory folder
  and of settings, install record, hook state, config), memory files (index fixes, and fact fixes
  when enabled), and on install or uninstall `~/.claude/settings.json`.

## Mitigations

| Risk | Mitigation | File |
| --- | --- | --- |
| A prompt injection in a transcript reaches Claude through the hook | The hook prints a fixed sentence with one number; it never echoes transcript text | `scripts/context_guard.py` |
| The hook breaks or slows every prompt | Any error means no output and exit 0; it reads only the last 4 MB of the transcript; 5 s timeout in settings | `scripts/context_guard.py`, `scripts/setup.py` |
| A malformed session id writes outside the state folder | The id is reduced to letters, digits, `-` and `_` before it becomes a file name | `scripts/context_guard.py` |
| Hook state piles up | State files older than 30 days are deleted when the hook saves state (best effort) | `scripts/context_guard.py` |
| A repository's own git config runs commands during a fact check | Only two fixed git commands, with `-c core.fsmonitor=false` (and `core.hooksPath=/dev/null` and `--no-optional-locks` for `status`), paths in single quotes; no other git subcommands | `references/verify-agent.md` |
| Reading or writing the wrong project | `--project` maps a path to `projects/<key>`, or takes a bare key only if it is a folder directly under `projects/`; the argument itself is never used as the transcript folder; there is no "most recent project" fallback; every script prints the key it resolved | `scripts/common.py` |
| Symlinks pull in files from elsewhere | Transcripts and memory files count only as regular files (`lstat`); memory backups copy only regular `*.md` files and never follow symlinks | `scripts/common.py`, `scripts/memory_check.py` |
| An odd transcript record crashes or skews a run | Lines that are not JSON objects are skipped; token numbers count only as finite non-negative numbers; each line is handled on its own; config keys with the wrong type are ignored; bad dates give a clear error | `scripts/common.py`, `scripts/measure.py` |
| Corrupting `settings.json` | Invalid JSON or an unexpected shape stops install and uninstall before any write; the new file goes to a temp file in the same folder, keeps the original mode (0600 for a new file) and replaces the real file, so a symlinked `settings.json` keeps its link | `scripts/setup.py` |
| Removing someone else's hook | Only hook entries tagged `# token-checkup-guard` are removed; untagged hooks that look like a context guard are listed and left alone unless `--replace-legacy` is given; other groups are not touched; uninstall with nothing installed writes nothing | `scripts/setup.py` |
| Losing earlier settings | A backup with a unique name (mode 0600) before every write; the very first backup is kept as `settings-first.json`, plus the last five | `scripts/setup.py` |
| Losing the user's own compaction setting | The value from before the first install is recorded with the installed value and time; uninstall restores it only if the current value is still the installed one and the recorded value is valid | `scripts/setup.py` |
| A hook that silently stops working | `setup.py check` reports BROKEN when the hook's interpreter or script is missing; install warns when it runs from a virtualenv | `scripts/setup.py` |
| Shell injection through the install path | Paths in the hook command are quoted with `shlex.quote` | `scripts/setup.py` |
| Slow regexes on hostile memory text | Bounded patterns without nested repetition; length limits on links (200) and paths (300) | `scripts/memory_check.py` |
| Hostile path text in a memory | Path text with `$`, backticks, `;`, `&`, `\|`, `(`, `)`, `<`, `>` or control characters is reported as unusual and never checked; printed memory text is stripped of control characters and cut to 120 characters | `scripts/memory_check.py` |
| Index links that point outside the memory folder | Links are resolved inside the memory folder; links outside it are reported and never dropped | `scripts/memory_check.py` |
| A question for the user passes as a verified fact | A line starting with `**Needs user` marks the memory "waiting for the user", never verified; agents write that line instead of a Verified line | `scripts/memory_check.py`, `references/verify-agent.md` |
| Memory edits without consent | `auto_fix_memory` is false by default, so fact fixes are proposals unless the user said yes at setup; the only automatic index fix drops a line whose file does not exist | `scripts/common.py`, `SKILL.md` |
| Memory edits go wrong | `memory_check.py --backup` copies the folder first (last four kept, only folders with the backup name pattern are rotated); agents may only fix facts their own checks proved wrong, never rewrite, delete or move; claims in transcripts are not evidence | `scripts/memory_check.py`, `references/verify-agent.md` |
| Destructive or broad changes without consent | Archiving, splitting, CLAUDE.md, settings, hooks and connectors are proposals only, applied after the user's yes; scheduled `claude -p` runs never use `--dangerously-skip-permissions` | `SKILL.md` |
| Secrets in transcripts or memory end up in a report | Reports hold numbers, file names and questions from the fact checks, never secrets: SKILL.md, the report template and the agent prompt forbid quoting them | `SKILL.md`, `references/report.md`, `references/verify-agent.md` |
| Reading huge transcripts exhausts memory | Files are read line by line, except the bounded hook tail and the memory-mention search, which reads one transcript at a time | `scripts/common.py`, `scripts/memory_check.py` |

Tests for these cases are in `tests/test_token_checkup.py`.

## Residual risks

- Memory backups are full copies of private memory files, kept under `~/.claude/token-checkup/backups/`.
- Settings backups are full copies of `~/.claude/settings.json`, which can hold env values or tokens.
  They are kept under `~/.claude/token-checkup/backups/` with mode 0600: the first one ever made and
  the last five.
- Reports may quote questions from the fact checks, written by a model from memory text. They never
  hold secrets on purpose, but a model can make a mistake; read a report before sharing it.
- Fact-check agents run with the user's permissions and `gh` login. Their prompt limits them to
  read-only commands and memory edits, but a model can still make a wrong edit; the backup is the
  safety net.
- The hook and `autoCompactWindow` are user-level settings and apply to every project.
- Earlier compaction loses detail of early turns in long sessions.
- The transcript format is undocumented; a format change could make numbers wrong before they go
  missing. Compare with what you see in the app when a number looks odd.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for security problems.
