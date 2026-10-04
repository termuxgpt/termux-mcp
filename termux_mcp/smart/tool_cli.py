import json
import shlex
import sys

ALLOWED = {"briefing", "net_check", "storage_report", "security_check", "backup_incremental", "health_watch"}
BACKUP_ACTIONS = {"backup", "verify", "list"}


def main(argv) -> int:
    if not argv or argv[0] not in ALLOWED:
        print(f"usage: tool_cli <{'|'.join(sorted(ALLOWED))}> [json] [--notify TITLE]")
        return 2
    tool = argv[0]
    params = {}
    title = ""
    rest = argv[1:]
    if "--notify" in rest:
        i = rest.index("--notify")
        title = rest[i + 1] if i + 1 < len(rest) else "TermuxGPT"
        rest = rest[:i] + rest[i + 2:]
    if rest:
        try:
            params = json.loads(rest[0])
        except ValueError:
            print("params must be JSON")
            return 2
    if tool == "backup_incremental" and str(params.get("action", "backup")) not in BACKUP_ACTIONS:
        print("backup_incremental: only backup, verify and list run from a schedule")
        return 2
    from . import run_smart_tool
    from . import base
    result = run_smart_tool(tool, params)
    digest = result.get("digest", result)
    ok = digest.get("ok", True) is not False
    summary = str(digest.get("summary") or ("done" if ok else "failed"))
    if tool == "briefing" and digest.get("lines"):
        summary = "\n".join(digest["lines"])
    print(summary)
    if title:
        base.sh("termux-notification --title " + shlex.quote(title) + " --content " + shlex.quote(summary[:400]),
                timeout=15, check_risk=False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
