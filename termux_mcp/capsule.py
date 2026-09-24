import datetime
import hashlib
import json
import os
import subprocess
import tempfile

from . import playbook as pb
from .config import HOME

CAPSULE_VERSION = 1
NAMESPACE = "termux-playbook"
MAX_CAPSULE_BYTES = 256 * 1024
SIGN_TIMEOUT = 20

SIGN_HINT = ("No SSH key was found to sign with. Run: ssh-keygen -t "
             "ed25519 — or the capsule still carries a checksum.")


def capsule_dir() -> str:
    return pb.safety_root("capsules")


def _canonical(playbook: dict) -> bytes:
    body = {k: v for k, v in playbook.items() if not k.startswith("_")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ssh_key() -> str:
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        path = os.path.join(HOME, ".ssh", name)
        if os.path.isfile(path):
            return path
    return ""


def _installs(playbook: dict, checks: dict) -> list:
    packages = set()
    for check_id in pb.requirement_ids(playbook):
        fix = (checks.get(check_id) or {}).get("fix") or {}
        if isinstance(fix, dict) and fix.get("playbook") == "install_package":
            package = (fix.get("inputs") or {}).get("package")
            if package:
                packages.add(str(package))
    for step in playbook.get("steps") or []:
        found = pb.EXTRACTORS["package"].search(str(step.get("run") or ""))
        if found:
            named = [group for group in found.groups() if group]
            if named:
                packages.add(str(named[0]))
    return sorted(packages)


def manifest(playbook: dict, checks: dict) -> dict:
    return {
        "id": playbook.get("id"),
        "title": playbook.get("title"),
        "risk": playbook.get("risk", "safe"),
        "phrases": playbook.get("phrases") or [],
        "requires": pb.requirement_ids(playbook),
        "installs": _installs(playbook, checks),
        "takes": sorted((playbook.get("match") or {}).keys()),
        "steps": [step.get("run") or f"tool: {step.get('tool')}"
                  for step in playbook.get("steps") or []],
        "rollback": [step.get("run") or f"tool: {step.get('tool')}"
                     for step in playbook.get("rollback") or []],
    }


def sign(data: bytes, key_path: str, identity: str) -> dict:
    with tempfile.TemporaryDirectory() as work:
        target = os.path.join(work, "content")
        with open(target, "wb") as handle:
            handle.write(data)
        try:
            done = subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", NAMESPACE,
                 target],
                capture_output=True, text=True, timeout=SIGN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"error": str(error)}
        if done.returncode != 0:
            return {"error": (done.stderr or "signing failed").strip()}
        try:
            with open(target + ".sig", encoding="utf-8") as handle:
                signature = handle.read()
        except OSError as error:
            return {"error": str(error)}

    public = ""
    try:
        with open(key_path + ".pub", encoding="utf-8") as handle:
            public = handle.read().strip()
    except OSError:
        public = ""

    return {"identity": identity, "namespace": NAMESPACE, "key": public,
            "sig": signature}


def verify(data: bytes, signature: dict, against: str = "") -> dict:
    sig = str((signature or {}).get("sig") or "")
    identity = str((signature or {}).get("identity") or "termux")
    if not sig:
        return {"present": False, "ok": False, "detail": "no signature"}

    key = str(against or (signature or {}).get("key") or "")
    if not key or not key.startswith(("ssh-", "ecdsa-", "sk-")):
        return {"present": True, "ok": False, "trusted": False,
                "detail": "the signature carries no usable public key"}

    with tempfile.TemporaryDirectory() as work:
        signers = os.path.join(work, "allowed_signers")
        with open(signers, "w", encoding="utf-8") as handle:
            handle.write(f"{identity} {key}\n")
        sig_path = os.path.join(work, "content.sig")
        with open(sig_path, "w", encoding="utf-8") as handle:
            handle.write(sig)
        try:
            done = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", signers, "-I", identity,
                 "-n", NAMESPACE, "-s", sig_path],
                input=data, capture_output=True, timeout=SIGN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"present": True, "ok": False, "detail": str(error)}

    ok = done.returncode == 0
    return {"present": True, "ok": ok, "identity": identity,
            "trusted": bool(against),
            "detail": "" if ok else "the signature does not match this content"}


