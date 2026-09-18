#!/usr/bin/env python3
"""Deploy the per-agent SOUL.md files into their OpenClaw workspaces.

Each agent reads its own ``SOUL.md`` from ``~/.openclaw/workspace/<agent>/``.
This copies the reviewed files in this directory into those workspaces, backing
up whatever is already there.

    python agent_souls/deploy.py --dry-run    # show what would change
    python agent_souls/deploy.py              # write, keeping backups
    python agent_souls/deploy.py --restore    # put the newest backups back

Nothing is written without an explicit run: --dry-run is the default-safe path
and is what you should run first.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from datetime import datetime
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = Path.home() / ".openclaw" / "workspace"

# One source file may serve several agents: the two writer agents do the same
# job and differ only in how long a run they are given.
DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    "writer.md": ("writer-agent-1", "long-writer-agent-1"),
    "trivia.md": ("trivia-agent-1",),
    "stories.md": ("stories-agent-1",),
    "puzzle.md": ("puzzle-agent-1",),
}

BACKUP_PREFIX = "SOUL.md.bak-"


def _targets(workspace: Path) -> list[tuple[Path, Path]]:
    """(source, destination) for every agent, whether or not it exists yet."""
    pairs: list[tuple[Path, Path]] = []
    for filename, agents in sorted(DEPLOYMENTS.items()):
        src = SOURCE_DIR / filename
        for agent in agents:
            pairs.append((src, workspace / agent / "SOUL.md"))
    return pairs


def _newest_backup(dest: Path) -> Path | None:
    backups = sorted(dest.parent.glob(BACKUP_PREFIX + "*"))
    return backups[-1] if backups else None


def deploy(workspace: Path, dry_run: bool) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    changed = skipped = missing = 0

    for src, dest in _targets(workspace):
        if not src.is_file():
            print(f"  MISSING SOURCE  {src.name}")
            missing += 1
            continue
        if not dest.parent.is_dir():
            # An agent that has never run has no workspace yet. Creating one
            # here would be guessing at a layout OpenClaw owns, so report it.
            print(f"  NO WORKSPACE    {dest.parent}  (agent never run?)")
            missing += 1
            continue

        if dest.is_file() and filecmp.cmp(src, dest, shallow=False):
            print(f"  unchanged       {dest.parent.name}/SOUL.md")
            skipped += 1
            continue

        action = "would write" if dry_run else "wrote"
        if dest.is_file():
            backup = dest.with_name(f"{BACKUP_PREFIX}{stamp}")
            if not dry_run:
                shutil.copy2(dest, backup)
            print(f"  {action:14}  {dest.parent.name}/SOUL.md  "
                  f"(backup {backup.name})")
        else:
            print(f"  {action:14}  {dest.parent.name}/SOUL.md  (new)")

        if not dry_run:
            shutil.copy2(src, dest)
        changed += 1

    verb = "would change" if dry_run else "changed"
    print(f"\n{verb} {changed}, unchanged {skipped}, skipped {missing}")
    if dry_run and changed:
        print("Re-run without --dry-run to apply.")
    return 1 if missing else 0


def restore(workspace: Path, dry_run: bool) -> int:
    restored = 0
    for _, dest in _targets(workspace):
        backup = _newest_backup(dest)
        if backup is None:
            continue
        print(f"  {'would restore' if dry_run else 'restored'}  "
              f"{dest.parent.name}/SOUL.md  from {backup.name}")
        if not dry_run:
            shutil.copy2(backup, dest)
        restored += 1
    if not restored:
        print("  no backups found")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change without writing")
    ap.add_argument("--restore", action="store_true",
                    help="restore the newest backup for each agent")
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                    help=f"OpenClaw workspace root (default: {DEFAULT_WORKSPACE})")
    args = ap.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"ERROR: workspace not found: {args.workspace}", file=sys.stderr)
        print("Pass --workspace if OpenClaw lives elsewhere on this machine.",
              file=sys.stderr)
        return 2

    print(f"Workspace: {args.workspace}\n")
    if args.restore:
        return restore(args.workspace, args.dry_run)
    return deploy(args.workspace, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
