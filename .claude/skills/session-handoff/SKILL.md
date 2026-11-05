---
name: session-handoff
description: Save the state of a long or ending session to .agents/handoffs/ so a fresh chat, or a different agent, can continue without losing context. Use when a chat passes about 40 messages, before switching agents, or when the owner says to wrap up.
---

# Session handoff

Long chats lose detail and cost more per message. A fresh chat that starts
from a good handoff is usually better than continuing. Offer one when a chat
passes about 40 of the owner's messages, when switching to another agent, or
when the owner says to stop for the day.

## Write the file

Path: `.agents/handoffs/YYYY-MM-DD-<topic>.md` (local only; the folder is in
`.gitignore` so private context never reaches the public repository).

```markdown
# <Topic> — handoff <date>

From: <agent and model>   To: <next agent, or "next session">
Branch: <branch> at <commit>   PR: <link or none>

## Goal
<one or two sentences>

## State
- Implemented: <what the code does now, with paths>
- Verified: <command → result>; Not run: <checks still to run>
- Accepted by the owner: <only what they said>

## Next step
<the single next action, concrete enough to start on>

## Decisions made this session
<owner decisions, with the owner's words where they matter>

## Open questions
<what is undecided, and who decides>

## Do not touch
<files another agent owns; running processes to leave alone>

## Read first
<paths, including skill paths>
```

## Rules

- Check every "implemented" and "verified" line against the files and a fresh
  test run before writing it. A handoff that overstates progress is worse than
  none.
- Decisions that will matter beyond this task belong in the repository's own
  decision or design documents too, not only in a handoff.
- The next session starts by reading the newest handoff on its topic, then
  re-checking the branch, before doing anything else.
- Delete or archive a handoff once its work is merged.
