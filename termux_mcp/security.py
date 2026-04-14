import re
from typing import Tuple, Optional

class CommandRiskLevel:
    SAFE = "safe"
    WARNING = "warning"
    DANGEROUS = "dangerous"

DANGEROUS_PATTERNS = [
    r'rm\s+-rf\s+/?\s*$',                    # rm -rf /
    r'rm\s+-rf\s+~',                         # rm -rf ~
    r'rm\s+-rf\s+/\*',                       # rm -rf /*
    r'rm\s+-rf\s+--no-preserve-root',        # Attempts to bypass safety

    r'dd\s+if=',                             # dd if=...
    r'mkfs\.',                               # mkfs.ext4, mkfs.ntfs etc.
    r'format\s+',                            # format commands

    r':\(\)\s*\{\s*:\|\s*&\s*\};:',          # Classic fork bomb

    r'>\s*/dev/',                            # Redirect to /dev/*
    r'echo\s+.*>\s*/dev/',

    r'chmod\s+-R\s+777',                     # chmod -R 777 /
    r'chmod\s+-R\s+000',
    r'chown\s+-R\s+root',

    r'pkg\s+remove\s+termux.*',              # Removing core Termux packages
    r'apt\s+purge\s+-y\s+.*termux',

    r';\s*rm\s+-rf',
    r'&&\s*rm\s+-rf',
    r'\|\s*rm\s+-rf',
]

WARNING_PATTERNS = [
    r'rm\s+-rf',                             # Any rm -rf (even on folders)
    r'rm\s+-r',                              # Recursive remove
    r'>>\s*/dev/null',                       # Overwriting logs aggressively
    r'chmod\s+-R',                           # Recursive chmod
    r'find\s+.*-delete',                     # Find + delete
    r'> /',                                  # Redirect to root
]

def is_dangerous_command(cmd: str) -> Tuple[bool, str, str]:
    cmd_lower = cmd.strip().lower()

    if not cmd_lower or len(cmd_lower) < 3:
        return False, CommandRiskLevel.SAFE, ""

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower):
            return True, CommandRiskLevel.DANGEROUS, f"Blocked dangerous command: {cmd}"

    for pattern in WARNING_PATTERNS:
        if re.search(pattern, cmd_lower):
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
