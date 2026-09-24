import json
import os
import re
import subprocess
from typing import Optional

LIBRARY_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_DIR = os.path.join(LIBRARY_DIR, "playbooks")
CHECK_DIR = os.path.join(LIBRARY_DIR, "checks")

SCHEMA_VERSION = 1

SLOT_TYPES = ("repo", "path", "url", "port", "package", "name", "text",
              "host", "ssid", "command")

DERIVES = ("basename",)

SEVERITIES = ("high", "medium", "low")

RISKS = ("safe", "write", "destructive")

EXPECT_KEYS = ("exit", "stdout_matches", "stdout_missing")

PROBE_TIMEOUT = 8

MAX_PROBE_OUTPUT = 400

PLACEHOLDER = re.compile(r"(?<!\$)\{([a-z_][a-z0-9_]*)\}")

ID_OK = re.compile(r"^[a-z][a-z0-9_]*$")


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            if path.endswith((".yaml", ".yml")):
                try:
                    import yaml
                except ImportError:
                    return None, f"{os.path.basename(path)}: PyYAML is not installed"
                return yaml.safe_load(handle), None
            return json.load(handle), None
    except (OSError, ValueError) as error:
        return None, f"{os.path.basename(path)}: {error}"


def _load_dir(directory: str):
    found, errors = {}, []
    if not os.path.isdir(directory):
        return found, errors
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".json", ".yaml", ".yml")):
            continue
        body, error = _read(os.path.join(directory, name))
        if error:
            errors.append(error)
            continue
        if not isinstance(body, dict):
            errors.append(f"{name}: not a mapping")
            continue
        item_id = str(body.get("id") or os.path.splitext(name)[0])
        if item_id in found:
            errors.append(f"{name}: duplicate id {item_id}")
            continue
        found[item_id] = dict(body, id=item_id, _file=name)
    return found, errors


def load_playbooks():
    return _load_dir(TASK_DIR)


def load_checks():
    return _load_dir(CHECK_DIR)


def load_library():
    playbooks, playbook_errors = load_playbooks()
    checks, check_errors = load_checks()
    return {
        "playbooks": playbooks,
        "checks": checks,
        "problems": (playbook_errors + check_errors
                     + validate(playbooks, checks)),
    }


def _slots(playbook: dict) -> dict:
    match = playbook.get("match")
    return match if isinstance(match, dict) else {}


def _placeholders(text: str):
    return PLACEHOLDER.findall(text or "")


def _steps_for(playbook: dict):
    steps = playbook.get("steps")
    return steps if isinstance(steps, list) else []


def _step_templates(step: dict):
    if "run" in step:
        yield "run", str(step.get("run") or "")
    if "verify" in step:
        yield "verify", str(step.get("verify") or "")
    params = step.get("with")
    if isinstance(params, dict):
        for key, value in params.items():
            if isinstance(value, str):
                yield f"with.{key}", value


def _validate_playbook(playbook: dict, checks: dict) -> list:
    problems = []
    name = playbook.get("id", "?")

    if playbook.get("schema") != SCHEMA_VERSION:
        problems.append(f"{name}: schema {playbook.get('schema')!r} is not "
                        f"{SCHEMA_VERSION}")
    if not ID_OK.match(name):
        problems.append(f"{name}: id must be lower_snake_case")
    stem = os.path.splitext(str(playbook.get("_file") or ""))[0]
    if stem and stem != name:
        problems.append(f"{name}: id does not match its filename {stem}")
    if not str(playbook.get("title") or "").strip():
        problems.append(f"{name}: no title")

    risk = playbook.get("risk", "safe")
    if risk not in RISKS:
        problems.append(f"{name}: risk {risk!r} is not one of {RISKS}")

    slots = _slots(playbook)
    declared = set(slots)
    for slot, spec in slots.items():
        if not isinstance(spec, dict):
            problems.append(f"{name}: slot {slot} is not a mapping")
            continue
        kind = spec.get("type")
        if kind not in SLOT_TYPES:
            problems.append(f"{name}: slot {slot} has unknown type {kind!r}")
        if spec.get("required") and "default" in spec:
            problems.append(f"{name}: slot {slot} is required and also has a "
                            "default")
        source = spec.get("from")
        if source is not None:
            if source not in declared:
                problems.append(f"{name}: slot {slot} derives from undeclared "
                                f"{source}")
            op = spec.get("op")
            if op not in DERIVES:
                problems.append(f"{name}: slot {slot} has unknown op {op!r}")

    steps = _steps_for(playbook)
    if not steps:
        problems.append(f"{name}: no steps")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"{name}: step {index} is not a mapping")
            continue
        has = [key for key in ("run", "tool") if key in step]
        if len(has) != 1:
            problems.append(f"{name}: step {index} needs exactly one of run "
                            "or tool")
        for where, text in _step_templates(step):
            for slot in _placeholders(text):
                if slot not in declared:
                    problems.append(f"{name}: step {index} {where} uses "
                                    f"{{{slot}}}, which is not declared")

    for slot in _placeholders(str(playbook.get("success") or "")):
        if slot not in declared:
            problems.append(f"{name}: success uses {{{slot}}}, which is not "
                            "declared")

    for requirement in playbook.get("requires") or []:
        if not isinstance(requirement, dict) or not requirement.get("check"):
            problems.append(f"{name}: a requirement is not a check reference")
            continue
        if requirement["check"] not in checks:
            problems.append(f"{name}: requires unknown check "
                            f"{requirement['check']}")

    if not playbook.get("phrases"):
        problems.append(f"{name}: no phrases to match on")

    return problems


