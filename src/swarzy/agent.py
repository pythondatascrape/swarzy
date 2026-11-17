"""swarzy: answers an Archon coding-loop run's pauses unattended, following the archon-monitor skill.

Code does the mechanical steps through the skill's own scripts (watch, pending, precheck, amend,
log) and the loop's answer tool; Claude Code, run headless in the project, makes the judgement and
returns a structured decision: answer (reply text, files to grant, log note) or escalate to the
owner. Claude may change project files and run checks while it decides (the loop keeps what changes
during a pause as the owner's); every changed file is logged for the owner's review, and the owner's
standing approvals (approvals.json) are part of its instructions. A refused reply goes back to Claude with the loop's reason, up to max_tries; then, and
for every final approval, the owner decides.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reply", "amend", "log_title", "log_note", "review", "escalate_reason"],
    "properties": {
        "action": {"enum": ["answer", "escalate"]},
        "reply": {"type": "string"},
        "amend": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["task", "files"],
            "properties": {"task": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
                           "expect_failure": {"type": "string"},
                           "tests": {"type": "array", "items": {"type": "string"}},
                           "commands": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["old", "new"],
                               "properties": {"old": {"type": "string"}, "new": {"type": "string"}}}},
                           "drop_commands": {"type": "array", "items": {"type": "string"}}}}},
        "log_title": {"type": "string"},
        "log_note": {"type": "string"},
        "review": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
    },
}
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "task", "why", "checks", "open_p4", "pending_evidence", "note"],
    "properties": {
        "verdict": {"enum": ["approve", "reject"]},
        "task": {"type": "string"},
        "why": {"type": "string"},
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["check", "result"],
            "properties": {"check": {"type": "string"}, "result": {"type": "string"}}}},
        "open_p4": {"type": "array", "items": {"type": "string"}},
        "pending_evidence": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
}
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git grep:*)"]
CHECK_TOOLS = [  # never the loop's answer.py, archon, git writes, npm install/ci or sudo
    "Edit", "Write", "Bash(git status:*)",
    "Bash(make:*)", "Bash(go test:*)", "Bash(go vet:*)", "Bash(go build:*)", "Bash(go version:*)",
    "Bash(gofmt:*)", "Bash(shasum:*)", "Bash(uv run:*)", "Bash(python3 -m pytest:*)",
    "Bash(npm run:*)", "Bash(npm test:*)", "Bash(npx vitest:*)", "Bash(npx playwright:*)", "Bash(npx tsc:*)",
    "Bash(node --version)",
]
HISTORY_LIMIT = 60_000  # characters of earlier answers shown to Claude; each answer is shown whole


@dataclass
class Config:
    project: Path
    log: Path
    skill_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "SWARZY_SKILL_DIR", Path.home() / "dev" / "archon_monitor_skill" / "archon-monitor")))
    archon_home: Path = field(default_factory=lambda: Path(os.environ.get("ARCHON_HOME", Path.home() / ".archon")))
    claude_cmd: list[str] = field(default_factory=lambda: shlex.split(os.environ.get(
        "SWARZY_CLAUDE_CMD", "claude -p --output-format json --model opus --permission-mode default")))
    approvals: Path = field(default_factory=lambda: Path(os.environ.get(
        "SWARZY_APPROVALS", Path.home() / ".config" / "swarzy" / "approvals.json")))
    notify_cmd: list[str] = field(default_factory=lambda: shlex.split(os.environ.get("SWARZY_NOTIFY_CMD", "")))
    max_tries: int = 3
    dry_run: bool = False
    interval: float = 30
    settle: float = 60

    def env(self) -> dict:
        return {**os.environ, "ARCHON_HOME": str(self.archon_home)}

    def status_tool(self) -> Path:
        """The loop's read-only status tool (status.py), under this ARCHON_HOME."""
        return self.archon_home / "workflows" / "tasks" / "coding-loop" / "tools" / "status.py"

    def tools(self) -> str:
        """The tools Claude may use: read-only ones and the status tool with --dry-run, the checks too otherwise."""
        status = [f"Bash(python3 {self.status_tool()}:*)", f"Bash({self.status_tool()}:*)"]
        return ",".join(READ_ONLY_TOOLS + status + ([] if self.dry_run else CHECK_TOOLS))


# ---- the skill's scripts ------------------------------------------------------------------------

def script(cfg: Config, name: str, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(cfg.skill_dir / "scripts" / name), *args],
                          cwd=cfg.project, capture_output=True, text=True, env=cfg.env(), timeout=timeout)


