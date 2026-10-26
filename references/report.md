# Report template

Save as `~/.claude/token-checkup/reports/<YYYY-MM-DD>.md`. Keep it under a page. Write in the
user's language. Mark every number as measured or estimated.

```markdown
# Token checkup, <date> (<project>)

<One sentence: the most important change this week and whether things got better or worse.>

## Numbers (last 7 days vs the 7 before; vs baseline <date>)
| | last week | this week | vs baseline |
|---|---|---|---|
| Sessions | | | |
| First-call context (median) | | | |
| Mean context per model call | | | |
| Sessions over the compact window, not compacted | | | |
| Compactions (fired at about) | | | |
| Cache reads per session (median) | | | |

## What grew at session start
<Only real growth, with its likely cause: app version, connectors, skills, hooks, CLAUDE.md, memory.>

## Done automatically
- Memory backup: <path>
- Fact checks: <n> memories, <n> corrected (<file: what changed>, ...)
- Index fixes: <...>

## Proposals (nothing applied yet)
1. <exact change>: <why, rough saving>
2. ...

## Questions only the user can answer
- <from the fact checks>

## Not checked
- <anything skipped and why>
```
