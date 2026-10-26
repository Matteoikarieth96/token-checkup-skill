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
  commands: `echo "$HOME"`, `ls`, `test -e`, and `grep -l` on transcripts followed by at most a few
  matching lines of up to 200 characters each. For a local repo they only check that the folder
  exists and read the plain file `<dir>/.git/HEAD` for the branch name (at most 200 characters,
  treated as data). In runs where the user is present, and only then, they may also run
  `gh repo view OWNER/NAME --json visibility,isArchived,pushedAt`. No run uses git. Scheduled
  (unattended) runs run no gh at all. Agents edit memory files only when `auto_fix_memory` is true
  and the backup of that run worked.
- **Why git is not used.** Every git command, even one that only reads, loads the `.git/config` of
  the repository it opens, and that config can make git run a program: through fsmonitor, filter
  drivers, signature verification (`gpg.program`), or a partial clone's lazy fetch, which can run
  `core.sshCommand` or `remote.*.uploadpack`. A memory can name any folder, so no git command is
  safe to run on it. Reading `.git/HEAD` as a plain file runs nothing. The last commit and
  uncommitted work are therefore always reported as "not checked".
- **Network**: none from the scripts. In runs where the user is present, subagents may call
  `gh repo view OWNER/NAME --json visibility,isArchived,pushedAt` with the user's own login,
  read-only. Scheduled runs make no gh calls. `memory_check.py` never checks a path under `/net/`,
  `/Network/`, `/Volumes/` or `/mnt/`, or one with a `..` component, so a memory cannot make it
  trigger an automount or a network lookup.
- **Outputs**: `~/.claude/token-checkup/` (history of numbers, reports, backups of the memory folder
  per project and of settings, install record, hook state, config), memory files (fact fixes when
  enabled, and only the changes the user approves, index changes included), and on install or
  uninstall `~/.claude/settings.json`.

## Mitigations

