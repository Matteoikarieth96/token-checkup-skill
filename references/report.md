# Report template

Save as `~/.claude/token-checkup/reports/<YYYY-MM-DD>.md`. Keep it under a page. Write in the
user's language. Mark every number as measured or estimated. Never quote a secret.

```markdown
# Token checkup, <date> (<project folder>, key <key>)

<One sentence: the most important change this week and whether things got better or worse.>

## Numbers (last 7 days vs the 7 before; vs baseline <date>)
| | last week | this week | vs baseline |
|---|---|---|---|
| Sessions | | | - |
| First-call context (median) | | | |
| Mean context per model call | | | |
| Sessions that should have compacted but did not (since setup) | | | - |
| Compactions (fired at about) | | | - |
| Cache reads per session (median) | | | |

"vs baseline" only for medians and per-call values: the windows can differ in length.

## What grew at session start
<Only real growth, with its likely cause: app version, connectors, skills, hooks, CLAUDE.md, memory.>

## Done automatically
- Memory backup: <path>, or "FAILED: <reason>; no memory file was edited in this run"
- Fact checks: <n> memories; <n> corrected (<file: what changed>, ...) when auto_fix_memory is true
  and the backup worked, otherwise listed as proposals below
- Index: nothing changed. Every index change is a proposal below.

## Proposals (nothing applied yet)
1. <exact change>: <why, rough saving>
2. ...
<Index changes are numbered proposals here too: drop candidate lines (one plain link, broken), the
broken link to remove from each mixed line, lines for orphan files, shorter versions of long lines.>

## Questions only the user can answer
- <from the fact checks and the memories waiting for the user>

## Not checked
- <anything skipped and why: no memory folder, unusual path text, the last commit and uncommitted
  work of local repos (never checked), GitHub claims in an unattended run, ...>
```
