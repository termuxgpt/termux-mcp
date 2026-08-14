OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": "Execute a shell command in Termux with real-time streaming output. Maintains persistent cd state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get live system stats: CPU%, RAM, disk, temperature, uptime as JSON.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "health",
            "description": "Run a full diagnostic: core packages, Termux:API, storage, network, permissions status.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "List running processes sorted by CPU usage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max processes to show", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_kill",
            "description": "Terminate a process by PID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "Process ID to kill"},
                    "signal": {"type": "integer", "description": "Signal number", "default": 15}
                },
                "required": ["pid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List directory contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path", "default": "."},
                    "detailed": {"type": "boolean", "description": "Show detailed listing", "default": False}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file (first 500 lines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "Create a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete",
            "description": "Delete a file or directory. Requires confirmation for recursive deletes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to delete"},
                    "recursive": {"type": "boolean", "description": "Delete recursively", "default": False},
                    "confirmed": {"type": "boolean", "description": "Confirm deletion", "default": False}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Find files by name pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to search in", "default": "."},
                    "pattern": {"type": "string", "description": "File name pattern", "default": "*"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "battery",
            "description": "Get battery status: percentage, health, temperature, charging state.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "location",
            "description": "Get GPS coordinates, altitude, accuracy, speed, bearing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["gps", "network"], "description": "Location provider", "default": "gps"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wifi_info",
            "description": "Get WiFi connection details: SSID, signal strength, IP address.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output": {"type": "string", "description": "Output file path", "default": "screenshot.png"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camera_photo",
            "description": "Take a photo with the camera.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "integer", "description": "Camera ID (0=back, 1=front)", "default": 0},
                    "output": {"type": "string", "description": "Output file path", "default": "photo.jpg"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notify",
            "description": "Send an Android notification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Notification title"},
                    "content": {"type": "string", "description": "Notification body"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sms_send",
            "description": "Send an SMS message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "Phone number"},
                    "text": {"type": "string", "description": "Message text"}
                },
                "required": ["number", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sms_inbox",
            "description": "Read SMS inbox messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max messages", "default": 10}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tts_speak",
            "description": "Convert text to speech and play it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "backup",
            "description": "Create a tar.gz backup of home directory, packages, or configs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["home", "packages", "configs"], "description": "What to backup", "default": "home"},
                    "output": {"type": "string", "description": "Output file path"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restore",
            "description": "Restore from a backup file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Backup file path"},
                    "target": {"type": "string", "enum": ["home", "packages", "configs"], "description": "What to restore", "default": "home"}
                },
                "required": ["file"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_add",
            "description": "Add a cron job. Schedule format: 0 3 * * * for daily at 3am.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule": {"type": "string", "description": "Cron schedule (5 fields)"},
                    "command": {"type": "string", "description": "Command to run"},
                    "label": {"type": "string", "description": "Label for the job", "default": "task"}
                },
                "required": ["schedule", "command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_list",
            "description": "List all cron jobs.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cron_remove",
            "description": "Remove cron jobs matching a label, or all if no label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Label to match for removal"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_sync",
            "description": "Create or restore cloud backups with rclone integration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["backup", "restore", "list"], "description": "Action to perform"},
                    "target": {"type": "string", "enum": ["home", "packages", "configs"], "default": "home"},
                    "output": {"type": "string", "description": "Output file path for backup"},
                    "file": {"type": "string", "description": "File to restore from"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diff",
            "description": "Show diff between two files, or file info for a single file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Primary file path"},
                    "file2": {"type": "string", "description": "Second file to diff against"}
                },
                "required": ["file"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the device browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download",
            "description": "Download a file from a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Download URL"},
                    "description": {"type": "string", "description": "File description"},
                    "title": {"type": "string", "description": "Notification title"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "public_ip",
            "description": "Get the device's public IP address.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "speedtest",
            "description": "Run an internet speed test.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "smart_install",
            "description": "Intelligently install packages with conflict detection and pre-flight checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {"type": "string", "description": "Space-separated package names"},
                    "manager": {"type": "string", "enum": ["auto", "pkg", "pip", "npm"], "default": "auto"},
                    "dry_run": {"type": "boolean", "description": "Preview only, don't install", "default": False}
                },
                "required": ["packages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "qrcode",
            "description": "Generate a QR code image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Content to encode"},
                    "output": {"type": "string", "description": "Output image path", "default": "qrcode.png"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "image_process",
            "description": "Process images: resize, crop, rotate via ImageMagick.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["info", "resize", "crop", "rotate"]},
                    "input": {"type": "string", "description": "Input image path"},
                    "output": {"type": "string", "description": "Output image path"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"}
                },
                "required": ["action", "input", "output"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel",
            "description": "Cancel the currently running command.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camera",
            "description": "Take a photo with the device camera.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_id": {"type": "integer", "description": "Camera ID (0=back, 1=front)", "default": 0},
                    "output": {"type": "string", "description": "Output file path", "default": "/sdcard/DCIM/termux_photo.jpg"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_get",
            "description": "Read the current clipboard content.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_set",
            "description": "Set the clipboard content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to copy to clipboard"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "toast",
            "description": "Show a brief Android toast message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Toast message text"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "share",
            "description": "Share text or a file via the Android share sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to share"},
                    "file": {"type": "string", "description": "File path to share"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sms",
            "description": "Send an SMS message (alias of sms_send).",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "Phone number"},
                    "text": {"type": "string", "description": "Message text"}
                },
                "required": ["number", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tts",
            "description": "Speak text aloud via text-to-speech (alias of tts_speak).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wifi",
            "description": "Get WiFi connection details (alias of wifi_info).",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose",
            "description": "Diagnose missing tools or environment issues (python, git, storage).",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["python", "git", "storage", "all"], "default": "all"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "optimize",
            "description": "Show performance overview: memory, disk, top processes.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ocr",
            "description": "Extract text from an image using tesseract.",
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Image file path"}
                },
                "required": ["input"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_pr",
            "description": "GitHub PR operations: list, view, diff, merge, approve, create, status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "status", "view", "diff", "merge", "approve", "create"]},
                    "repo": {"type": "string", "description": "GitHub repo (owner/name)"},
                    "number": {"type": "string", "description": "PR number"},
                    "state": {"type": "string", "description": "PR state filter", "default": "open"},
                    "limit": {"type": "integer", "description": "Max PRs", "default": 10},
                    "title": {"type": "string", "description": "PR title for create"},
                    "body": {"type": "string", "description": "PR body for create"},
                    "base": {"type": "string", "description": "Base branch", "default": "main"},
                    "method": {"type": "string", "description": "Merge method", "default": "merge"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recipe_list",
            "description": "List saved task recipes.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recipe_run",
            "description": "Run a saved recipe by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe": {"type": "string", "description": "Recipe id"}
                },
                "required": ["recipe"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recipe_save",
            "description": "Save a multi-step task as a reusable recipe.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe": {"type": "string", "description": "Recipe id"},
                    "name": {"type": "string", "description": "Display name"},
                    "desc": {"type": "string", "description": "Description"},
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "Shell commands"}
                },
                "required": ["recipe", "name", "steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context",
            "description": "Read the saved environment context.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "context_save",
            "description": "Snapshot environment context (hostname, packages, disk, cwd).",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_start",
            "description": "Start a persistent tmux session for stateful commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Session name", "default": "termux-mcp"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_run",
            "description": "Run a command inside a persistent tmux session and stream its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Command to run"},
                    "session": {"type": "string", "description": "Session name"}
                },
                "required": ["cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_list",
            "description": "List active tmux sessions.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_kill",
            "description": "Kill a tmux session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "session_poll",
            "description": "Get new output from a running tmux session since the last poll (or since session_run). Returns output + whether the session is still alive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session name"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "history",
            "description": "List previous task history entries.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "history_save",
            "description": "Save a task entry to history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rawInput": {"type": "string", "description": "Original user input"},
                    "output": {"type": "string", "description": "Task output"},
                    "success": {"type": "boolean", "description": "Whether task succeeded"},
                    "ranCommand": {"type": "string", "description": "Command that ran"},
                    "traces": {"type": "array", "description": "Agent trace steps"}
                },
                "required": ["rawInput", "output"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "history_clear",
            "description": "Clear all task history.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]

# ── Tool categories (used by the /catalog endpoint) ──────────────────────

TOOL_CATEGORIES = {
    "run": "shell", "ls": "shell", "read": "shell", "write": "shell",
    "mkdir": "shell", "delete": "shell", "search": "shell", "cancel": "shell",
    "screenshot": "device", "camera": "device", "camera_photo": "device",
    "battery": "device", "location": "device", "wifi": "device",
    "wifi_info": "device", "clipboard_get": "device", "clipboard_set": "device",
    "notify": "communicate", "sms": "communicate", "sms_send": "communicate",
    "sms_inbox": "communicate", "tts": "communicate", "tts_speak": "communicate",
    "toast": "communicate", "share": "communicate", "call": "communicate",
    "contacts": "communicate",
    "smart_install": "smart", "diagnose": "smart", "optimize": "smart",
    "download": "network", "public_ip": "network", "weather": "network",
    "speedtest": "network", "open_url": "network",
    "qrcode": "media", "image_process": "media", "ocr": "media",
    "system_info": "monitor", "process_list": "monitor",
    "process_kill": "monitor", "health": "monitor",
    "cron_add": "cron", "cron_list": "cron", "cron_remove": "cron",
    "backup": "backup", "restore": "backup", "cloud_sync": "backup",
    "git_pr": "git", "diff": "git",
    "recipe_list": "recipes", "recipe_run": "recipes", "recipe_save": "recipes",
    "context": "context", "context_save": "context",
    "session_start": "session", "session_run": "session",
    "session_list": "session", "session_kill": "session",
    "session_poll": "session",
    "history": "history", "history_save": "history", "history_clear": "history",
}


def build_catalog() -> list:
    """Compact per-tool catalog for LLM meta-tool routing.

    Each entry: {name, desc, params, category} where `params` is a short
    "key:type, key2:type" summary — small enough to embed in a system prompt
    or a use_tool meta-tool description without blowing the token budget.
    """
    catalog = []
    for entry in OPENAI_TOOLS:
        fn = entry.get("function", {})
        name = fn.get("name", "")
        props = (fn.get("parameters") or {}).get("properties", {})
        param_summary = ", ".join(
            f"{k}:{_type_label(v)}" for k, v in props.items()
        )
        catalog.append({
            "name": name,
            "desc": fn.get("description", ""),
            "params": param_summary,
            "category": TOOL_CATEGORIES.get(name, "other"),
        })
    return catalog


def _type_label(spec: dict) -> str:
    t = spec.get("type", "str")
    enum = spec.get("enum")
    if enum:
        return "|".join(enum)
    return "int" if t == "integer" else "bool" if t == "boolean" else "str"