| Risk | Mitigation | File |
| --- | --- | --- |
| A prompt injection in a transcript reaches Claude through the hook | The hook prints a fixed sentence with one number; it never echoes transcript text | `scripts/context_guard.py` |
| The hook breaks or slows every prompt | Any error means no output and exit 0; it reads only the last 4 MB of the transcript; 5 s timeout in settings | `scripts/context_guard.py`, `scripts/setup.py` |
| A malformed session id writes outside the state folder | The id is reduced to letters, digits, `-` and `_` before it becomes a file name | `scripts/context_guard.py` |
| Hook state piles up | State files older than 30 days are deleted when the hook saves state (best effort) | `scripts/context_guard.py` |
| A repository's own config (`.git/config`) runs commands during a fact check (through fsmonitor, filter drivers, signature verification, or a partial clone's lazy fetch through `core.sshCommand` or `remote.*.uploadpack`) | No run uses git at all. For a local repo, agents only check that the folder exists and read the plain file `<dir>/.git/HEAD` for the branch name (at most 200 characters, as data). The last commit, uncommitted work and anything else about the repo are reported as "not checked" | `references/verify-agent.md`, `SKILL.md` |
| A third-party README reaches the agent through gh | The only gh command is `gh repo view OWNER/NAME --json visibility,isArchived,pushedAt` (the plain form prints the README), only in runs where the user is present and only when OWNER and NAME use letters, digits, `.`, `-` and `_`. Scheduled runs make no gh calls | `references/verify-agent.md`, `SKILL.md` |
| A `~` path is checked as written, so the shell tests a folder literally named `~` | `memory_check.py` expands `~` itself and reports dead paths both as written and expanded (`--json`); the text output shows the expanded path. Agents replace a leading `~` with the absolute home folder before quoting a path and never single-quote a path that still starts with `~` | `scripts/memory_check.py`, `references/verify-agent.md` |
| Reading or writing the wrong project | `--project` maps a path to `projects/<key>`, or takes a bare key only if it is a folder directly under `projects/`; the argument itself is never used as the transcript folder; there is no "most recent project" fallback; every script prints the key it resolved | `scripts/common.py` |
| Symlinks pull in files from elsewhere | Transcripts and memory files count only as regular files (`lstat`); memory backups copy only regular `*.md` files and never follow symlinks | `scripts/common.py`, `scripts/memory_check.py` |
| An odd transcript record crashes or skews a run | Lines that are not JSON objects are skipped; token numbers count only as finite non-negative numbers; each line is handled on its own; config keys with the wrong type are ignored; bad dates give a clear error | `scripts/common.py`, `scripts/measure.py` |
| Corrupting `settings.json` | Invalid JSON or an unexpected shape stops install and uninstall before any write; the new file goes to a temp file in the same folder, keeps the original mode (0600 for a new file) and replaces the real file, so a symlinked `settings.json` keeps its link | `scripts/setup.py` |
| Removing someone else's hook | Only hook entries tagged `# token-checkup-guard` are removed; untagged hooks that look like a context guard are listed and left alone unless `--replace-legacy` is given; other groups are not touched; uninstall with nothing installed writes nothing | `scripts/setup.py` |
| Losing earlier settings | A backup with a unique name (mode 0600) before every write; the very first backup is kept as `settings-first.json`, plus the last five | `scripts/setup.py` |
| Losing the user's own compaction setting | The value from before the first install is recorded with the installed value and time. If the user changed the value after an install, the next install records the user's value as the one to restore and prints a note. Uninstall restores the recorded value only if the current value is still the installed one and the recorded value is valid | `scripts/setup.py` |
| A hook that silently stops working | `setup.py check` reports BROKEN when the hook's interpreter or script is missing; install warns when it runs from a virtualenv | `scripts/setup.py` |
| Shell injection through the install path | Paths in the hook command are quoted with `shlex.quote` | `scripts/setup.py` |
| Slow regexes on hostile memory text | Every pattern is bounded and has no nested repetition: index links `\]\(([^)\s]{1,200}\.md)\)`, `[[links]]` up to 200 characters, paths up to 300, a plain index line up to about 2,500; a 40 KB index line of repeated `](` and 40 KB plain-line tails are checked in well under 0.5 s (tested) | `scripts/memory_check.py` |
| Hostile path text in a memory | Path text with `$`, backticks, `;`, `&`, `\|`, `(`, `)`, `<`, `>` or control characters is reported as unusual and never checked; printed memory text is stripped of control characters and cut to 120 characters | `scripts/memory_check.py` |
| A memory path triggers an automount or a network lookup | Path text with a `..` component once `~` is expanded, or whose normalized path is under `/net/`, `/Network/`, `/Volumes/` or `/mnt/` (any letter case), is reported as unusual and never checked (tested); agents follow the same rule | `scripts/memory_check.py`, `references/verify-agent.md` |
| An index link is called broken by mistake, also when written with %xx | A link works when its raw text or its %xx-decoded text names an existing file inside the memory folder (a file literally named `a%20b.md` counts, tested). Links outside the memory folder are reported and never called broken | `scripts/memory_check.py` |
| An index entry is changed without consent | No index change is automatic: dropping a line, removing a broken link from a mixed line, adding a line for an orphan and shortening a line are all proposals. A line is a drop candidate only when it is a plain `- [title](file.md) text` line with that one broken link and no other link, `[[link]]`, URL or backtick; any other line with a broken link is "mixed" (tested) | `scripts/memory_check.py`, `SKILL.md`, `references/report.md` |
| A question for the user passes as a verified fact | A line starting with `**Needs user` marks the memory "waiting for the user", never verified; agents write that line instead of a Verified line | `scripts/memory_check.py`, `references/verify-agent.md` |
| Memory edits without consent | `auto_fix_memory` is false by default, so fact fixes are proposals unless the user said yes at setup; the skill makes no index change on its own | `scripts/common.py`, `SKILL.md` |
| Memory edits go wrong | `memory_check.py --backup` first copies the folder to `backups/memory/<project key>/<timestamp>`. Rotation keeps the last four of that project: the new copy plus the newest older ones by modification time. It never deletes the new copy, even next to folders with future-dated names (tested), and deletes only timestamp-named folders inside that project's folder, so other projects' backups and the flat `backups/memory-*` folders of earlier versions are never touched. If `backups`, `backups/memory` or the project's backup folder is a symlink, the backup is refused and nothing is created (tested). If the new copy is gone after rotation, the backup is reported as failed (tested). After `Backup FAILED`, no memory file is edited in that run. Agents may only fix facts their own checks proved wrong, never rewrite, delete or move; claims in transcripts are not evidence | `scripts/memory_check.py`, `SKILL.md`, `references/verify-agent.md` |
| Destructive or broad changes without consent | Archiving, splitting, index changes, CLAUDE.md, settings, hooks and connectors are proposals only, applied after the user's yes. Scheduled `claude -p` runs allow only the three scripts by absolute path, Read in the project's transcript folder, the data folder and the skill folder, and edits in the memory and report folders, and nothing else: never a bare `Read`, never generic `Bash(python3:*)`, `Bash(git:*)` or `Bash(gh:*)`, never `--dangerously-skip-permissions` | `SKILL.md` |
| Secrets in transcripts or memory end up in a report | Reports hold numbers, file names and questions from the fact checks, never secrets: SKILL.md, the report template and the agent prompt forbid quoting them | `SKILL.md`, `references/report.md`, `references/verify-agent.md` |
| Reading huge transcripts exhausts memory | Files are read line by line, except the bounded hook tail and the memory-mention search, which reads one transcript at a time | `scripts/common.py`, `scripts/memory_check.py` |

Tests for these cases are in `tests/test_token_checkup.py`.

## Residual risks

- Memory backups are full copies of private memory files, kept under
  `~/.claude/token-checkup/backups/memory/<project key>/` (the last four per project). Flat
  `backups/memory-*` folders from earlier versions are never rotated; delete them by hand when you
  no longer need them.
- Settings backups are full copies of `~/.claude/settings.json`, which can hold env values or tokens.
  They are kept under `~/.claude/token-checkup/backups/` with mode 0600: the first one ever made and
  the last five.
- Reports may quote questions from the fact checks, written by a model from memory text. They never
  hold secrets on purpose, but a model can make a mistake; read a report before sharing it.
- Fact-check agents run with the user's permissions and, when the user is present, the user's `gh`
  login. Their prompt limits them to read-only commands and memory edits, but a model can still make
  a wrong edit; the backup is the safety net.
- `memory_check.py` checks whether a quoted path exists, and that check follows symlinks. A path
  that passes the path rules but goes through a symlink to a network mount can still reach that
  mount.
- The content of `<dir>/.git/HEAD` was written by whoever made the repository. Agents read at most
  200 characters of it and treat it as data, but a model can still be misled by text.
- The hook and `autoCompactWindow` are user-level settings and apply to every project.
- Earlier compaction loses detail of early turns in long sessions.
- The transcript format is undocumented; a format change could make numbers wrong before they go
  missing. Compare with what you see in the app when a number looks odd.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for security problems.
