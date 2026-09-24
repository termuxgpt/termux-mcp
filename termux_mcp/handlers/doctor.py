from ..playbook import load_library, run_doctor
from ..utils import json_response


def _wanted(data: dict):
    raw = data.get("check") or data.get("checks") or ""
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _fix_line(fix) -> str:
    if not isinstance(fix, dict) or not fix.get("playbook"):
        return ""
    inputs = fix.get("inputs") or {}
    shown = ", ".join(f"{k}={v}" for k, v in inputs.items())
    return f"\n         Fix: {fix['playbook']}{' ' + shown if shown else ''}"


def render(report: dict) -> str:
    findings = report["findings"]
    passing = [f for f in findings if f["ok"]]
    lines = [f"Termux doctor: {len(passing)} of {len(findings)} checks pass."]

    if report["unknown"]:
        lines.append("")
        lines.append("No such check: " + ", ".join(report["unknown"]))

    failing = [f for f in findings if not f["ok"]]
    if failing:
        lines.append("")
        lines.append("NEEDS ATTENTION")
        for finding in failing:
            lines.append(f"  [{finding['severity']}] {finding['title']} "
                         f"({finding['id']})")
            if finding["detail"]:
                lines.append("         " + finding["detail"].replace(
                    "\n", "\n         "))
            else:
                lines.append(f"         checked with: {finding['probe']}")
            if finding["explain"]:
                lines.append("         " + finding["explain"])
            lines.append(_fix_line(finding["fix"]))

    repairs = report.get("repairs") or []
    if repairs:
        lines.append("")
        lines.append("FIXES")
        for attempt in repairs:
            state = ("fixed" if attempt.get("fixed")
                     else "already fine" if attempt.get("already_ok")
                     else "still failing")
            lines.append(f"  {attempt['check']}: {state}"
                         + (f" — {attempt['reason']}"
                            if not attempt.get("fixed")
                            and attempt.get("reason") else ""))
            for blocker in attempt.get("blocked_by") or []:
                finding = blocker.get("finding") or {}
                lines.append(f"         blocked by {blocker['check']}"
                             f": {finding.get('title') or blocker.get('reason')}")
                if finding.get("detail"):
                    lines.append("           " + finding["detail"].replace(
                        "\n", "\n           "))
            for run in attempt.get("runs") or []:
                lines.append(f"         ran: {run['cmd']}"
                             + ("" if run.get("ok")
                                else f"  ({run.get('reason') or 'failed'})"))

    if passing:
        lines.append("")
        lines.append("PASSING")
        lines.append("  " + ", ".join(f["id"] for f in passing))

    if report["errors"]:
        lines.append("")
        lines.append("The library itself has problems:")
        lines += ["  " + problem for problem in report["errors"]]

    return "\n".join(line for line in lines if line is not None)


def _flag(data: dict, key: str) -> bool:
    value = data.get(key)
    return value is True or str(value).strip().lower() in ("1", "true", "yes")


def handle_doctor(handler, data: dict) -> None:
    report = run_doctor(only=_wanted(data),
                        fix=_flag(data, "fix"),
                        confirmed=_flag(data, "confirmed"),
                        task_id=str(data.get("task_id") or "")[:64])

    if str(data.get("format") or "").strip().lower() == "json":
        json_response(handler, 200, report)
        return

    body = render(report).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_playbooks(handler, data: dict) -> None:
    library = load_library()
    wanted = str(data.get("playbook") or "").strip()

    if wanted:
        playbook = library["playbooks"].get(wanted)
        if playbook is None:
            json_response(handler, 404, {"error": f"No playbook named {wanted}"})
            return
        json_response(handler, 200, {
            "playbook": {k: v for k, v in playbook.items()
                         if not k.startswith("_")},
        })
        return

    json_response(handler, 200, {
        "playbooks": [
            {"id": p["id"], "title": p.get("title"),
             "category": p.get("category"), "risk": p.get("risk", "safe"),
             "phrases": p.get("phrases") or [],
             "requires": [r.get("check") for r in p.get("requires") or []]}
            for p in sorted(library["playbooks"].values(),
                            key=lambda p: p["id"])
        ],
        "checks": [
            {"id": c["id"], "title": c.get("title"),
             "severity": c.get("severity")}
            for c in sorted(library["checks"].values(), key=lambda c: c["id"])
        ],
        "problems": library["problems"],
    })
