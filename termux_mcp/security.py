import re
from typing import Tuple

class CommandRiskLevel:
    SAFE = "safe"
    WARNING = "warning"
    DANGEROUS = "dangerous"

DANGEROUS_PATTERNS = [
    # Dangerous *targets*, matched anywhere in the command rather than only at
    # the end — `echo x && rm -rf /` has to be caught too.
    r'rm\s+-rf\s+/(?:\s|$)',                 # rm -rf /   (also mid-command)
    r'rm\s+-rf\s+~/?(\s|$)',                 # rm -rf ~   or rm -rf ~/
    r'rm\s+-rf\s+~/\*',                      # rm -rf ~/* (all of home)
    r'rm\s+-rf\s+/\*',                       # rm -rf /*
    r'rm\s+-rf\s+--no-preserve-root',        # Attempts to bypass safety

    r'dd\s+if=',                             # dd if=...
    r'mkfs\.',                               # mkfs.ext4, mkfs.ntfs etc.
    r'(?:^|\s)mkfs\.',                       # mkfs.ext4, mkfs.ntfs etc.

    r':\(\)\s*\{\s*:\|\s*&\s*\};:',          # Classic fork bomb

    r'>\s*/dev/(?!null)',                    # Redirect to /dev/* (not /dev/null)
    r'echo\s+.*>\s*/dev/(?!null)',

    r'chmod\s+-R\s+777',                     # chmod -R 777 /
    r'chmod\s+-R\s+000',
    r'chown\s+-R\s+root',

    r'pkg\s+remove\s+termux.*',              # Removing core Termux packages
    r'apt\s+purge\s+-y\s+.*termux',

    # Chained-`rm -rf` patterns (; && |) used to live here. They are gone
    # because they fired on the mere *presence* of a chained rm regardless of
    # target, and so blocked legitimate cleanup: the migrate handler ends with
    # `... && rm -rf $TMPDIR/migrate_*`, which this refused. The target-based
    # patterns above already catch a dangerous rm wherever it appears, and a
    # plain `rm -rf <specific path>` is a WARNING, not a block.
]

WARNING_PATTERNS = [
    r'rm\s+-rf',                             # Any rm -rf (even on folders)
    r'rm\s+-r',                              # Recursive remove
    r'>>\s*/dev/null',                       # Overwriting logs aggressively
    r'chmod\s+-R',                           # Recursive chmod
    r'find\s+.*-delete',                     # Find + delete
    r'>\s*/(?:bin|boot|etc|lib|opt|root|sbin|srv|sys|usr|var)(?:/|\s)',  # Redirect to system dirs
    r'pkg\s+(?:uninstall|remove)\b',         # Removing packages — confirm first
    r'apt(?:-get)?\s+(?:remove|purge)\b',    # apt removals
    r'pip\s+(?:uninstall|remove)\b',         # pip removals
]

def is_dangerous_command(cmd: str) -> Tuple[bool, str, str]:
    cmd_lower = cmd.strip().lower()

    if not cmd_lower or len(cmd_lower) < 3:
        return False, CommandRiskLevel.SAFE, ""

    # IGNORECASE is not optional here. The command is lowercased above, but
    # several patterns are written with uppercase flags — `chmod\s+-R\s+777`,
    # `chown\s+-R\s+root`, `chmod\s+-R`. Matching a lowercased command against
    # an uppercase pattern case-sensitively meant those four could never fire:
    # `chmod -R 777 /` came back SAFE.
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