def _validate_check(check: dict, playbooks: dict) -> list:
    problems = []
    name = check.get("id", "?")

    if check.get("schema") != SCHEMA_VERSION:
        problems.append(f"{name}: schema {check.get('schema')!r} is not "
                        f"{SCHEMA_VERSION}")
    if not ID_OK.match(name):
        problems.append(f"{name}: id must be lower_snake_case")
    stem = os.path.splitext(str(check.get("_file") or ""))[0]
    if stem and stem != name:
        problems.append(f"{name}: id does not match its filename {stem}")
    if not str(check.get("title") or "").strip():
        problems.append(f"{name}: no title")
    if not str(check.get("probe") or "").strip():
        problems.append(f"{name}: no probe")

    severity = check.get("severity")
    if severity not in SEVERITIES:
        problems.append(f"{name}: severity {severity!r} is not one of "
                        f"{SEVERITIES}")

    expect = check.get("expect")
    if expect is not None:
        if not isinstance(expect, dict) or not expect:
            problems.append(f"{name}: expect must be a mapping")
        else:
            for key in expect:
                if key not in EXPECT_KEYS:
                    problems.append(f"{name}: unknown expect key {key!r}")
            pattern = expect.get("stdout_matches")
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as error:
                    problems.append(f"{name}: stdout_matches does not compile "
                                    f"({error})")

    fix = check.get("fix")
    if fix is not None:
        if not isinstance(fix, dict) or not fix.get("playbook"):
            problems.append(f"{name}: fix must name a playbook")
        else:
            target = playbooks.get(fix["playbook"])
            if target is None:
                problems.append(f"{name}: fix names unknown playbook "
                                f"{fix['playbook']}")
            else:
                supplied = set(fix.get("inputs") or {})
                for slot, spec in _slots(target).items():
                    if not isinstance(spec, dict):
                        continue
                    if slot in supplied or "default" in spec:
                        continue
                    if spec.get("from"):
                        continue
                    if spec.get("required"):
                        problems.append(f"{name}: fix does not supply "
                                        f"{slot} for {fix['playbook']}")

    return problems


def _phrases(playbooks: dict):
    seen = {}
    for name, playbook in playbooks.items():
        for phrase in playbook.get("phrases") or []:
            key = " ".join(str(phrase).lower().split())
            seen.setdefault(key, []).append(name)
    return {key: names for key, names in seen.items() if len(names) > 1}


def _repair_cycles(playbooks: dict, checks: dict):
    def targets(check_id):
        check = checks.get(check_id) or {}
        fix = check.get("fix")
        if not isinstance(fix, dict):
            return []
        playbook = playbooks.get(fix.get("playbook")) or {}
        return [r.get("check") for r in playbook.get("requires") or []
                if isinstance(r, dict) and r.get("check")]

    cycles = []
    for start in checks:
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            for nxt in targets(current):
                if nxt == start:
                    cycles.append(" -> ".join([start, current, start]))
                elif nxt not in seen and nxt in checks:
                    seen.add(nxt)
                    stack.append(nxt)
    return sorted(set(cycles))


def validate(playbooks: dict, checks: dict) -> list:
    problems = []
    for playbook in playbooks.values():
        problems += _validate_playbook(playbook, checks)
    for check in checks.values():
        problems += _validate_check(check, playbooks)
    for phrase, names in _phrases(playbooks).items():
        problems.append(f"phrase {phrase!r} is claimed by {', '.join(names)}")
    for cycle in _repair_cycles(playbooks, checks):
        problems.append(f"repair cycle: {cycle}")
    return problems


