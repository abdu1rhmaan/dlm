import argparse
import sys
from .models import FileEntry
from .picker import pick_file
from .server import ShareServer
from .client import ShareClient

def _check_termux_wakelock():
    """Check if running in Termux and if wakelock might be needed."""
    import os
    import subprocess
    
    # Detect Termux
    is_termux = (
        "TERMUX_VERSION" in os.environ or 
        "/data/data/com.termux" in os.environ.get("PATH", "")
    )
    
    if not is_termux:
        return True  # Not Termux, no warning needed
    
    # Check if termux-wake-lock command exists
    try:
        result = subprocess.run(
            ["which", "termux-wake-lock"],
            capture_output=True,
            text=True,
            timeout=2
        )
        # If command exists, assume user knows about it
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        # Command not found or error
        return False

def _warn_termux_wakelock():
    """Display warning about wakelock if needed."""
    if not _check_termux_wakelock():
        print("\n⚠️  \033[1;33mWARNING: Running on Termux\033[0m")
        print("   Android may throttle network during transfer.")
        print("   \033[1;32mRecommended:\033[0m Run 'termux-wake-lock' before sharing")
        print("   (Install with: pkg install termux-api)\n")


def handle_share_command(args, bus):
    """
    Dispatcher for 'share' subcommand.
    args: parsed arguments from main parser.
    """
    if getattr(args, 'share_action', None) == 'send':
        file_path = getattr(args, 'file_path', None)
        _do_send(bus, file_path=file_path)
    elif getattr(args, 'share_action', None) == 'receive':
        _do_receive(args, bus)
    else:
        print("Usage: dlm share send [file-path] | dlm share receive [ip] [port] [token]")
def _do_send(bus, file_path=None):
    import time
    
    # Check Termux wakelock before starting
    _warn_termux_wakelock()
    
    # 1. Pick File (or use provided path)
    if not file_path:
        file_path = pick_file()
        if not file_path:
            return # User aborted or failed
    
    # Validate file exists
    from pathlib import Path
    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        return
        
    try:
        entry = FileEntry.from_path(file_path)
    except Exception as e:
        print(f"Error preparing file: {e}")
        return

    # 2. Start Server (Background, No DB Task)
    # We do NOT register an external task.
    server = ShareServer(entry, bus=bus, upload_task_id=None)
    info = server.prepare()
    
    import threading
    t = threading.Thread(target=server.run_server, daemon=True)
    t.start()
    
    # 3. TUI Monitor (Standalone Loop)
    from dlm.interface.tui import TUI
    tui = TUI(bus)
    
    def get_header():
        clients = list(getattr(server, 'connected_clients', []))
        status_line = f"Clients: \033[1;32m{len(clients)} Connected\033[0m" if clients else "Clients: \033[1;31mWaiting...\033[0m"
        return [
            f"\033[1;32m[ SHARE SENDER ]\033[0m",
            f"File:  {entry.name}",
            f"Size:  {server._format_size(entry.size_bytes)}",
            f"IP:    \033[1;33m{info['ip']}\033[0m",
            f"Port:  \033[1;33m{info['port']}\033[0m",
            f"Token: \033[1;33m{info['token']}\033[0m",
            status_line,
            ""
        ]

    try:
        # Clear screen
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        
        while True:
            # Poll Server State
            bytes_sent = getattr(server, '_bytes_sent', 0)
            speed = getattr(server, 'current_speed', 0.0)
            total = entry.size_bytes
            
            # Dynamic State
            state = "WAITING"
            if len(server.connected_clients) > 0:
                if bytes_sent >= total:
                    state = "COMPLETED"
                else:
                    state = "DOWNLOADING"
            
            # Calculate Progress
            pct = (bytes_sent / total * 100) if total > 0 else 0.0
            
            # Ephemeral Task Dict for TUI Renderer
            fake_task = {
                'index': 1,
                'id': 'share-sender',
                'filename': entry.name,
                'state': state,
                'progress': f"{pct:.1f}%",
                'downloaded': bytes_sent,
                'total': total,
                'speed': speed
            }
            
            # Max name length calculation
            max_len = len(entry.name)
            
            # Render
            header = get_header()
            tui._render_active_tasks([fake_task], max_len, custom_header=header)
            
            if state == "COMPLETED":
                 # Wait a moment then exit? Or keep showing?
                 # User usually wants to know it sent successfully.
                 # Let's keep showing "COMPLETED" until Ctrl+C
                 pass

            time.sleep(0.2)
            
    except KeyboardInterrupt:
        # Clear screen on exit
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

def _do_receive(args, bus):
    # Check Termux wakelock before starting
    _warn_termux_wakelock()
    
    # 1. Get details from args or prompt
    try:
        # Use provided arguments or prompt
        ip = args.ip if hasattr(args, 'ip') and args.ip else input("Sender IP: ").strip()
        if not ip: return
        
        if hasattr(args, 'port') and args.port:
            port = args.port
        else:
            port_str = input("Sender Port: ").strip()
            if not port_str: return
            port = int(port_str)
        
        token = args.token if hasattr(args, 'token') and args.token else input("Token (XXX-XXX): ").strip()
        if not token: return
    except KeyboardInterrupt:
        return
    except ValueError:
        print("Invalid port number.")
        return

    # 2. Start Client & Download
    client = ShareClient(bus)
    save_to = getattr(args, 'save_to', None)
    
    dl_id = client.connect(ip, port, token, save_to=save_to)
    
    if dl_id:
        # 3. TUI Monitor
        from dlm.interface.tui import TUI
        tui = TUI(bus)
        
        header = [
            f"\033[1;34m[ SHARE RECEIVER ]\033[0m",
            f"Source: {ip}:{port}",
            ""
        ]
        
        try:
            tui.monitor_task(dl_id, custom_header=header)
        except KeyboardInterrupt:
            pass
