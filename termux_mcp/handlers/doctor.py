from ..playbook import (do_text, harvest, load_library, run_doctor,
                        run_playbook, undo_run)
from .terminal import _text_response
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

    _text_response(handler, render(report))


def _inputs(data: dict) -> dict:
    for key in ("with", "inputs"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def render_run(result: dict) -> str:
    name = result.get("playbook") or "?"
    if not result.get("ok"):
        lines = [f"{name}: not run."]
        for blocker in result.get("requirements") or []:
            if blocker.get("fixed") or blocker.get("already_ok"):
                continue
            lines.append(f"  {blocker['check']} is not satisfied: "
                         f"{blocker.get('reason') or 'unmet'}")
            for inner in blocker.get("blocked_by") or []:
                lines.append(f"    blocked by {inner['check']}")
        for error in result.get("errors") or []:
            lines.append(f"  {error}")
        for run in result.get("runs") or []:
            mark = "ok" if run["ok"] else "failed"
            lines.append(f"  $ {run.get('step', {}).get('run') or 'step'}"
                         f" — {mark}")
            if not run["ok"] and run.get("text"):
                lines.append("    " + run["text"][:300].replace(
                    "\n", "\n    "))
        return "\n".join(lines)

    task_id = result.get("task_id")
    lines = [f"{name} done ({task_id})."]
    if result.get("success"):
        lines.append(result["success"])
    for run in result.get("runs") or []:
        what = run.get("step", {}).get("run") or run.get("step", {}).get("tool")
        lines.append(f"  {'checked' if run.get('verify') else 'ran'}: {what}")
    if result.get("rollback"):
        lines.append(f"Undo the whole run: playbooks with undo={task_id} "
                     f"(and confirmed: true)")
    return "\n".join(lines)


def _list(value) -> list:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").splitlines()
            if part.strip()]


def render_harvest(result: dict) -> str:
    if not result.get("ok"):
        lines = ["Not saved."]
        for error in result.get("errors") or []:
            lines.append(f"  {error}")
        for step in (result.get("draft") or {}).get("steps") or []:
            lines.append(f"  $ {step['run']}")
        return "\n".join(lines)

    draft = result["draft"]
    lines = [f"Saved as {result['playbook']} — {draft['title']}.",
             f"  file: {result['path']}"]
    for step in draft["steps"]:
        lines.append(f"  $ {step['run']}")
    if result.get("slots"):
        lines.append("  takes: " + ", ".join(result["slots"]))
    lines.append(f"  phrases: {'; '.join(draft['phrases'])}")
    lines.append("  " + result["note"])
    return "\n".join(lines)


def handle_harvest(handler, data: dict) -> None:
    result = harvest(steps=_list(data.get("steps")),
                     title=str(data.get("title") or ""),
                     phrases=_list(data.get("phrases")),
                     values=data.get("values") if isinstance(
                         data.get("values"), dict) else {},
                     playbook_id=str(data.get("playbook")
                                     or data.get("id") or ""),
                     overwrite=_flag(data, "overwrite"))

    if str(data.get("format") or "").strip().lower() == "json":
        json_response(handler, 200, result)
        return
    _text_response(handler, render_harvest(result))


def _signature_line(signature: dict) -> str:
    if not signature or not signature.get("present"):
        return "  unsigned"
    who = signature.get("identity") or "unknown"
    if not signature.get("ok"):
        return f"  signed by {who} — and the signature does not match"
    if signature.get("trusted"):
        return f"  signed by {who}, and the key is the one you named"
    return (f"  signed by {who} — the key is inside the capsule, so this "
            "proves it is whole, not who wrote it")


def render_capsule(result: dict, action: str) -> str:
    if result.get("errors") and not result.get("manifest"):
        return "\n".join(["Nothing done."] + [f"  {e}"
                                              for e in result["errors"]])

    manifest = result.get("manifest") or {}
    lines = [f"{manifest.get('id') or result.get('playbook')} — "
             f"{manifest.get('title') or ''}".strip(" —")]

    if action == "export" and result.get("ok"):
        lines.append(f"  written: {result['path']} ({result.get('size')} bytes)")
        lines.append("  signed" if result.get("signed") else "  unsigned")
        if result.get("note"):
            lines.append(f"  {result['note']}")
        return "\n".join(lines)

    lines.append(f"  risk: {manifest.get('risk')}"
                 + (f" | takes: {', '.join(manifest.get('takes') or [])}"
                    if manifest.get("takes") else ""))
    if manifest.get("installs"):
        lines.append(f"  would install: {', '.join(manifest['installs'])}")
    if manifest.get("requires"):
        lines.append(f"  needs: {', '.join(manifest['requires'])}")
    lines.append(_signature_line(result.get("signature") or {}))

    for step in manifest.get("steps") or []:
        lines.append(f"  $ {step}")
    for step in manifest.get("rollback") or []:
        lines.append(f"  undo: {step}")

    if not result.get("intact"):
        lines.append("  THIS DOES NOT MATCH ITS CHECKSUM — it was changed "
                     "after it was written")
    for problem in result.get("problems") or []:
        lines.append(f"  broken: {problem}")
    if result.get("clash"):
        lines.append("  the name is taken by a shipped playbook")
    if result.get("missing_checks"):
        lines.append("  needs checks this phone does not have: "
                     + ", ".join(result["missing_checks"]))

    if result.get("installed"):
        lines.append(f"  installed: {result.get('target')}")
    elif result.get("reason") == "confirmation_required":
        lines.append("  Send it again with confirmed: true to install it.")
    for error in result.get("errors") or []:
        lines.append(f"  {error}")
    return "\n".join(line for line in lines if line.strip())