def normalise(value: str) -> str:
    return " ".join(str(value or "").split())


def derive(spec: dict, values: dict):
    source = values.get(spec.get("from"))
    if source is None:
        return None
    op = spec.get("op")
    if op == "basename":
        return os.path.basename(str(source).rstrip("/"))
    return None


def substitute(template, values: dict):
    missing = []

    def one(text: str) -> str:
        def replace(match):
            slot = match.group(1)
            if slot not in values or values[slot] is None:
                missing.append(slot)
                return match.group(0)
            return str(values[slot])
        return PLACEHOLDER.sub(replace, text)

    if isinstance(template, str):
        return one(template), missing
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            out[key], _ = substitute(value, values)
        return out, missing
    if isinstance(template, list):
        out = []
        for value in template:
            rendered, _ = substitute(value, values)
            out.append(rendered)
        return out, missing
    return template, missing


def collect_inputs(playbook: dict, supplied: dict):
    values, missing, unused = {}, [], []
    slots = _slots(playbook)
    for slot, spec in slots.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("from"):
            continue
        if slot in supplied and supplied[slot] not in (None, ""):
            values[slot] = supplied[slot]
        elif "default" in spec:
            values[slot] = spec["default"]
        elif spec.get("required"):
            missing.append(slot)
    for slot in supplied:
        if slot not in slots:
            unused.append(slot)
    return values, missing, unused


def _fill_derived(playbook: dict, values: dict):
    slots = _slots(playbook)
    for slot, spec in slots.items():
        if not isinstance(spec, dict) or not spec.get("from"):
            continue
        made = derive(spec, values)
        if made is None:
            return f"cannot derive {slot} from {spec.get('from')}"
        values[slot] = made
    return None


def resolve(playbook_id: str, inputs=None, playbooks=None):
    playbooks = load_playbooks()[0] if playbooks is None else playbooks
    playbook = playbooks.get(playbook_id)
    if playbook is None:
        return {"ok": False, "errors": [f"No playbook named {playbook_id}"]}

    values, missing, unused = collect_inputs(playbook, dict(inputs or {}))
    if missing:
        return {"ok": False,
                "errors": [f"Missing required: {', '.join(sorted(missing))}"],
                "playbook": playbook_id}

    error = _fill_derived(playbook, values)
    if error:
        return {"ok": False, "errors": [error], "playbook": playbook_id}

    steps, unresolved = [], []
    for step in _steps_for(playbook):
        rendered = {}
        for key, value in step.items():
            text, _ = substitute(value, values)
            rendered[key] = text
        unresolved += _placeholders(json.dumps(rendered))
        steps.append(rendered)

    if unresolved:
        return {"ok": False,
                "errors": [f"Unresolved: {', '.join(sorted(set(unresolved)))}"],
                "playbook": playbook_id}

    success, _ = substitute(str(playbook.get("success") or ""), values)
    return {
        "ok": True,
        "playbook": playbook_id,
        "title": playbook.get("title"),
        "risk": playbook.get("risk", "safe"),
        "inputs": values,
        "unused": unused,
        "steps": steps,
        "success": success,
    }


