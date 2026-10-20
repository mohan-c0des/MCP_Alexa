"""Run one automation from the command line, for schedulers (Windows Task Scheduler, cron).

  python extras/automations_cli.py weather_snapshot city=Vijayawada days=3
  python extras/automations_cli.py notes_compile folder=notes pdf=true --notify
  python extras/automations_cli.py repos_sync --yes          (automations that change things need --yes)

Use the Python from your server environment so the same packages and .env are found.
"""
import json
import sys

from automations import AUTOMATIONS, _coerce, execute


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if not args or args[0] not in AUTOMATIONS:
        print("Usage: python extras/automations_cli.py <name> [key=value ...] [--yes] [--notify]")
        print("Available:", ", ".join(sorted(AUTOMATIONS)))
        return 1
    name, auto = args[0], AUTOMATIONS[args[0]]
    params = {k: v for k, _, v in (a.partition("=") for a in args[1:]) if k}
    params = _coerce(auto.fn, params)
    if auto.needs_approval(params) and "--yes" not in flags:
        what = auto.preview(params) if auto.preview else auto.summary
        print(f"This would: {what}\nRe-run with --yes to allow it.")
        return 2
    print(json.dumps(execute(name, params, notify_when_done="--notify" in flags), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))