import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

CRON_FIELD = r"[0-9*/,\-]+"
CRON_SCHEDULE_RE = re.compile(rf"{CRON_FIELD}(?: {CRON_FIELD}){{4}}")
MARKER = "# termuxgpt "
PAUSED = "#paused# "
EXIT_TAG = "__tgpt_exit="
LOG_KEEP = 400
TMCP_RE = re.compile(r"^(?P<cmd>.*?)(?: >> (?P<log>\S+) 2>&1)? # tmcp:(?P<name>[\w.-]+)\s*$")


@dataclass
class Job:
    id: str
    label: str
    schedule: str
    command: str
    paused: bool = False
    managed: bool = True
    last_run: Optional[int] = None
    last_exit: Optional[int] = None
    log_tail: List[str] = field(default_factory=list)
    raw: str = ""
    log: str = ""


def home() -> str:
    return os.environ.get("HOME", "/data/data/com.termux/files/home")


def log_dir() -> str:
    return os.path.join(home(), "termuxGPT", "automations")


def log_path(job_id: str) -> str:
    return os.path.join(log_dir(), f"{job_id}.log")


def valid_schedule(schedule: str) -> bool:
    return bool(CRON_SCHEDULE_RE.fullmatch(schedule.strip()))


def wrap(job: Job) -> str:
    log = shlex.quote(log_path(job.id))
    tmp = shlex.quote(log_path(job.id) + ".tmp")
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return (
        f"export PATH={shlex.quote(prefix + '/bin')}:$PATH; "
        f"sh -c {shlex.quote(job.command)} >> {log} 2>&1; "
        f'echo "{EXIT_TAG}$? $(date +%s)" >> {log}; '
        f"tail -n {LOG_KEEP} {log} > {tmp} && mv {tmp} {log}"
    )


def parse(text: str) -> Tuple[List[Job], List[str]]:
    jobs: List[Job] = []
    other: List[str] = []
    lines = text.splitlines()
    i = 0
    pending_label = ""
    while i < len(lines):
        line = lines[i]
        if line.startswith(MARKER):
            try:
                meta = json.loads(line[len(MARKER):])
            except ValueError:
                meta = None
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if meta and nxt.strip():
                paused = nxt.startswith(PAUSED)
                jobs.append(Job(
                    id=str(meta.get("id", "")),
                    label=str(meta.get("label", "")),
                    schedule=str(meta.get("schedule", "")),
                    command=str(meta.get("command", "")),
                    paused=paused,
                ))
                i += 2
                continue
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            if stripped.startswith("#") and not stripped.startswith("#!"):
                pending_label = stripped.lstrip("#").strip()
            other.append(line)
            i += 1
            continue
        paused = stripped.startswith(PAUSED)
        body = stripped[len(PAUSED):] if paused else stripped
        parts = body.split(None, 5)
        if len(parts) == 6 and valid_schedule(" ".join(parts[:5])):
            schedule = " ".join(parts[:5])
            rest = parts[5]
            m = TMCP_RE.match(rest)
            if m:
                jobs.append(Job(id="tmcp-" + m.group("name"), label=m.group("name"), schedule=schedule,
                                command=m.group("cmd").strip(), paused=paused, managed=False,
                                raw=body, log=m.group("log") or ""))
                pending_label = ""
                i += 1
                continue
            jid = "legacy-" + hashlib.md5(body.encode()).hexdigest()[:8]
            jobs.append(Job(id=jid, label=pending_label or rest[:40], schedule=schedule,
                            command=rest, paused=paused, managed=False, raw=body))
            if other and other[-1].strip().lstrip("#").strip() == pending_label and pending_label:
                other.pop()
            pending_label = ""
            i += 1
            continue
        other.append(line)
        pending_label = ""
        i += 1
    return jobs, other


def render(jobs: List[Job], other: List[str]) -> str:
    out = [line for line in other]
    while out and not out[-1].strip():
        out.pop()
    for job in jobs:
        if job.managed:
            meta = {"id": job.id, "label": job.label, "schedule": job.schedule, "command": job.command}
            out.append(MARKER + json.dumps(meta, ensure_ascii=False))
            line = f"{job.schedule} {wrap(job)}"
        else:
            if job.label and not job.id.startswith("tmcp-"):
                out.append(f"# {job.label}")
            line = job.raw or f"{job.schedule} {job.command}"
        out.append((PAUSED + line) if job.paused else line)
    return "\n".join(out) + "\n"


def attach_logs(job: Job) -> Job:
    path = job.log or log_path(job.id)
    if not os.path.exists(path):
        return job
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()[-60:]
    except OSError:
        return job
    current: List[str] = []
    for line in lines:
        if line.startswith(EXIT_TAG):
            bits = line[len(EXIT_TAG):].split()
            try:
                job.last_exit = int(bits[0])
                job.last_run = int(bits[1]) if len(bits) > 1 else None
            except (ValueError, IndexError):
                pass
            job.log_tail = current[-12:]
            current = []
        else:
            current.append(line)
    if job.last_run is None:
        job.log_tail = current[-12:]
    return job