def pending(cfg: Config) -> dict | None:
    """The open question or approval as pending.py --json gives it; None when nothing waits."""
    r = script(cfg, "pending.py", str(cfg.project), "--json")
    return json.loads(r.stdout) if r.returncode == 0 else None


def log(cfg: Config, task: str | None, title: str, note: str, review: bool) -> None:
    script(cfg, "log.py", "--log", str(cfg.log), "--task", task or "-", "--title", title, "--note", note,
           *(["--review"] if review else []))


def amend(cfg: Config, phase: str, changes: list[dict]) -> bool:
    ok = True
    for change in changes:
        args = ["--phase", phase, "--task", change["task"]]
        for f in change["files"]:
            args += ["--file", f]
        if change.get("expect_failure"):
            args += ["--expect-failure", change["expect_failure"]]
            for name in change.get("tests", []):
                args += ["--test", name]
        for c in change.get("commands", []):
            args += ["--command", c["old"], c["new"]]
        for old in change.get("drop_commands", []):
            args += ["--drop-command", old]
        ok &= script(cfg, "amend.py", str(cfg.project), *args).returncode == 0
    return ok


def precheck(cfg: Config, reply: Path) -> tuple[str, str]:
    """('accepted', its line), ('refused', the loop's reason) or ('broken', the last line of what went wrong).
    precheck.py prints REFUSED: and exits 1 for a refusal; a crash in the loop's answer handling also exits 1
    but with a traceback and no REFUSED: line, and exit 2 means it could not load the controller."""
    r = script(cfg, "precheck.py", str(cfg.project), "--reply", str(reply))
    out, err = r.stdout.strip(), r.stderr.strip()
    if r.returncode == 0:
        return "accepted", out
    if r.returncode == 1 and "Traceback" not in err and any(
            ln.startswith("REFUSED:") for ln in out.splitlines()):
        return "refused", out
    lines = (err or out).splitlines()
    return "broken", lines[-1].strip() if lines else f"precheck.py exited {r.returncode} with no output"


def send(cfg: Config, phase: str, reply: Path, amended: bool) -> tuple[bool, str]:
    tool = cfg.archon_home / "workflows" / "tasks" / "coding-loop" / "tools" / "answer.py"
    args = [sys.executable, "-B", str(tool), str(cfg.project), "--phase", phase, "--file", str(reply)]
    r = subprocess.run(args + (["--amended"] if amended else []), cwd=cfg.project, capture_output=True,
                       text=True, env=cfg.env())
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ---- the judgement --------------------------------------------------------------------------------

def state_file(cfg: Config, phase: str) -> Path:
    return (cfg.archon_home / "workspaces" / "_folder" / cfg.project.resolve().name / "state" / "coding-loop"
            / f"phase-{phase}" / "state.json")


def plan_tasks(cfg: Config, phase: str) -> dict[str, dict] | None:
    """The phase's tasks by id, or None when the plan cannot be read."""
    tasks = {}
    try:
        for p in (cfg.project / "docs" / "subtask" / f"phase-{phase}").glob("*.json"):
            for t in json.loads(p.read_text(encoding="utf-8")).get("tasks", []):
                tasks[t["task_id"]] = t
    except (OSError, ValueError, KeyError, AttributeError):
        return None
    return tasks


def earlier_answers(cfg: Config, phase: str, task: str | None = None) -> tuple[list[dict], int]:
    """This phase's answers so far that bear on the task (newest last), each whole, so a new answer stays
    consistent with them: answers on the task and the tasks it depends on, and answers tied to no plan task.
    Phase 12: all answers were 62% of the prompt, most about unrelated pages; tasks sharing a file are left
    out because most phase 12 tasks share a few common files. Every answer is a candidate when the plan or
    the task cannot be read. Over HISTORY_LIMIT characters, the oldest answers not on the task or its
    dependencies are left out first. Returns the answers shown and how many were left out."""
    try:
        items = json.loads(state_file(cfg, phase).read_text(encoding="utf-8")).get("answer_items", [])
    except (OSError, ValueError):
        return [], 0
    tasks = plan_tasks(cfg, phase)
    related = {task, *tasks[task].get("depends_on", [])} if tasks and task in tasks else {task}
    if tasks and task in tasks:
        items = [i for i in items if i.get("task") in related or i.get("task") not in tasks]
    shown = [{"id": i.get("id"), "task": i.get("task"), "status": i.get("status"), "answer": str(i.get("answer", ""))}
             for i in items]
    size = sum(len(json.dumps(a)) for a in shown)
    left_out = 0
    for on_task in (False, True):  # unrelated answers go first, oldest first
        for a in list(shown):
            if size <= HISTORY_LIMIT:
                break
            if (a["task"] in related) == on_task:
                shown.remove(a)
                size -= len(json.dumps(a))
                left_out += 1
    return shown, left_out


