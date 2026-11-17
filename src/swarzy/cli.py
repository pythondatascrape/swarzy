"""swarzy command line: watch a project's coding loop and answer its pauses, or handle the current one."""
from __future__ import annotations

import argparse
from pathlib import Path

from swarzy.agent import Config, run


def main() -> int:
    ap = argparse.ArgumentParser(prog="swarzy", description=__doc__)
    ap.add_argument("project", help="the Archon project folder the coding loop runs in")
    ap.add_argument("--log", required=True, help="markdown file every decision is appended to")
    ap.add_argument("--once", action="store_true", help="handle the pause waiting now, then exit")
    ap.add_argument("--dry-run", action="store_true", help="decide and pre-check, but send nothing and amend nothing")
    ap.add_argument("--max-tries", type=int, default=3, help="refused replies before leaving it to the owner")
    a = ap.parse_args()
    run(Config(project=Path(a.project).resolve(), log=Path(a.log), max_tries=a.max_tries, dry_run=a.dry_run),
        once=a.once)
    return 0
