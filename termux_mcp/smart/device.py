import json
import os
import shlex
import time
import uuid

from . import base


def _ocr(image: str, lang: str = "eng") -> dict:
    if not base.which("tesseract"):
        return {"ok": False, "errors": [{"code": "E_NOT_FOUND_CMD", "subject": "tesseract",
                                         "msg": "install with: pkg install tesseract"}]}
    res = base.sh(f"tesseract {shlex.quote(image)} - -l {shlex.quote(lang)} 2>/dev/null", timeout=90,
                  check_risk=False)
    text = "\n".join(l for l in res.out.splitlines() if l.strip())
    return {"ok": res.ok, "text": text[:4000], "chars": len(text),
            "summary": f"read {len(text)} characters" if text else "no text found"}


def _codes(image: str) -> list:
    if not base.which("zbarimg"):
        return []
    res = base.sh(f"zbarimg --quiet {shlex.quote(image)} 2>/dev/null", timeout=30, check_risk=False)
    out = []
    for line in res.out.splitlines():
        if ":" in line:
            kind, data = line.split(":", 1)
            out.append({"type": kind, "data": data})
    return out


def screen_ocr(image: str = "", lang: str = "eng") -> dict:
    path = os.path.expanduser(image) if image else os.path.join(base.ensure_dir(base.root("vision")), "screen.png")
    if not image:
        res = base.sh(f"termux-screenshot -f {shlex.quote(path)} 2>/dev/null || screencap -p {shlex.quote(path)}",
                      timeout=20, check_risk=False)
        if not os.path.exists(path):
            return {"ok": False, "errors": [{"code": "E_TERMUX_API",
                                             "msg": "could not capture the screen; pass image: <path>"}],
                    "detail": res.text[:200]}
    if not os.path.exists(path):
        return {"ok": False, "errors": [{"code": "E_NO_FILE", "msg": f"{path} not found"}]}
    body = _ocr(path, lang)
    codes = _codes(path)
    if codes:
        body["codes"] = codes
    body["image"] = path
    return body


def camera_scan(camera: int = 0, lang: str = "eng", image: str = "") -> dict:
    path = os.path.expanduser(image) if image else os.path.join(base.ensure_dir(base.root("vision")), "camera.jpg")
    if not image:
        res = base.sh(f"termux-camera-photo -c {int(camera)} {shlex.quote(path)}", timeout=25, check_risk=False)
        if not res.ok or not os.path.exists(path):
            return {"ok": False, "errors": [{"code": "E_TERMUX_API", "msg": "camera unavailable (Termux:API + permission)"}]}
    codes = _codes(path)
    body = {"ok": True, "image": path}
    if codes:
        body["codes"] = codes
        body["summary"] = f"{len(codes)} code(s): " + ", ".join(c["data"][:60] for c in codes)
    ocr = _ocr(path, lang)
    if ocr.get("ok") and ocr.get("text"):
        body["text"] = ocr["text"]
        body.setdefault("summary", ocr["summary"])
    if "summary" not in body:
        body["summary"] = "nothing readable found"
        if not base.which("tesseract") and not base.which("zbarimg"):
            body["ok"] = False
            body["errors"] = [{"code": "E_NOT_FOUND_CMD", "subject": "tesseract",
                               "msg": "pkg install tesseract zbar"}]
    return body


def notify_ask(question: str, options=None, timeout: int = 300, title: str = "TermuxGPT") -> dict:
    options = [str(o)[:30] for o in (options or ["Yes", "No"])][:3]
    if not base.which("termux-notification"):
        return {"ok": False, "errors": [{"code": "E_TERMUX_API", "msg": "pkg install termux-api"}]}
    qid = uuid.uuid4().hex[:8]
    folder = base.ensure_dir(base.root("asks"))
    answer_file = os.path.join(folder, qid + ".txt")
    args = ["termux-notification", "--id", f"ask{qid}", "--title", title, "--content", str(question)[:300],
            "--priority", "high", "--ongoing"]
    for i, opt in enumerate(options, start=1):
        act = f"echo {shlex.quote(opt)} > {shlex.quote(answer_file)}; termux-notification-remove ask{qid}"
        args += [f"--button{i}", opt, f"--button{i}-action", act]
    base.sh(" ".join(shlex.quote(a) for a in args), timeout=15, check_risk=False)
    deadline = time.time() + max(5, min(int(timeout), 3600))
    while time.time() < deadline:
        if os.path.exists(answer_file):
            with open(answer_file, encoding="utf-8", errors="replace") as handle:
                answer = handle.read().strip()
            os.remove(answer_file)
            return {"ok": True, "answer": answer, "summary": f"user chose: {answer}"}
        time.sleep(1)
    base.sh(f"termux-notification-remove ask{qid}", timeout=5, check_risk=False)
    return {"ok": False, "answer": None, "errors": [{"code": "E_TIMEOUT", "msg": "no answer in time"}]}