GATE_DECISION = re.compile(r"^(human rejected\b|explicit human approval recorded|human approved\b)")


def owner_decisions(cfg: Config, phase: str) -> list[dict]:
    """The owner's decisions at the approval gate, in full: the history transitions that record a human approval
    or rejection, and the `approval` record (its text is the latest decision whole; history reasons may be cut)."""
    try:
        s = json.loads(state_file(cfg, phase).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = [{"recorded": "history", "to": h.get("to"), "ts": h.get("ts"), "decision": h.get("reason")}
           for h in s.get("history") or [] if isinstance(h, dict) and GATE_DECISION.match(str(h.get("reason") or ""))]
    a = s.get("approval")
    if isinstance(a, dict) and (a.get("decision") or a.get("text")):
        out.append({"recorded": "approval", "decision": a.get("decision"), "text": a.get("text"), "tree": a.get("tree")})
    return out


def approvals(cfg: Config) -> list[dict]:
    """The owner's standing approvals for this project ("project": its folder name, or "*" for all)."""
    try:
        items = json.loads(cfg.approvals.read_text(encoding="utf-8")).get("approvals", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [a for a in items if a.get("project") in ("*", cfg.project.resolve().name)]


def tree(project: Path) -> dict[str, str]:
    """Content hash of every changed or untracked file (all files outside git), to see what Claude changed."""
    r = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "-z"], cwd=project, capture_output=True, text=True)
    if r.returncode == 0:
        paths = [e[3:] for e in r.stdout.split("\0") if len(e) > 3]
    else:
        paths = [str(p.relative_to(project)) for p in project.rglob("*") if p.is_file() and ".git" not in p.parts]
    out = {}
    for rel in paths:
        try:
            out[rel] = hashlib.sha256((project / rel).read_bytes()).hexdigest()
        except OSError:
            out[rel] = "missing"
    return out


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


OWNER_STYLE_FALLBACK = """- Lead with what happened and what it means for the owner, then what they need to do, if anything.
- Be short: say only what the owner needs; no restating, no narrating.
- Plain language, for a technical manager: no jargon; if a term cannot be avoided, say what it means in a few words.
- Be specific: say exactly which test, file or check you mean and what it checks or breaks.
- When two things could share a name, say which one you mean."""


