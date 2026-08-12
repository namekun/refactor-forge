from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import apply, plan
from .spec import load_spec
from .watch import WatchConfig, WatchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="refactor-forge")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        item = sub.add_parser(command)
        item.add_argument("--spec", required=True, type=Path)
        item.add_argument("--target", required=True, type=Path)
        item.add_argument("--allow-command", action="store_true")
        item.add_argument("--json", action="store_true")
        if command == "apply":
            item.add_argument("--allow-dirty", action="store_true")
    watch = sub.add_parser("watch")
    watch.add_argument("--spec", required=True, type=Path)
    watch.add_argument("--target", required=True, type=Path)
    watch.add_argument("--state-file", type=Path)
    watch.add_argument("--reports-dir", type=Path)
    watch.add_argument("--interval", type=float, default=30.0)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--auto-apply", action="store_true")
    watch.add_argument("--allow-command", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = load_spec(args.spec.resolve())
        if args.command == "watch":
            target = args.target.resolve()
            service = WatchService(WatchConfig(
                target=target,
                spec=spec,
                state_file=(args.state_file.resolve() if args.state_file else target / ".refactor-forge" / "watch-state.json"),
                reports_dir=(args.reports_dir.resolve() if args.reports_dir else target / ".refactor-forge" / "reports"),
                interval_seconds=args.interval,
                auto_apply=args.auto_apply,
                allow_commands=args.allow_command,
            ))
            if args.once:
                event = service.tick()
                print(json.dumps({
                    "status": event.status,
                    "message": event.message,
                    "report_path": str(event.report_path) if event.report_path else None,
                }, ensure_ascii=False))
            else:
                service.run_forever()
            return 0
        if args.command == "plan":
            report = plan(spec, args.target.resolve(), args.allow_command)
        else:
            report = apply(spec, args.target.resolve(), args.allow_command, args.allow_dirty)
        if args.json:
            print(report.to_json())
        else:
            print(f"Transformation: {report.transformation}")
            print(f"Mode: {report.mode}")
            print(f"Changed files: {len(report.changed_files)}")
            for message in report.messages:
                print(f"  {message}")
            for verification in report.verification:
                print(f"  {verification}")
            if report.diff:
                print("\n" + report.diff, end="" if report.diff.endswith("\n") else "\n")
            else:
                print("No changes.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
