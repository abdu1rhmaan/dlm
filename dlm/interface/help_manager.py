"""Extensible help manager for DLM commands."""

HELP_DATA = {
    "add": {
        "summary": "Add a download to the queue.",
        "usage": "add <url> [flags] [--item <selector>:<options>]...",
        "flags": {
            "--audio": "Download as audio only.",
            "--video": "Download as video.",
            "--quality <Q>": "Specify quality (e.g., 1080p, 720p).",
            "--cut [range]": "Cut range (e.g., 00:01:00-00:02:30). If no range, toggles picker.",
            "--vocals": "Separate vocals after download.",
            "--vocals-gpu": "Use GPU for vocal separation.",
            "--output <path>": "Output folder template.",
            "--rename <name>": "Filename template.",
            "--referer <url>": "Set HTTP referer.",
            "--limit <N>": "Parallel connection limit for this task.",
            "--only <idx>": "Process only specific items from a playlist."
        },
        "examples": [
            "add https://youtu.be/... -q 1080p",
            "add https://youtu.be/... --audio --cut 00:00:10-00:00:40",
            "add https://spotify.com/... (playlist support)"
        ]
    },
    "list": {
        "summary": "List all downloads and their status.",
        "usage": "list",
        "description": "Shows a table of downloads with their status [↓] (Downloading), [✓] (Done), [✗] (Failed), etc."
    },
    "start": {
        "summary": "Start queued downloads.",
        "usage": "start <selector>",
        "examples": [
            "start 1",
            "start 1..5",
            "start *"
        ]
    },
    "pause": {
        "summary": "Pause running downloads.",
        "usage": "pause <selector>",
        "examples": ["pause 1", "pause *"]
    },
    "resume": {
        "summary": "Resume paused or failed downloads.",
        "usage": "resume <selector>",
        "examples": ["resume 1", "resume *"]
    },
    "remove": {
        "summary": "Remove downloads from the queue.",
        "usage": "remove <selector>",
        "description": "Removes the task from the database. Does not delete downloaded files."
    },
    "split": {
        "summary": "Split a download into parts for distributed downloading.",
        "usage": "split <id> --parts <N> --users <u1> <u2> ...",
        "examples": [
            "split 1 --parts 4 --users Alice Bob",
            "split 1 --parts 8 --users 2 (Auto-assigns to user_1, user_2)"
        ]
    },
    "import": {
        "summary": "Import a partial download from a manifest.",
        "usage": "import [path] [--parts <selector>] [--separate]",
        "flags": {
            "--parts": "Import only specific parts (e.g., --parts 1,3..5).",
            "--separate": "Add each part as a separate task in the queue."
        },
        "description": "If path is missing, a file picker will open."
    },
    "merge": {
        "summary": "Merge downloaded parts into the final file.",
        "usage": "merge <task_folder>",
        "description": "Assembles parts from a split download. Deletes parts after successful merge."
    },

    "vocals": {
        "summary": "Direct vocal separation for local files.",
        "usage": "vocals [path] [--gpu]",
        "description": "If path is missing, a file picker will open."
    },
    "config": {
        "summary": "Manage configuration settings.",
        "usage": "config <key> [value]",
        "keys": {
            "limit": "Simultaneous download limit.",
            "spotify": "Interactive Spotify setup."
        },
        "examples": [
            "config limit 2",
            "config spotify"
        ]
    },
    "error": {
        "summary": "Show error details for a failed task.",
        "usage": "error <index>",
        "description": "Displays the full traceback or error message for a specific task."
    },
    "cls": {
        "summary": "Clear the terminal screen.",
        "usage": "cls"
    },
    "exit": {
        "summary": "Exit the application.",
        "usage": "exit"
    }
}

def get_detailed_help(command):
    """Return formatted help string for a command."""
    data = HELP_DATA.get(command.lower())
    if not data:
        return f"No detailed help available for '{command}'."

    output = []
    output.append(f"\n{command.upper()} - {data['summary']}")
    output.append("-" * (len(command) + 3 + len(data['summary'])))
    output.append(f"Usage: {data['usage']}")
    
    if "description" in data:
        output.append(f"\nDescription:\n  {data['description']}")
        
    if "flags" in data:
        output.append("\nFlags:")
        for flag, desc in data['flags'].items():
            output.append(f"  {flag:<15} {desc}")
            
    if "keys" in data:
        output.append("\nKeys:")
        for key, desc in data['keys'].items():
            output.append(f"  {key:<15} {desc}")
            
    if "examples" in data:
        output.append("\nExamples:")
        for ex in data['examples']:
            output.append(f"  {ex}")
            
    return "\n".join(output) + "\n"
