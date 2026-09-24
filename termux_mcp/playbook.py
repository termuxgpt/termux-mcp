import datetime
import json
import os
import re
import subprocess
import time
from typing import Optional

from . import changes
from .safety import safety_root

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


def user_dir() -> str:
    return safety_root("playbooks")


def load_playbooks():
    shipped, errors = _load_dir(TASK_DIR)
    mine, mine_errors = _load_dir(user_dir())
    errors = errors + mine_errors
    clash = sorted(set(shipped) & set(mine))
    for name in clash:
        errors.append(f"{name}: a saved playbook cannot take a shipped name")
        mine.pop(name, None)
    return dict(shipped, **mine), errors


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
    for label, block in (("step", steps),
                         ("rollback step", playbook.get("rollback") or [])):
        for index, step in enumerate(block):
            if not isinstance(step, dict):
                problems.append(f"{name}: {label} {index} is not a mapping")
                continue
            has = [key for key in ("run", "tool") if key in step]
            if len(has) != 1:
                problems.append(f"{name}: {label} {index} needs exactly one "
                                "of run or tool")
            for where, text in _step_templates(step):
                for slot in _placeholders(text):
                    if slot not in declared:
                        problems.append(f"{name}: {label} {index} {where} "
                                        f"uses {{{slot}}}, which is not "
                                        "declared")

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


MIN_CONFIDENCE = 0.6

STOPWORDS = frozenset(
    "a an the this that these those my your our their please can you could "
    "would to for of in on at it is are be and or with me i".split()
)

EXTRACTORS = {
    "repo": re.compile(
        r"(?<![/\w.-])(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?"
        r"(?:\s|$)", re.IGNORECASE),
    "url": re.compile(r"https?://[^\s\"']+", re.IGNORECASE),
    "path": re.compile(
        r"(?<![\w:/~])(~\S+|/(?:[\w.-]+)(?:/[\w.-]+)*)"),
    "port": re.compile(r"\b(\d{2,5})\b"),
    "host": re.compile(r"\b(?:[\w-]+\.)+[a-z]{2,}\b", re.IGNORECASE),
    "ssid": re.compile(r"[\"']([^\"']{1,32})[\"']"),
    "name": re.compile(r"[\"']([^\"']{1,40})[\"']"),
    "command": re.compile(r"`([^`]+)`"),
    "package": re.compile(
        r"\b(?:\w+\s+)?install\s+(?:-{1,2}[\w-]+\s+)*(?:the\s+)?"
        r"(?:package\s+)?([A-Za-z0-9][\w.+-]*)", re.IGNORECASE),
}

MAX_CANDIDATES = 3

MAX_REPAIRS = 6
MAX_RUNS_KEPT = 50
MAX_RECORD_TEXT = 400


def words(text) -> set:
    found = re.findall(r"[a-z0-9_]+", str(text or "").lower())
    return {word for word in found if word not in STOPWORDS and len(word) > 1}


def phrase_score(text, phrase) -> float:
    wanted = words(PLACEHOLDER.sub(" ", str(phrase or "")))
    if not wanted:
        return 0.0
    return len(wanted & words(text)) / len(wanted)


def score_playbook(text, playbook: dict) -> float:
    best = 0.0
    for phrase in playbook.get("phrases") or []:
        best = max(best, phrase_score(text, phrase))
    if best < 1.0:
        best = max(best, 0.9 * phrase_score(text, playbook.get("title") or ""))
    return round(best, 3)


def extract_inputs(playbook: dict, text: str):
    values, missing = {}, []
    for slot, spec in _slots(playbook).items():
        if not isinstance(spec, dict) or spec.get("from"):
            continue
        pattern = EXTRACTORS.get(str(spec.get("type")))
        if pattern is not None:
            found = pattern.search(str(text or ""))
            if found:
                values[slot] = (found.group(1) if found.groups()
                                else found.group(0))
                continue
        if "default" in spec or spec.get("from"):
            continue
        if spec.get("required"):
            missing.append(slot)
    return values, missing


