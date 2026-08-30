# Agent SOUL files

One `SOUL.md` per OpenClaw agent. Each agent loads its own file from its
workspace at `~/.openclaw/workspace/<agent-id>/SOUL.md`, so the file here is
the source and `deploy.py` copies it into place.

## What belongs here, and what does not

These files shape **craft**: voice, rhythm, what good output feels like, the
judgement calls a prompt cannot spell out.

They are **not** where hard constraints live. Question counts, word bands,
answer-key placement, paragraph minimums and the banned-pattern gates are
enforced in the Python prompt builders and validators
(`trivia/engine.py`, `stories/engine.py`, `puzzle/engine.py`,
`openclaw_docx_writer.py`). Those run on every build and reject or repair
output; a SOUL file cannot.

Stating a rule in both places is fine and often useful, but the Python is
authoritative. If the two ever disagree, fix the Python first.

## Files

| File | Agent | Writes |
|------|-------|--------|
| `writer.md` | `writer-agent-1`, `long-writer-agent-1` | Long-form prose from a heading outline |
| `trivia.md` | `trivia-agent-1` | Multiple-choice questions and Did You Know facts |
| `stories.md` | `stories-agent-1` | Researched non-fiction short stories |
| `puzzle.md` | `puzzle-agent-1` | Riddles, cryptogram phrases, word lists, clues |

## Deploying

    python agent_souls/deploy.py --dry-run    # show what would change
    python agent_souls/deploy.py              # write, backing up what is there
    python agent_souls/deploy.py --restore    # put the backups back

Nothing is written until you run it without `--dry-run`. Every existing
`SOUL.md` is backed up to `SOUL.md.bak-<timestamp>` first.

The production machine is separate: run the deploy there too, or copy the
files across by hand.
