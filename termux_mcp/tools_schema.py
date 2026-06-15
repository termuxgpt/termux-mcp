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
]
