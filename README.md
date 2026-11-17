# swarzy

Answers an Archon coding-loop run's pauses while the owner is away, following the
[archon-monitor skill](../archon_monitor_skill/archon-monitor/SKILL.md), which is its spec.

```bash
uv run swarzy <project folder> --log ~/swarzy-night.md            # watch and answer; stop with Ctrl-C
uv run swarzy <project folder> --log night.md --once --dry-run    # decide the current pause, send nothing
```

Each pause:

1. **Read**: the skill's `pending.py` gives the question and the rule labels the loop accepts.
2. **Decide**: Claude Code runs headless in the project with the skill as its instructions, the
   question, this phase's earlier answers and the owner's standing approvals. It may change project
   files and run the project's checks when the fix is one no answer or plan change can make (a
   version pin, a contract, a check failing since phase start); the loop keeps what changes during
   a pause as the owner's, and every changed file is listed in the log. `--dry-run`
   gives it read-only tools. It returns a structured decision
   (`answer` with reply text, files to grant and a log note, or `escalate` with a reason). Nothing
   is parsed out of free text.
3. **Check**: `amend.py` saves any plan amendment, then `precheck.py` runs the loop's own answer
   handling on a scratch copy. A refusal goes back to Claude with the loop's reason, up to
   `--max-tries` (3).
4. **Send and log**: the loop's own `tools/answer.py` sends it (`--amended` with an amendment),
   and `log.py` appends the decision to `--log`. REVIEW marks only an entry the owner must act on:
   every escalation, a failed or cancelled run, and an owner decision Claude recorded but could not
   confirm (the note names the action). Changed files and questions left for later are in the note,
   without REVIEW.

Always left for the owner, logged with REVIEW and passed to `SWARZY_NOTIFY_CMD` when set:
the final phase approval, a decision Claude escalates, an amendment that cannot be saved, a
reply still refused after the last try, and a pre-check that itself failed (asked of Claude once). An escalated pause is waited out until the owner answers.

A run that fails or is cancelled with nothing waiting is logged with REVIEW; a run that finished
is logged without it. Both are passed to `SWARZY_NOTIFY_CMD`; swarzy then waits until a run of the
project starts again and carries on.

| Variable | Default |
| --- | --- |
| `SWARZY_SKILL_DIR` | `~/dev/archon_monitor_skill/archon-monitor` |
| `ARCHON_HOME` | `~/.archon` |
| `SWARZY_CLAUDE_CMD` | `claude -p --output-format json --model opus --permission-mode default` |
| `SWARZY_APPROVALS` | `~/.config/swarzy/approvals.json`: `{"approvals": [{"project": "<folder name> or *", "given": "<date>", "approval": "<what is OK>"}]}`, written by the owner only |
| `SWARZY_NOTIFY_CMD` | none (for example a script that sends a phone notification) |


Licence: MIT (see `LICENSE`).