def owner_style() -> str:
    """The owner's "How to talk to me" rules from ~/.claude/CLAUDE.md, read each time so they stay in step;
    a built-in fallback when the file or the section is missing."""
    try:
        lines = (Path.home() / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return OWNER_STYLE_FALLBACK
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+how to talk to me\s*$", line.strip(), re.I)
        if m:
            body = []
            for rest in lines[i + 1:]:
                h = re.match(r"^(#+)\s", rest)
                if h and len(h.group(1)) <= len(m.group(1)):
                    break
                body.append(rest)
            text = "\n".join(body).strip()
            return text or OWNER_STYLE_FALLBACK
    return OWNER_STYLE_FALLBACK


def prompt(cfg: Config, waiting: dict, refusals: list[str]) -> str:
    skill = (cfg.skill_dir / "SKILL.md").read_text(encoding="utf-8")
    shown, left_out = earlier_answers(cfg, waiting["phase"], waiting.get("task"))
    parts = [
        "You answer an Archon coding-loop pause for its owner, who is away. Follow this skill exactly; "
        "the code around you runs its scripts, so you only decide and write the reply.",
        "<skill>\n" + skill + "\n</skill>",
        f"Project: {cfg.project}. Read the phase document, the task's sub-phase plan under "
        f"docs/subtask/phase-{waiting['phase']}/ and the code with your tools before deciding. You may change project "
        "files (Edit, Write) and run the project's checks (make, go test, uv run, npm test, npm run <script>, "
        "npx vitest, npx playwright, npx tsc, ...) when the fix is one no task's "
        "permitted files can hold or no answer can make: for example a version pin, a contract or config file, or "
        "a check that already failed at phase start. The loop keeps what changes during a pause as the owner's "
        "(kept as the owner's), so change only what the fix needs, run the failing check to confirm it passes, "
        "and say in the reply which files you changed. Never weaken a test, a threshold or a check to make it "
        "pass, and never edit docs/subtask/ (amend does that). To see the run's state, its last transitions and "
        f"the controller's reply, run `python3 {cfg.status_tool()} {cfg.project} --phase {waiting['phase']}` "
        "(read-only). Never install packages, commit, push or answer the loop yourself.",
        "<owner_approvals>\n" + json.dumps(approvals(cfg), indent=1) + "\n</owner_approvals>\n"
        "Standing approvals the owner gave; within them, act as the owner would, including changing an approved "
        "decision they cover.",
        "<pending>\n" + json.dumps(waiting, indent=1) + "\n</pending>",
        "<owner_decisions>\n" + json.dumps(owner_decisions(cfg, waiting["phase"]), indent=1) + "\n</owner_decisions>\n"
        "The owner's own decisions at the approval gate (approve, or reject with instructions). They amend the "
        "plan and the earlier answers: where they differ, follow these.",
        "<earlier_answers>\n" + json.dumps(shown, indent=1)
        + "\n</earlier_answers>\nThese are the answers on this task, the tasks it depends on, "
        f"and the phase as a whole; every earlier answer is in {state_file(cfg, waiting['phase'])} (answer_items)."
        + (f" {left_out} earlier answers left out to keep this prompt short (the oldest, not on this task first); "
           "read them there." if left_out else ""),
    ]
    if refusals:
        parts.append("Your earlier reply was refused by the loop's own check. Fix exactly this:\n" + "\n".join(refusals))
    parts.append(
        "Write log_title and log_note for the owner, who reads the log later, by their own rules:\n<owner_style>\n"
        + owner_style() + "\n</owner_style>\n"
        "So: log_title is one short line saying what happened. log_note leads with what happened and what it means "
        "for the owner, then what they need to do, if anything; it is short, uses no jargon (or explains a term in "
        "a few words), names exactly which test, file or check is meant, and says which one when two things could "
        "share a name. Keep task, finding and answer ids (07.3, ADV-021, ANS-008) out of the sentences: put them at "
        "the end of log_note on one line starting `Refs:`.")
    parts.append(
        "Return the decision object. action 'answer': reply holds the full reply text (one line per numbered "
        "question, including the questions asked ahead numbered on, then proposal marks using only the printed "
        "labels); amend lists any files a task must be "
        "given (task id and repo-relative paths) and, when the plan expects a failure the loop can never accept, "
        "expect_failure (the assertion text) for its tests (all, or those named in tests); commands rewrites a gate "
        "command the plan states in a way that can never pass (old and new text, exact), and drop_commands removes a "
        "gate command that already failed at phase start (pending's phase_start_failures) for a reason the phase "
        "cannot fix: drop it, answer, set review and name the fix the owner must queue in log_note; log_title and log_note say "
        "what was asked and decided. review means the owner must confirm or act on this: set it only when they have "
        "to do something (an owner decision you recorded but could not confirm, a fix they must queue) and name that "
        "action in log_note; files you changed and questions left for later are logged anyway and are no reason for "
        "review. You answer for the owner: never escalate "
        "because a phase was not delegated, and never only to ask permission. Making the plan runnable is yours: "
        "amend it (files, expected failures, commands), answer, and say in log_note what you changed. action 'escalate' only when the facts cannot "
        "be found in the plan, the earlier answers or the code, or the answer would drop or change a feature the "
        "approved design shows and no owner approval covers it; a frozen pin or approved decision blocking a check "
        "that already failed at phase start is not a reason to escalate when an approval covers it: fix it; then put the reason in escalate_reason and leave reply empty.")
    return "\n\n".join(parts)


def ask_claude(cfg: Config, text: str, schema: dict) -> dict:
    """Run Claude Code headless in the project with the tools and the schema; its structured output, or ValueError."""
    cmd = cfg.claude_cmd + ["--allowedTools", cfg.tools(), "--json-schema", json.dumps(schema)]
    r = subprocess.run(cmd, input=text, cwd=cfg.project, capture_output=True, text=True, timeout=3600, env=cfg.env())
    out = json.loads(r.stdout)
    result = out.get("structured_output") if isinstance(out, dict) else None
    if result is None and isinstance(out, dict) and isinstance(out.get("result"), str):
        result = json.loads(out["result"])
    if not isinstance(result, dict):
        raise ValueError("no structured output")
    return result


def decide(cfg: Config, waiting: dict, refusals: list[str]) -> dict:
    """Claude Code's structured decision for this pause; an unusable answer becomes an escalation."""
    try:
        decision = ask_claude(cfg, prompt(cfg, waiting, refusals), DECISION_SCHEMA)
        if decision.get("action") not in ("answer", "escalate"):
            raise ValueError("no decision object")
        return decision
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"action": "escalate", "reply": "", "amend": [], "log_title": "no usable decision",
                "log_note": "", "review": True, "escalate_reason": f"Claude gave no usable decision ({exc})"}