def handle_capsule(handler, data: dict) -> None:
    from ..capsule import export_capsule, import_capsule, preview_capsule

    action = str(data.get("action") or "preview").strip().lower()
    path = str(data.get("path") or "").strip()
    playbook = str(data.get("playbook") or "").strip()

    if action == "export":
        result = export_capsule(playbook, sign_it=_flag(data, "sign"))
    elif action == "import":
        result = import_capsule(path, confirmed=_flag(data, "confirmed"),
                                overwrite=_flag(data, "overwrite"),
                                signer=str(data.get("signer") or ""))
    else:
        result = preview_capsule(path, signer=str(data.get("signer") or ""))

    if str(data.get("format") or "").strip().lower() == "json":
        json_response(handler, 200, result)
        return
    _text_response(handler, render_capsule(result, action))


def render_undo(result: dict) -> str:
    if result.get("errors"):
        return "\n".join(str(error) for error in result["errors"])

    if result.get("reason") == "confirmation_required":
        lines = [f"{result['task_id']} would put back "
                 f"{len(result.get('files') or [])} file(s) and run "
                 f"{len(result.get('steps') or [])} undo step(s)."]
        for path in result.get("files") or []:
            lines.append(f"  file: {path}")
        for step in result.get("steps") or []:
            lines.append(f"  step: {step.get('run') or step.get('tool')}")
        lines.append("Send it again with confirmed: true.")
        return "\n".join(lines)

    lines = [f"{result['task_id']} undone."]
    for entry in result.get("reverted") or []:
        lines.append(f"  {entry['what']}: {entry['path']}")
    for run in result.get("rollback") or []:
        mark = "ok" if run["ok"] else "failed"
        what = run.get("step", {}).get("run") or run.get("step", {}).get("tool")
        lines.append(f"  {what} — {mark}")
        if not run["ok"] and run.get("text"):
            lines.append("    " + run["text"][:200])
    return "\n".join(lines)


def render_do(result: dict, text: str) -> str:
    if result.get("playbook"):
        head = f"matched: {result['matched']}" if result.get("matched") else ""
        return "\n".join(part for part in (head, render_run(result)) if part)

    lines = []
    if result.get("matched"):
        lines.append(f"matched: {result['matched']}")
    lines.append(f"{text.strip()!r}: nothing was run.")
    for error in result.get("errors") or []:
        lines.append(f"  {error}")

    candidates = result.get("candidates") or []
    for candidate in candidates:
        needs = (f" — needs {', '.join(candidate['missing'])}"
                 if candidate.get("missing") else "")
        lines.append(f"  {candidate['playbook']} "
                     f"({candidate['score']:.2f}): "
                     f"{'; '.join(candidate['phrases'][:2])}{needs}")

    if result.get("reason") == "unknown":
        library = load_library()
        lines.append("  known: " + ", ".join(sorted(library["playbooks"])))
    return "\n".join(lines)


def handle_do(handler, data: dict) -> None:
    text = str(data.get("text") or data.get("say") or "").strip()
    result = do_text(text, confirmed=_flag(data, "confirmed"),
                     dry_run=_flag(data, "dry_run"))

    if str(data.get("format") or "").strip().lower() == "json":
        json_response(handler, 200, result)
        return
    _text_response(handler, render_do(result, text))


def handle_playbooks(handler, data: dict) -> None:
    library = load_library()
    wanted = str(data.get("playbook") or "").strip()
    as_json = str(data.get("format") or "").strip().lower() == "json"

    undo = str(data.get("undo") or "").strip()
    if undo:
        result = undo_run(undo, confirmed=_flag(data, "confirmed"))
        if as_json:
            json_response(handler, 200, result)
            return
        _text_response(handler, render_undo(result))
        return

    if wanted and (_flag(data, "run") or _flag(data, "dry_run")):
        result = run_playbook(wanted, _inputs(data),
                              confirmed=_flag(data, "confirmed"),
                              task_id=str(data.get("task_id") or "")[:64],
                              dry_run=_flag(data, "dry_run"))
        if as_json:
            json_response(handler, 200, result)
            return
        _text_response(handler, render_run(result))
        return

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
