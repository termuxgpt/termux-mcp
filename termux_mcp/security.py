import re
from typing import Tuple

class CommandRiskLevel:
    SAFE = "safe"
    WARNING = "warning"
    DANGEROUS = "dangerous"

DANGEROUS_PATTERNS = [
    r'rm\s+-rf\s+/(?:\s|$)',
    r'rm\s+-rf\s+~/?(\s|$)',
    r'rm\s+-rf\s+~/\*',
    r'rm\s+-rf\s+/\*',
    r'rm\s+-rf\s+--no-preserve-root',

    r'dd\s+if=',
    r'mkfs\.',
    r'(?:^|\s)mkfs\.',

    r':\(\)\s*\{\s*:\|\s*&\s*\};:',

    r'>\s*/dev/(?!null)',
    r'echo\s+.*>\s*/dev/(?!null)',

    r'chmod\s+-R\s+777',
    r'chmod\s+-R\s+000',
    r'chown\s+-R\s+root',

    r'pkg\s+remove\s+termux.*',
    r'apt\s+purge\s+-y\s+.*termux',

]

WARNING_PATTERNS = [
    r'rm\s+-rf',
    r'rm\s+-r',
    r'>>\s*/dev/null',
    r'chmod\s+-R',
    r'find\s+.*-delete',
    r'>\s*/(?:bin|boot|etc|lib|opt|root|sbin|srv|sys|usr|var)(?:/|\s)',
    r'>>?[^|;&<>]*\.termux/',
    r'>>?[^|;&<>]*\.bashrc',
    r'>>?[^|;&<>]*\.bash_profile',
    r'>>?[^|;&<>]*\.profile\b',
    r'>>?[^|;&<>]*\.zshrc',
    r'>>?[^|;&<>]*\.ssh/',
    r'>>?[^|;&<>]*\.config/fish/',
    r'pkg\s+(?:uninstall|remove)\b',
    r'apt(?:-get)?\s+(?:remove|purge)\b',
    r'pip\s+(?:uninstall|remove)\b',
]

def is_dangerous_command(cmd: str) -> Tuple[bool, str, str]:
    cmd_lower = cmd.strip().lower()

    if not cmd_lower or len(cmd_lower) < 3:
        return False, CommandRiskLevel.SAFE, ""

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return True, CommandRiskLevel.DANGEROUS, f"Blocked dangerous command: {cmd}"

    for pattern in WARNING_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return False, CommandRiskLevel.WARNING, f"High-risk command detected (confirmation recommended): {cmd}"

    if "sudo" in cmd_lower and "rm" in cmd_lower:
        return False, CommandRiskLevel.WARNING, "sudo + rm combination detected"

    if cmd_lower.startswith(("reboot", "shutdown", "poweroff")):
        return False, CommandRiskLevel.WARNING, "System shutdown/reboot command detected"

    return False, CommandRiskLevel.SAFE, ""


def get_risk_assessment(cmd: str) -> dict:
    blocked, level, message = is_dangerous_command(cmd)
    
    return {
        "command": cmd,
        "risk_level": level,
        "blocked": blocked,
        "message": message or "Command appears safe",
        "requires_confirmation": level == CommandRiskLevel.WARNING
    }