def export_capsule(playbook_id: str, sign_it: bool = False,
                   out_dir: str = "") -> dict:
    playbooks, errors = pb.load_playbooks()
    playbook = playbooks.get(str(playbook_id))
    if playbook is None:
        return {"ok": False,
                "errors": [f"No playbook named {playbook_id}"] + errors}

    checks = pb.load_checks()[0]
    data = _canonical(playbook)
    body = {
        "capsule": CAPSULE_VERSION,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "server": pb.SCHEMA_VERSION,
        "sha256": digest(data),
        "manifest": manifest(playbook, checks),
        "playbook": json.loads(data.decode()),
    }

    note = ""
    if sign_it:
        key = _ssh_key()
        if not key:
            note = SIGN_HINT
        else:
            user = (os.environ.get("USER") or os.environ.get("USERNAME")
                    or "user")
            host = os.uname().nodename if hasattr(os, "uname") else "device"
            result = sign(data, key, f"{user}@{host}")
            if result.get("error"):
                note = f"Signing failed: {result['error']}"
            else:
                body["signature"] = result

    directory = out_dir or capsule_dir()
    path = os.path.join(directory, f"{playbook_id}.capsule.json")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        return {"ok": False, "errors": [str(error)]}

    return {"ok": True, "path": path, "capsule": body, "signed":
            "signature" in body, "note": note, "playbook": playbook_id,
            "manifest": body["manifest"], "signature": body.get("signature") or {},
            "size": os.path.getsize(path)}


def read_capsule(path: str):
    if not os.path.isfile(path):
        return None, f"No capsule at {path}"
    body: dict = {}
    if os.path.getsize(path) > MAX_CAPSULE_BYTES:
        return None, "That capsule is too large to be one of ours"
    try:
        with open(path, encoding="utf-8") as handle:
            body = json.load(handle)
    except (OSError, ValueError) as error:
        return None, str(error)
    if not isinstance(body, dict) or body.get("capsule") != CAPSULE_VERSION:
        return None, (f"Unknown capsule version "
                      f"{(body or {}).get('capsule')!r}")
    if not isinstance(body.get("playbook"), dict):
        return None, "The capsule carries no playbook"
    return body, None


def preview_capsule(path: str, signer: str = "") -> dict:
    body, error = read_capsule(path)
    if error:
        return {"ok": False, "errors": [error]}

    data = _canonical(body["playbook"])
    checksum = digest(data)
    intact = checksum == body.get("sha256")
    signature = body.get("signature")
    verdict = verify(data, signature, signer) if signature else {"present": False}

    playbook_id = body["playbook"].get("id", "")
    library = dict(pb.shipped_playbooks())
    library[playbook_id] = body["playbook"]
    problems = [problem for problem in pb.validate(library, pb.load_checks()[0])
                if problem.startswith(f"{playbook_id}:")]
    clash = body["playbook"]["id"] in pb.shipped_playbooks()
    missing_checks = [check for check in pb.requirement_ids(body["playbook"])
                      if check not in pb.load_checks()[0]]

    return {
        "ok": intact and not problems,
        "path": path,
        "intact": intact,
        "checksum": checksum,
        "signature": verdict,
        "problems": problems,
        "clash": clash,
        "missing_checks": missing_checks,
        "manifest": body.get("manifest", {}),
        "playbook": body["playbook"],
    }


def import_capsule(path: str, confirmed: bool = False,
                   overwrite: bool = False, signer: str = "",
                   playbook_dir: str = "") -> dict:
    found = preview_capsule(path, signer=signer)
    if not found.get("intact"):
        return dict(found, errors=["The capsule does not match its own "
                                   "checksum — it was changed after it was "
                                   "written."])
    if found["problems"]:
        return dict(found, errors=found["problems"])
    if found["clash"]:
        return dict(found, errors=[f"{found['playbook']['id']} is the name of "
                                   "a shipped playbook"])

    if not confirmed:
        return dict(found, reason="confirmation_required",
                    errors=[f"Install {found['playbook']['id']}? Send it again "
                            "with confirmed: true."])

    playbook_id = found["playbook"]["id"]
    directory = playbook_dir or pb.user_dir()
    target = os.path.join(directory, f"{playbook_id}.json")
    if os.path.exists(target) and not overwrite:
        return dict(found, errors=[f"{target} already exists — pass overwrite "
                                   "to replace it"])

    try:
        os.makedirs(directory, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(found["playbook"], handle, indent=2)
            handle.write("\n")
    except OSError as error:
        return dict(found, errors=[str(error)])

    playbooks, _ = pb.load_playbooks()
    return dict(found, installed=True, target=target,
                ok=playbook_id in playbooks)


