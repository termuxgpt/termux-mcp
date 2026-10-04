import json
import re
import shutil
import socket
import time
import urllib.request

from . import base

SPEED_URL = "https://speed.cloudflare.com/__down?bytes=5000000"
LATENCY_URL = "https://www.google.com/generate_204"


def _battery() -> dict:
    res = base.sh("termux-battery-status", timeout=15, check_risk=False)
    try:
        return json.loads(res.out)
    except ValueError:
        return {}


def briefing() -> dict:
    lines = []
    b = _battery()
    if b:
        health = str(b.get("health", "")).lower()
        lines.append(f"Battery {b.get('percentage')}% {str(b.get('status', '')).lower()}"
                     + (f", health {health}" if health and health != "good" else "")
                     + (f", {b.get('temperature', 0):.0f}°C" if b.get("temperature") else ""))
    try:
        du = shutil.disk_usage(base.home())
        lines.append(f"Storage {du.free / 1e9:.1f} GB free")
    except OSError:
        pass
    up = base.sh("apt list --upgradable 2>/dev/null | grep -c upgradable", timeout=60, check_risk=False).out.strip()
    if up.isdigit() and int(up):
        lines.append(f"{up} package update(s) available")
    try:
        from .intent_tools import service_table
        down = [r[0] for r in service_table() if not r[3]]
        if down:
            lines.append("Down: " + ", ".join(down))
    except Exception:
        pass
    try:
        from .. import automations
        failed = [j.label for j in (automations.attach_logs(j) for j in automations.parse(automations.read_crontab())[0])
                  if j.last_exit not in (None, 0) and j.last_run and time.time() - j.last_run < 86400]
        if failed:
            lines.append("Failed automations: " + ", ".join(failed))
    except Exception:
        pass
    problems = [l for l in lines if l.startswith(("Down", "Failed"))]
    return {"ok": True, "lines": lines, "problems": len(problems),
            "summary": " · ".join(lines) if lines else "All quiet"}


def _timed(fn):
    t = time.time()
    try:
        fn()
        return round((time.time() - t) * 1000), None
    except Exception as e:
        return None, str(e)[:120]


def net_check(speed: bool = True) -> dict:
    checks = {}
    gw = base.sh("ip route 2>/dev/null | awk '/default/{print $3; exit}'", timeout=5, check_risk=False).out.strip()
    if gw and re.fullmatch(r"[\d.]+", gw):
        ping = base.sh(f"ping -c 3 -W 2 {gw} 2>/dev/null | tail -1", timeout=15, check_risk=False).out
        m = re.search(r"= [\d.]+/([\d.]+)/", ping)
        checks["router_ms"] = float(m.group(1)) if m else None
    dns_ms, dns_err = _timed(lambda: socket.getaddrinfo("example.com", 443))
    checks["dns_ms"] = dns_ms
    web_ms, web_err = _timed(lambda: urllib.request.urlopen(LATENCY_URL, timeout=10).read())
    checks["web_ms"] = web_ms
    mbps = None
    if speed and web_err is None:
        t = time.time()
        try:
            with urllib.request.urlopen(SPEED_URL, timeout=30) as r:
                n = 0
                while time.time() - t < 15:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    n += len(chunk)
            secs = max(time.time() - t, 0.001)
            mbps = round(n * 8 / secs / 1e6, 1)
        except Exception:
            mbps = None
    checks["download_mbps"] = mbps
    wifi = {}
    w = base.sh("termux-wifi-connectioninfo", timeout=10, check_risk=False)
    try:
        wifi = json.loads(w.out)
    except ValueError:
        pass
    rssi = wifi.get("rssi")
    findings = []
    if dns_err and web_err:
        verdict = "offline"
        findings.append("No internet: the phone can't resolve names or reach the web.")
    else:
        verdict = "good"
        if dns_err:
            findings.append("DNS lookups fail; try switching Private DNS off or to dns.google.")
            verdict = "problem"
        elif dns_ms and dns_ms > 300:
            findings.append(f"DNS is slow ({dns_ms} ms); a faster DNS (1.1.1.1 / dns.google) would help.")
            verdict = "slow"
        router = checks.get("router_ms")
        if router is not None and router > 50:
            findings.append(f"Wi-Fi to router is slow ({router:.0f} ms): move closer or switch band.")
            verdict = "slow"
        if isinstance(rssi, (int, float)) and rssi < -75:
            findings.append(f"Weak Wi-Fi signal ({rssi} dBm).")
            verdict = "slow"
        if web_ms and web_ms > 800:
            findings.append(f"Websites respond slowly ({web_ms} ms).")
            verdict = "slow"
        if mbps is not None and mbps < 5:
            findings.append(f"Download speed is low ({mbps} Mbps).")
            verdict = "slow"
    if not findings:
        findings.append("Your connection looks healthy.")
    return {"ok": True, "verdict": verdict, "checks": checks,
            "wifi": {k: wifi.get(k) for k in ("ssid", "rssi", "frequency_mhz", "link_speed_mbps") if k in wifi},
            "findings": findings, "summary": findings[0]}