# ---- one pause, and the watch loop ----------------------------------------------------------------

def fingerprint(waiting: dict | None) -> str:
    return hashlib.sha256(json.dumps(waiting, sort_keys=True).encode()).hexdigest() if waiting else ""


def escalate(cfg: Config, waiting: dict, reason: str, short: str | None = None) -> str:
    task = waiting.get("task") or "-"
    log(cfg, task, "left for the owner", reason, review=True)
    notify(cfg, f"coding loop phase {waiting.get('phase')} task {task}: {short or reason}")
    return "escalated"


# ---- the final approval: a verdict for the owner, never an answer -----------------------------------

def git(project: Path, *args: str, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True,
                       env={**os.environ, **(env or {})})
    return r.stdout.strip()


def disk_tree(project: Path) -> str | None:
    """Tree id of the files on disk (tracked and untracked, not ignored), the way the loop computes it: `git add -A`
    into a temporary copy of the index, then `git write-tree`. The real index is never touched. None outside git."""
    try:
        real = Path(git(project, "rev-parse", "--git-path", "index"))
        real = real if real.is_absolute() else project / real
        with tempfile.TemporaryDirectory() as tmp:
            idx = Path(tmp) / "index"
            if real.is_file():
                idx.write_bytes(real.read_bytes())
            env = {"GIT_INDEX_FILE": str(idx)}
            git(project, "add", "-A", ".", env=env)
            return git(project, "write-tree", env=env)
    except (OSError, subprocess.CalledProcessError):
        return None


def tree_difference(project: Path, a: str, b: str) -> str:
    try:
        return ", ".join(git(project, "diff", "--name-only", a, b).split()) or "no file differs"
    except (OSError, subprocess.CalledProcessError):
        return "the files could not be listed"


def approval_prompt(cfg: Config, waiting: dict, state: dict, open_p4: list[str]) -> str:
    phase = waiting["phase"]
    return "\n\n".join([
        f"The coding loop's phase {phase} in {cfg.project} waits for the owner's final approval. You prepare a "
        "verdict for the owner; you never approve, reject or answer anything, and you change no file.",
        f"Check the phase fresh, with your tools: run every task command in docs/subtask/phase-{phase}/*.json "
        "(each task's `commands`) and the project's own checks (make, go test, uv run, npm test, npm run <script>, "
        "npx vitest, npx tsc, ...), each once, and record each result. Run `git status --porcelain` before and "
        "after and confirm the checks left the files unchanged. "
        f"Run `python3 {cfg.status_tool()} {cfg.project} --phase {phase}` to see the run's state.",
        "Open P4 findings (low severity, left open by the reviews): " + (", ".join(open_p4) or "none") + ". Say "
        "for each whether it blocks approval. List every evidence row still marked PENDING in the phase's "
        "evidence documents (docs/evidence/).",
        "<owner_decisions>\n" + json.dumps(owner_decisions(cfg, phase), indent=1) + "\n</owner_decisions>",
        "Return the verdict object: verdict 'approve' only when every check passes and nothing blocks; otherwise "
        "'reject' with task (the task to send back) and why (one plain sentence). checks lists each command or "
        "check and its result (pass, or fail with the one line that matters); open_p4 and pending_evidence list "
        "what is still open; note says in a few plain sentences what the owner needs to know, by their rules:\n"
        "<owner_style>\n" + owner_style() + "\n</owner_style>",
    ])