def _clean(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_PROBE_OUTPUT:
        text = text[:MAX_PROBE_OUTPUT] + "..."
    return text


def _evaluate(check: dict, done) -> bool:
    expect = check.get("expect") or {}
    code = expect.get("exit", 0)
    if done.returncode != code:
        return False
    pattern = expect.get("stdout_matches")
    if pattern and not re.search(pattern, done.stdout or "", re.MULTILINE):
        return False
    missing = expect.get("stdout_missing")
    if missing and re.search(missing, done.stdout or "", re.MULTILINE):
        return False
    return True


def run_check(check: dict, timeout: int = PROBE_TIMEOUT) -> dict:
    finding = {
        "id": check.get("id"),
        "title": check.get("title"),
        "severity": check.get("severity", "medium"),
        "explain": check.get("explain", ""),
        "probe": check.get("probe", ""),
        "fix": check.get("fix"),
    }

    try:
        done = subprocess.run(str(check.get("probe") or ""), shell=True,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(finding, ok=False, detail=f"no answer in {timeout}s")
    except OSError as error:
        return dict(finding, ok=False, detail=str(error))

    ok = _evaluate(check, done)
    detail = _clean(done.stdout if done.stdout.strip() else done.stderr)
    return dict(finding, ok=ok, detail="" if ok else detail,
                code=done.returncode)


MAX_REPAIRS = 6


def requirement_ids(playbook: dict):
    return [str(r.get("check")) for r in playbook.get("requires") or []
            if isinstance(r, dict) and r.get("check")]


def apply_fix(check: dict, confirmed: bool = False, task_id: str = "",
              playbooks=None) -> dict:
    from .shell import run_captured

    name = check.get("id")
    fix = check.get("fix")
    if not isinstance(fix, dict) or not fix.get("playbook"):
        return {"check": name, "applied": False, "fixed": False,
                "reason": "no fix is declared for this check"}

    if playbooks is None:
        playbooks = load_playbooks()[0]

    plan = resolve(fix["playbook"], fix.get("inputs") or {}, playbooks)
    if not plan["ok"]:
        return {"check": name, "applied": False, "fixed": False,
                "reason": "; ".join(plan["errors"])}

    runs = []
    for step in plan["steps"]:
        command = step.get("run")
        if not command:
            runs.append({"cmd": "", "ok": False,
                         "reason": "tool steps arrive with execution"})
            return {"check": name, "applied": False, "fixed": False,
                    "reason": "this fix needs a step the engine cannot run "
                              "yet", "runs": runs,
                    "playbook": fix["playbook"]}
        result = run_captured(command, confirmed=confirmed, task_id=task_id)
        runs.append(dict(result, cmd=command))
        if not result["ok"]:
            return {"check": name, "applied": False, "fixed": False,
                    "runs": runs,
                    "reason": result["reason"] or "the step failed",
                    "playbook": fix["playbook"]}

    return {"check": name, "applied": True, "fixed": False, "runs": runs,
            "reason": "", "playbook": fix["playbook"]}


def repair(check_id: str, confirmed: bool = False, task_id: str = "",
           seen: Optional[set] = None, depth: int = 0) -> dict:
    checks, _ = load_checks()
    playbooks = load_playbooks()[0]
    seen = set(seen or set())
    check = checks.get(check_id)

    if check is None:
        return {"check": check_id, "applied": False, "fixed": False,
                "reason": f"no check named {check_id}"}
    if check_id in seen or depth > MAX_REPAIRS:
        return {"check": check_id, "applied": False, "fixed": False,
                "reason": "the repairs would not finish, so this one was "
                          "left alone"}

    seen.add(check_id)
    before = run_check(check)

    if before["ok"]:
        return {"check": check_id, "applied": False, "fixed": False,
                "reason": "", "already_ok": True, "finding": before}

    blockers = []
    fix = check.get("fix")
    target = playbooks.get((fix or {}).get("playbook")) if isinstance(fix, dict) else None
    for needed in requirement_ids(target or {}):
        outcome = repair(needed, confirmed=confirmed, task_id=task_id,
                         seen=seen, depth=depth + 1)
        if not outcome.get("fixed") and not outcome.get("already_ok"):
            blockers.append(outcome)

    if blockers:
        return {"check": check_id, "applied": False, "fixed": False,
                "reason": "a requirement could not be met first",
                "blocked_by": blockers, "finding": before}

    attempt = apply_fix(check, confirmed=confirmed, task_id=task_id,
                        playbooks=playbooks)
    after = run_check(check)
    return dict(attempt, fixed=after["ok"], check=check_id,
                finding=before, finding_after=after)


def run_doctor(only=None, timeout: int = PROBE_TIMEOUT, fix: bool = False,
               confirmed: bool = False, task_id: str = "") -> dict:
    checks, errors = load_checks()
    wanted = [name for name in (only or []) if str(name).strip()]
    unknown = [name for name in wanted if name not in checks]
    selected = ([checks[name] for name in wanted if name in checks]
                if wanted else list(checks.values()))

    order = {name: index for index, name in enumerate(SEVERITIES)}
    selected.sort(key=lambda c: (order.get(c.get("severity"), 9),
                                 str(c.get("id"))))
    findings = [run_check(check, timeout) for check in selected]

    repairs = []
    if fix:
        for finding in [f for f in findings if not f["ok"]]:
            repairs.append(repair(str(finding["id"] or ""), confirmed=confirmed,
                                  task_id=task_id))
        findings = [run_check(check, timeout) for check in selected]

    failing = [f for f in findings if not f["ok"]]
    return {
        "ok": not failing,
        "checked": len(findings),
        "failing": len(failing),
        "findings": findings,
        "repairs": repairs,
        "unknown": unknown,
        "errors": errors,
    }