def match_text(text: str, playbooks=None) -> list:
    if playbooks is None:
        playbooks = load_playbooks()[0]
    scored = []
    for name, playbook in playbooks.items():
        score = score_playbook(text, playbook)
        if score <= 0:
            continue
        values, missing = extract_inputs(playbook, text)
        scored.append({
            "playbook": name, "title": playbook.get("title"),
            "category": playbook.get("category"), "score": score,
            "inputs": values, "missing": missing,
            "phrases": playbook.get("phrases") or [],
        })
    scored.sort(key=lambda item: (-item["score"], item["playbook"]))
    return scored


def do_text(text: str, confirmed: bool = False, dry_run: bool = False,
            playbooks=None) -> dict:
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty", "errors": ["Nothing to do."],
                "candidates": []}

    candidates = match_text(text, playbooks)
    if not candidates:
        return {"ok": False, "reason": "unknown", "candidates": [],
                "errors": ["Nothing in the library does that yet."]}

    best = candidates[0]
    if best["score"] < MIN_CONFIDENCE:
        return {"ok": False, "reason": "unsure", "candidates": candidates[:MAX_CANDIDATES],
                "errors": ["Not sure which of these you mean."]}

    if best["missing"]:
        return {"ok": False, "reason": "missing_input",
                "matched": best["playbook"],
                "candidates": candidates[:MAX_CANDIDATES],
                "errors": [f"{best['playbook']} needs: "
                           f"{', '.join(best['missing'])}"]}

    result = run_playbook(best["playbook"], best["inputs"], confirmed=confirmed,
                          dry_run=dry_run)
    return dict(result, matched=best["playbook"], score=best["score"],
                candidates=candidates[:MAX_CANDIDATES])


SLOT_ORDER = ("repo", "url", "path", "host", "package", "port", "name",
              "command")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug[:48]


def suggest_values(steps) -> dict:
    joined = "\n".join(
        step if isinstance(step, str) else str(step.get("run") or "")
        for step in steps)
    found = {}
    for kind in SLOT_ORDER:
        pattern = EXTRACTORS.get(kind)
        if pattern is None:
            continue
        for match in pattern.finditer(joined):
            value = match.group(1) if match.groups() else match.group(0)
            if len(str(value)) < 2:
                continue
            found.setdefault(kind, [])
            if value not in found[kind]:
                found[kind].append(value)
    return found


def _slot_names(kinds) -> dict:
    names = {}
    for kind, values in kinds.items():
        for index, value in enumerate(values):
            name = kind if index == 0 else f"{kind}{index + 1}"
            names[value] = (name, kind)
    return names