def crontab_available() -> bool:
    return shutil.which("crontab") is not None


def crond_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "crond"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_crontab() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def write_crontab(text: str) -> Optional[str]:
    try:
        r = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else (r.stderr.strip() or "crontab refused the change")


def status() -> dict:
    installed = crontab_available()
    jobs: List[Job] = []
    if installed:
        jobs, _ = parse(read_crontab())
        jobs = [attach_logs(j) for j in jobs]
    return {
        "installed": installed,
        "running": crond_running() if installed else False,
        "jobs": [asdict(j) for j in jobs],
    }


def _mutate(fn) -> dict:
    if not crontab_available():
        return {"error": "Scheduling is not set up. Run cron_setup first.", "needs_setup": True}
    jobs, other = parse(read_crontab())
    result = fn(jobs)
    if isinstance(result, dict) and result.get("error"):
        return result
    err = write_crontab(render(jobs, other))
    if err:
        return {"error": err}
    return result if isinstance(result, dict) else {"ok": True}


def _clean(command: str) -> Optional[str]:
    command = (command or "").strip()
    if not command or "\n" in command or "\r" in command:
        return None
    return command


def gate(command: str, confirmed: bool) -> Optional[dict]:
    from . import kernel
    decision = kernel.gate(command, confirmed=confirmed, tool="cron_add")
    if decision["status"] == "confirm":
        return {"error": decision["message"], "requires_confirmation": True}
    if not decision["allow"]:
        return {"error": decision["message"], "blocked": True}
    return None


def add(schedule: str, command: str, label: str = "", confirmed: bool = False) -> dict:
    schedule = (schedule or "").strip()
    cleaned = _clean(command)
    if not valid_schedule(schedule):
        return {"error": "Invalid schedule: expected 5 cron fields, e.g. '0 9 * * *'"}
    if cleaned is None:
        return {"error": "Command is required and must be a single line"}
    denied = gate(cleaned, confirmed)
    if denied:
        return denied
    job = Job(id=uuid.uuid4().hex[:10], label=(label or cleaned)[:60].strip(), schedule=schedule, command=cleaned)
    os.makedirs(log_dir(), exist_ok=True)

    def apply(jobs):
        jobs.append(job)
        return {"ok": True, "job": asdict(job)}

    return _mutate(apply)


def _find(jobs: List[Job], job_id: str) -> Optional[Job]:
    return next((j for j in jobs if j.id == job_id), None)


def remove(job_id: str = "", label: str = "") -> dict:
    def apply(jobs):
        before = len(jobs)
        jobs[:] = [j for j in jobs if not ((job_id and j.id == job_id) or (label and not job_id and j.label == label))]
        if len(jobs) == before:
            return {"error": "No such automation"}
        return {"ok": True, "removed": before - len(jobs)}

    return _mutate(apply)


def toggle(job_id: str, paused: bool) -> dict:
    def apply(jobs):
        job = _find(jobs, job_id)
        if not job:
            return {"error": "No such automation"}
        job.paused = paused
        return {"ok": True, "job": asdict(job)}

    return _mutate(apply)


def update(job_id: str, schedule: str = "", command: str = "", label: str = "", confirmed: bool = False) -> dict:
    if schedule and not valid_schedule(schedule):
        return {"error": "Invalid schedule: expected 5 cron fields"}
    if command:
        if _clean(command) is None:
            return {"error": "Command must be a single line"}
        denied = gate(command.strip(), confirmed)
        if denied:
            return denied

    def apply(jobs):
        job = _find(jobs, job_id)
        if not job:
            return {"error": "No such automation"}
        if schedule:
            job.schedule = schedule.strip()
        if command:
            job.command = command.strip()
        if label:
            job.label = label.strip()[:60]
        job.managed = True
        job.raw = ""
        job.log = ""
        if not job.id.isalnum():
            job.id = uuid.uuid4().hex[:10]
        return {"ok": True, "job": asdict(job)}

    return _mutate(apply)


SETUP_CMD = (
    'echo "Installing cronie and termux-services…"; '
    "pkg install -y cronie termux-services 2>&1 | tail -n 15; "
    "sv-enable crond >/dev/null 2>&1; "
    "pgrep -x crond >/dev/null || crond; "
    "sleep 1; "
    'command -v crontab >/dev/null && echo "crontab: ok" || echo "crontab: missing"; '
    'pgrep -x crond >/dev/null && echo "crond: running" || echo "crond: not running"'
)

START_CMD = "sv-enable crond >/dev/null 2>&1; pgrep -x crond >/dev/null || crond; sleep 1; pgrep -x crond >/dev/null && echo \"crond: running\" || echo \"crond: not running\""