def approval_verdict(cfg: Config, waiting: dict) -> str:
    """Leave the final approval to the owner with a verdict: the tree on disk against both reviews' trees, the
    checks Claude ran fresh, open P4 findings and PENDING evidence rows. Nothing is ever sent at this gate."""
    try:
        state = json.loads(state_file(cfg, waiting["phase"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    open_p4 = [str(f.get("id")) for f in state.get("findings") or []
               if isinstance(f, dict) and f.get("severity") == "P4" and str(f.get("status", "")).upper() == "OPEN"]
    rows: list[tuple[str, str]] = []
    blocked: list[str] = []
    disk = disk_tree(cfg.project)
    reviewed = state.get("reviewed") or {}
    for review in ("adversarial", "security"):
        seen = reviewed.get(review)
        if disk is None:
            rows.append((f"Files on disk vs the {review} review", "not compared: the project is not a git folder"))
        elif not seen:
            rows.append((f"Files on disk vs the {review} review", "no reviewed tree recorded"))
            blocked.append(f"the {review} review recorded no tree")
        elif seen == disk:
            rows.append((f"Files on disk vs the {review} review ({seen[:12]})", f"same ({disk[:12]})"))
        else:
            diff = tree_difference(cfg.project, seen, disk)
            rows.append((f"Files on disk vs the {review} review ({seen[:12]})", f"differ ({disk[:12]}): {diff}"))
            blocked.append(f"the files on disk differ from what the {review} review saw: {diff}")
    try:
        v = ask_claude(cfg, approval_prompt(cfg, waiting, state, open_p4), VERDICT_SCHEMA)
        if v.get("verdict") not in ("approve", "reject"):
            raise ValueError("no verdict")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        v = None
        rows.append(("Fresh checks", f"not run: Claude gave no usable verdict ({exc})"))
    after = disk_tree(cfg.project)
    if disk is not None and after != disk:
        diff = tree_difference(cfg.project, disk, after) if after else "unknown"
        rows.append(("Files unchanged by the checks", f"no: {diff}"))
        blocked.append(f"running the checks changed files: {diff}")
    elif disk is not None:
        rows.append(("Files unchanged by the checks", "yes"))
    for c in (v or {}).get("checks") or []:
        rows.append((str(c.get("check", "")), str(c.get("result", ""))))
    p4 = sorted(set(open_p4) | set((v or {}).get("open_p4") or []))
    rows.append(("Open P4 findings", ", ".join(p4) or "none"))
    if v is not None:
        rows.append(("Evidence rows still PENDING", ", ".join((v.get("pending_evidence") or [])) or "none"))
    if blocked:
        why = "; ".join(blocked)
        verdict = "Verdict: do not approve. " + why[0].upper() + why[1:] + "."
    elif v is None:
        verdict = "No verdict: Claude gave no usable result, so the checks were not run. Check the phase by hand."
    elif v["verdict"] == "approve":
        verdict = "Verdict: approve" + (" (the files on disk could not be compared with the reviews)." if disk is None else ".")
    else:
        verdict = f"Verdict: reject {v.get('task') or '(task not named)'}: {v.get('why') or 'see the checks below'}"
    table = "\n".join(["| Check | Result |", "| --- | --- |"]
                      + [f"| {a.replace('|', '/')} | {b.replace('|', '/')} |" for a, b in rows])
    note = (v or {}).get("note") or ""
    text = (f"The phase is ready for your approval. {verdict}\n\n{table}\n" + (f"\n{note}\n" if note else "")
            + "\nswarzy never approves or rejects: that is yours.")
    return escalate(cfg, waiting, text, short=f"ready for approval; {verdict}")


LIST_MARKER = re.compile(r"(?:^|\s+)(?:\(\d{1,3}\)|\d{1,3}\)|\d{1,3}\.(?=\s))\s*")  # knowledge.claim_problem's list rule
LIST_REFUSAL = re.compile(r"Not accepted: (\d+[a-z]): no numbered list")
SENTENCE_END = re.compile(r"[.!?]+(?=\s)")  # the loop's sentence end: a run of . ! ? before whitespace
SENTENCE_REFUSAL = re.compile(r"Not accepted: (\d+[a-z]): at most \d+ sentences")
REPAIR_PASSES = 6  # the loop names one refused rule at a time


def _one_sentence(rule: str) -> str:
    return SENTENCE_END.sub(";", rule)


def _no_list(rule: str) -> str:
    """Two or more markers are a list: split it into "a; b". A lone one is a number in the text
    ("port 0.", "count (298)"): keep the digits, drop only the brackets or the full stop."""
    if len(LIST_MARKER.findall(rule)) < 2:
        def keep(m: re.Match) -> str:
            mark = m.group(0)
            lead, trail = mark[:len(mark) - len(mark.lstrip())], mark[len(mark.rstrip()):]
            digits = re.search(r"\d+", mark).group(0)
            return f"{lead}{digits}{';' if mark.strip().endswith('.') else ''}{trail}"
        return LIST_MARKER.sub(keep, rule).strip()
    parts = [x.strip(" ;,:").rstrip(".").strip() for x in LIST_MARKER.split(rule)]
    return "; ".join(x for x in parts if x)


def repair_rules(reply: str, refusal: str, proposals: dict[str, str] | None = None) -> str | None:
    """The reply with each rule the loop refused for a mechanical wording reason rewritten: list markers
    taken out ("(1) a (2) b" -> "a; b") or sentences joined ("a. b. c" -> "a; b; c"). A refused rule
    accepted as written ("1b OK") is written out from the coder's proposal first, since the fault is in
    that wording. None when the refusal is about something else or nothing changes."""
    fixes = {label: _no_list for label in LIST_REFUSAL.findall(refusal)}
    fixes.update({label: _one_sentence for label in SENTENCE_REFUSAL.findall(refusal)})
    if not fixes:
        return None
    out = []
    for line in reply.splitlines(keepends=True):
        ok_line = re.match(r"^(\d+[a-z])\s+OK(\s*)$", line)
        if ok_line and ok_line.group(1) in fixes and (proposals or {}).get(ok_line.group(1)):
            line = f"{ok_line.group(1)}: {proposals[ok_line.group(1)]}{ok_line.group(2)}"
        m = re.match(r"^(\d+[a-z]):\s*(.*?)(\s*)$", line, re.S)
        if m and m.group(1) in fixes:
            line = f"{m.group(1)}: {fixes[m.group(1)](m.group(2))}{m.group(3)}"
        out.append(line)
    fixed = "".join(out)
    return fixed if fixed != reply else None


def plan_state(cfg: Config, phase: str) -> dict[str, str]:
    """Content hash of every file in the phase's plan folder: what --amended has to match."""
    plan = cfg.project / "docs" / "subtask" / f"phase-{phase}"
    return {str(p.relative_to(plan)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(plan.rglob("*")) if p.is_file()} if plan.is_dir() else {}


STALE_REFUSALS = ("is running, not paused", "was already answered and is awaiting continuation")


def moved_on(cfg: Config, waiting: dict) -> bool:
    """True when the pause handle() started on no longer waits (nothing waits, or a different pause)."""
    return fingerprint(pending(cfg)) != fingerprint(waiting)


def stale(cfg: Config, waiting: dict, d: dict, files: str = "") -> str:
    """The pause ended while Claude decided: nothing to send and nothing for the owner to do."""
    log(cfg, waiting.get("task"), d.get("log_title") or "pause over",
        "already answered or moved on; reply not sent" + files, review=False)
    return "stale"


ANSWER_RECORDED = re.compile(r"answered by human|resolved by human|rerouted\b.*\bby human|deferred by human"
                             r"|review questions answered")


def answer_recorded(cfg: Config, phase: str, since: float) -> bool:
    """True when the phase's history has a transition at or after `since` whose reason records a human answer
    (the answer tool's NOT ACCEPTED only means it did not see that transition within its wait)."""
    try:
        history = json.loads(state_file(cfg, phase).read_text(encoding="utf-8")).get("history", [])
    except (OSError, ValueError):
        return False
    return any(isinstance(h, dict) and float(h.get("ts") or 0) >= since - 1
               and ANSWER_RECORDED.search(str(h.get("reason") or "")) for h in history)


def handle(cfg: Config, waiting: dict) -> str:
    """Answer one pause: 'answered', 'escalated', 'stale' (the pause ended before the reply went), or 'dry-run'."""
    if waiting.get("kind") == "approval":
        return approval_verdict(cfg, waiting)
    refusals: list[str] = []
    plan_at_pause = plan_state(cfg, waiting["phase"])
    proposed = {p["label"]: p.get("rule", "") for p in waiting.get("proposals") or [] if p.get("label")}
    with tempfile.TemporaryDirectory() as tmp:
        reply = Path(tmp) / "reply.txt"
        for _ in range(cfg.max_tries):
            before = tree(cfg.project)
            d = decide(cfg, waiting, refusals)
            edits = changed(before, tree(cfg.project))
            files = f"\nFiles changed: {', '.join(edits)}" if edits else ""
            if not cfg.dry_run and moved_on(cfg, waiting):  # deciding can take an hour: the pause may be over
                return stale(cfg, waiting, d, files)
            if d["action"] == "escalate":
                return escalate(cfg, waiting, (d.get("escalate_reason") or "Claude asked for the owner") + files)
            if d["amend"] and not cfg.dry_run and not amend(cfg, waiting["phase"], d["amend"]):
                return escalate(cfg, waiting, f"could not save the plan amendment {d['amend']}")
            text = d["reply"]
            reply.write_text(text, encoding="utf-8")
            result, why = precheck(cfg, reply)
            for _pass in range(REPAIR_PASSES):  # mechanical wording rules: fix them in code, check again
                fixed = repair_rules(text, why, proposed) if result == "refused" else None
                if fixed is None:
                    break
                text = fixed
                reply.write_text(text, encoding="utf-8")
                result, why = precheck(cfg, reply)
            if result == "broken":  # the check failed, not the answer: asking Claude again cannot help
                return escalate(cfg, waiting, "the pre-check itself failed (not the answer), so the reply was not "
                                f"sent: {why}" + files)
            if result == "refused":
                refusals.append(why)
                continue
            d = {**d, "reply": text}
            if cfg.dry_run:
                log(cfg, waiting.get("task"), "dry run: " + d["log_title"], d["reply"], review=False)
                return "dry-run"
            # The plan files decide the flag, whoever changed them (Claude, an earlier try's amendment)
            # and whether or not a named amendment changed anything.
            amended = plan_state(cfg, waiting["phase"]) != plan_at_pause
            if moved_on(cfg, waiting):  # the check and wording repairs took time too
                return stale(cfg, waiting, d, files)
            sent_at = time.time()
            sent, out = send(cfg, waiting["phase"], reply, amended=amended)
            late = False
            if not sent and "NOT ACCEPTED within" in out and moved_on(cfg, waiting):
                sent = late = answer_recorded(cfg, waiting["phase"], sent_at)  # only slow to confirm
            if not sent:
                if any(m in out for m in STALE_REFUSALS) and moved_on(cfg, waiting):
                    return stale(cfg, waiting, d, files)
                return escalate(cfg, waiting, f"the loop did not accept the sent reply: {out[-500:]}")
            left = why.partition("(")[2].rstrip(")") if why.startswith("ACCEPTED (") else ""
            note = d["log_note"] + (f"\n{left}: the owner may answer them before their turn" if left else "") + files
            if late:
                note += "\nThe loop confirmed this answer late (after the answer tool stopped waiting); its history records it."
            # REVIEW only when the owner must act; changed files and questions left for later are in the note.
            log(cfg, waiting.get("task"), d["log_title"], note, review=bool(d["review"]))
            return "answered"
    return escalate(cfg, waiting, f"refused {cfg.max_tries} times: {refusals[-1] if refusals else ''}")


def wait_for_pause(cfg: Config) -> str | None:
    """The project's runs once none is running (watch.py's line); None when watching itself failed."""
    r = script(cfg, "watch.py", str(cfg.project), "--interval", str(cfg.interval), "--settle", str(cfg.settle))
    return r.stdout.strip() if r.returncode == 0 else None


def wait_for_start(cfg: Config) -> str | None:
    """The project's running run once one starts again (watch.py --until running); None when watching failed."""
    r = script(cfg, "watch.py", str(cfg.project), "--until", "running", "--interval", str(cfg.interval),
               "--settle", "0")
    return r.stdout.strip() if r.returncode == 0 else None


def notify(cfg: Config, text: str) -> None:
    if cfg.notify_cmd:
        subprocess.run(cfg.notify_cmd + [text], capture_output=True, text=True)


def run_finished(cfg: Config, runs: str) -> bool:
    """True when the run that stopped finished successfully (nothing for the owner to do): a run listed as
    completed and none failed or cancelled, or no run listed and the latest phase COMPLETE."""
    statuses = set(re.findall(r"\b(completed|failed|cancelled|canceled|paused|running|pending)\b", runs or ""))
    if statuses & {"failed", "cancelled", "canceled"}:
        return False
    if "completed" in statuses:
        return True
    phases = sorted(state_file(cfg, "x").parent.parent.glob("phase-*/state.json"), key=lambda p: p.stat().st_mtime)
    try:
        return not statuses and bool(phases) and json.loads(phases[-1].read_text(encoding="utf-8")).get("state") == "COMPLETE"
    except (OSError, ValueError):
        return False


def run(cfg: Config, once: bool = False) -> None:
    """Handle pauses (or one pause with once). An escalated pause is waited out; a run that failed or
    was stopped is reported to the owner, and watching carries on when a run of the project starts again."""
    left_for_owner = ""
    while True:
        runs = ""
        if not once:
            runs = wait_for_pause(cfg)
            if runs is None:
                time.sleep(cfg.interval)
                continue
        waiting = pending(cfg)
        if waiting is None:
            if once:
                return
            finished = run_finished(cfg, runs)
            log(cfg, "-", "run finished" if finished else "run stopped",
                f"nothing is waiting: the run {'finished' if finished else 'failed or was stopped'} ({runs})",
                review=not finished)
            notify(cfg, f"coding loop {cfg.project.name}: the run {'finished' if finished else 'stopped'} ({runs}); "
                   "swarzy waits for a run to start again")
            while (started := wait_for_start(cfg)) is None:
                time.sleep(cfg.interval)
            log(cfg, "-", "run started", f"watching again ({started})", review=False)
            continue
        if fingerprint(waiting) == left_for_owner:
            time.sleep(cfg.interval)  # still the owner's: wait until they answer
            continue
        outcome = handle(cfg, waiting)
        left_for_owner = fingerprint(waiting) if outcome == "escalated" else ""
        if once:
            return