def harvest(steps, title: str = "", phrases=None, values=None,
            playbook_id: str = "", overwrite: bool = False,
            playbook_dir: str = "") -> dict:
    commands = []
    for step in steps or []:
        if isinstance(step, str) and step.strip():
            commands.append(step.strip())
        elif isinstance(step, dict) and str(step.get("run") or "").strip():
            commands.append(str(step["run"]).strip())
    if not commands:
        return {"ok": False, "errors": ["Nothing to learn: no steps were given."]}

    supplied = {str(k): str(v) for k, v in (values or {}).items()}
    names = {value: (slot, _slot_type(slot))
             for slot, value in supplied.items()}
    if not supplied:
        names = _slot_names(suggest_values(commands))

    rank = {kind: index for index, kind in enumerate(SLOT_ORDER)}
    params, seen = {}, set()
    templated = []
    for command in commands:
        for value in sorted(names, key=lambda v: (rank.get(names[v][1], 99),
                                                  -len(v))):
            slot, _ = names[value]
            if value in command:
                command = command.replace(value, "{%s}" % slot)
                if slot not in seen:
                    seen.add(slot)
                    params[slot] = {"type": _slot_type(slot),
                                    "required": True}
        templated.append({"run": command})

    title = str(title or "").strip() or "Saved task"
    playbook_id = _slug(playbook_id or title)
    if not playbook_id:
        return {"ok": False, "errors": ["A title or an id is needed."]}

    draft = {
        "schema": SCHEMA_VERSION,
        "id": playbook_id,
        "title": title,
        "category": "saved",
        "risk": "write",
        "phrases": [str(p) for p in (phrases or []) if str(p).strip()]
                    or [f"do {_slug(title).replace('_', ' ')}"],
        "match": params,
        "requires": [],
        "steps": templated,
        "success": f"{title} done.",
    }

    problems = _validate_playbook(dict(draft, _file=f"{playbook_id}.json"),
                                  load_checks()[0])
    if problems:
        return {"ok": False, "errors": problems, "draft": draft}

    directory = playbook_dir or user_dir()
    path = os.path.join(directory, f"{playbook_id}.json")
    if os.path.exists(path) and not overwrite:
        return {"ok": False, "draft": draft,
                "errors": [f"{path} already exists — pass overwrite to "
                           "replace it"]}
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(draft, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        return {"ok": False, "errors": [str(error)], "draft": draft}

    return {"ok": True, "playbook": playbook_id, "path": path,
            "draft": draft, "slots": sorted(params),
            "known_values": sorted(names),
            "note": "No rollback was declared, so undo covers the files the "
                    "journal saw — not a clone, an uninstall or anything else "
                    "the safety layer cannot see."}


def _slot_type(slot: str) -> str:
    for kind in SLOT_ORDER:
        if slot == kind or slot.startswith(kind):
            return kind
    return "text"


def new_task_id(playbook_id: str) -> str:
    return f"{playbook_id}-{time.strftime('%Y%m%d-%H%M%S')}"[:64]


def runs_dir() -> str:
    return safety_root("runs")


def run_record_path(task_id: str) -> str:
    return os.path.join(runs_dir(), f"{task_id}.json")


def save_run(record: dict) -> None:
    path = run_record_path(record["task_id"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1)
        os.replace(tmp, path)
    except OSError:
        return
    try:
        kept = sorted(os.listdir(runs_dir()))
        for name in kept[:-MAX_RUNS_KEPT]:
            os.remove(os.path.join(runs_dir(), name))
    except OSError:
        return


def load_run(task_id: str):
    body, error = _read(run_record_path(str(task_id)))
    return None if error else body


def list_runs(limit: int = 20):
    try:
        names = sorted(os.listdir(runs_dir()), reverse=True)
    except OSError:
        return []
    records = []
    for name in names[:limit]:
        if not name.endswith(".json"):
            continue
        body = load_run(os.path.splitext(name)[0])
        if isinstance(body, dict):
            records.append(body)
    return records


def run_tool(name: str, params: dict) -> dict:
    from .mcp_bridge import VirtualHandler, decode_virtual, route_callable

    route = route_callable(name)
    if route is None:
        from .mcp_core import NATIVE_NAMES
        if name in NATIVE_NAMES:
            return {"ok": False, "reason": "unknown_tool",
                    "text": f"{name} is a native tool this engine cannot call "
                            "from a step — a run step reaches the same thing"}
        return {"ok": False, "reason": "unknown_tool",
                "text": f"No tool named {name}"}
    handler = VirtualHandler()
    route(handler, params or {})
    result = decode_virtual(handler)
    return {"ok": not result.get("is_error"), "reason": "",
            "text": result.get("text", "")}


def run_step(step: dict, confirmed: bool = False, task_id: str = "") -> dict:
    from .shell import run_captured

    if step.get("run"):
        return run_captured(step["run"], confirmed=confirmed, task_id=task_id)

    name = step.get("tool")
    if not name:
        return {"ok": False, "reason": "empty", "text": "Nothing to run."}

    params = dict(step.get("with") or {})
    if confirmed:
        params.setdefault("confirmed", True)
    return run_tool(name, params)


def _short(text: str) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_RECORD_TEXT else text[:MAX_RECORD_TEXT] + "..."


def run_playbook(playbook_id: str, inputs=None, confirmed: bool = False,
                 task_id: str = "", dry_run: bool = False) -> dict:
    playbooks, errors = load_playbooks()
    plan = resolve(playbook_id, inputs, playbooks)
    if not plan["ok"]:
        return {"ok": False, "playbook": playbook_id,
                "errors": plan["errors"] + errors}

    playbook = playbooks[playbook_id]
    task_id = str(task_id or new_task_id(playbook_id))[:64]

    rollback, _ = substitute(playbook.get("rollback") or [], plan["inputs"])

    if dry_run:
        return {"ok": True, "dry_run": True, "playbook": playbook_id,
                "task_id": task_id, "inputs": plan["inputs"],
                "steps": plan["steps"], "rollback": rollback,
                "success": plan["success"]}

    requirements = []
    for check_id in requirement_ids(playbook):
        outcome = repair(check_id, confirmed=confirmed, task_id=task_id)
        requirements.append(outcome)
        if not (outcome.get("fixed") or outcome.get("already_ok")):
            return {"ok": False, "playbook": playbook_id, "task_id": task_id,
                    "requirements": requirements, "steps": [],
                    "errors": [f"{check_id} is not satisfied: "
                               f"{outcome.get('reason') or 'unmet'}"]}

    started = datetime.datetime.now().isoformat(timespec="seconds")
    runs = []

    def finish(ok: bool, reason: str = ""):
        record = {
            "task_id": task_id, "playbook": playbook_id,
            "title": playbook.get("title"), "inputs": plan["inputs"],
            "started": started, "finished":
                datetime.datetime.now().isoformat(timespec="seconds"),
            "ok": ok, "reason": reason,
            "steps": [{"run": r.get("step", {}).get("run"),
                       "tool": r.get("step", {}).get("tool"),
                       "verify": bool(r.get("verify")),
                       "ok": r["ok"], "text": _short(r.get("text", ""))}
                      for r in runs],
            "rollback": rollback, "undone": False,
        }
        save_run(record)
        return record

    for step in plan["steps"]:
        result = run_step(step, confirmed=confirmed, task_id=task_id)
        runs.append(dict(result, step=step))
        if not result["ok"]:
            finish(False, "a step failed")
            return {"ok": False, "playbook": playbook_id, "task_id": task_id,
                    "requirements": requirements, "runs": runs,
                    "errors": [result.get("reason") or "a step failed"]}

        verify = step.get("verify")
        if verify:
            checked = run_step({"run": verify}, confirmed=True,
                               task_id=task_id)
            runs.append(dict(checked, step={"run": verify}, verify=True))
            if not checked["ok"]:
                finish(False, "verification failed")
                return {"ok": False, "playbook": playbook_id,
                        "task_id": task_id, "requirements": requirements,
                        "runs": runs,
                        "errors": [f"checked, and it did not hold: {verify}"]}

    record = finish(True)
    return {"ok": True, "playbook": playbook_id, "task_id": task_id,
            "requirements": requirements, "runs": runs, "record": record,
            "rollback": rollback, "success": plan["success"]}


def undo_run(task_id: str, confirmed: bool = False) -> dict:
    from .safety import snapshot_before_write

    record = load_run(str(task_id))
    if not isinstance(record, dict):
        return {"ok": False, "errors": [f"No run named {task_id}"]}
    if record.get("undone"):
        return {"ok": False, "errors": [f"{task_id} was already undone"]}

    rollback = record.get("rollback") or []
    entries = [e for e in changes.read(safety_root(""), limit=200,
                                       task=str(task_id))
               if changes.revertable(e)]

    if not confirmed:
        return {"ok": False, "reason": "confirmation_required",
                "task_id": task_id,
                "files": [e.get("path") for e in entries],
                "steps": rollback,
                "message": (f"Putting {len(entries)} file(s) back and running "
                            f"{len(rollback)} undo step(s).")}

    reverted = changes.revert(entries, snapshot_before=snapshot_before_write)
    rolls = []
    for step in rollback:
        rolls.append(dict(run_step(step, confirmed=True, task_id=task_id),
                          step=step))

    record["undone"] = True
    record["undone_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_run(record)
    return {"ok": all(r["ok"] for r in rolls), "task_id": task_id,
            "reverted": [{"path": path, "what": what} for path, what in reverted],
            "rollback": rolls}


def requirement_ids(playbook: dict):
    return [str(r.get("check")) for r in playbook.get("requires") or []
            if isinstance(r, dict) and r.get("check")]


def apply_fix(check: dict, confirmed: bool = False, task_id: str = "",
              playbooks=None) -> dict:
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
        command = step.get("run") or f"{step.get('tool')} " \
                                     f"{json.dumps(step.get('with') or {})}"
        result = run_step(step, confirmed=confirmed, task_id=task_id)
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
    wanted = str((fix or {}).get("playbook") or "") if isinstance(fix, dict) else ""
    target = playbooks.get(wanted)
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
