import argparse
import sys
from .models import FileEntry
from .picker import pick_file
from .server import ShareServer
from .client import ShareClient

def handle_share_command(args, bus):
    """
    Dispatcher for 'share' subcommand.
    args: parsed arguments from main parser (need to exist).
    """
    if getattr(args, 'share_action', None) == 'send':
        _do_send(bus)
    elif getattr(args, 'share_action', None) == 'receive':
        _do_receive(args, bus)
    else:
        print("Usage: share -send | share -rec")
def _do_send(bus):
    # 1. Pick File
    file_path = pick_file()
    if not file_path:
        return # User aborted or failed
        
    try:
        entry = FileEntry.from_path(file_path)
    except Exception as e:
        print(f"Error preparing file: {e}")
        return

    # 2. Register Upload Task
    from dlm.app.commands import RegisterExternalTask
    upload_id = bus.handle(RegisterExternalTask(
        filename=entry.name,
        total_size=entry.size_bytes,
        source="upload",
        state="WAITING"
    ))

    # 3. Start Server (Background)
    server = ShareServer(entry, bus=bus, upload_task_id=upload_id)
    info = server.prepare()
    
    import threading
    t = threading.Thread(target=server.run_server, daemon=True)
    t.start()
    
    # 4. TUI Monitor (Blocking)
    from dlm.interface.tui import TUI
    tui = TUI(bus)
    
    # Header Info
    header = [
        f"\033[1;32m[ SHARE SENDER ]\033[0m",
        f"File:  {entry.name}",
        f"Size:  {server._format_size(entry.size_bytes)}",
        f"IP:    \033[1;33m{info['ip']}\033[0m",
        f"Port:  \033[1;33m{info['port']}\033[0m",
        f"Token: \033[1;33m{info['token']}\033[0m",
        ""
    ]
    
    try:
        tui.monitor_task(upload_id, custom_header=header)
    except KeyboardInterrupt:
        pass
    
    # Cleanup? Thread is daemon, will die on exit. 
    # But usually good to signal stop.
    # ShareServer doesn't have stop() yet exposed cleanly for thread. 
    # Uvicorn handles signal.
    # For now, daemon thread exit is fine.

def _do_receive(args, bus):
    # 1. Ask for details
    try:
        ip = input("Sender IP: ").strip()
        if not ip: return
        
        port_str = input("Sender Port: ").strip()
        if not port_str: return
        port = int(port_str)
        
        token = input("Token (XXX-XXX): ").strip()
        if not token: return
    except KeyboardInterrupt:
        return
    except ValueError:
        print("Invalid number.")
        return

    # 2. Start Client & Download
    client = ShareClient(bus)
    save_to = getattr(args, 'save_to', None)
    
    # Connect and Start
    # Note: connect() might block on Request? No, 'connect' does Auth+List, then AddDownload+Start.
    # It returns download_id currently (after my fix).
    # BUT, 'connect' has user input "Download this file? [Y/n]".
    # We should probably respect that OR auto-yes if "Immediate Transfer" logic implies it?
    # Requirement: "dlm share receive MUST start download immediately... DO NOT require user to run go".
    # Connect confirms with user? 
    # "Phase 1 improvements... dlm share receive starts download immediately".
    # I should probably remove the "Download this file?" prompt in client.py OR rely on TUI.
    # client.py current logic: asks input.
    # I need to modify client.py to skip input?
    # Or just let it prompt (it's foreground).
    # Wait, "Enter a locked display mode... no prompt input".
    # So I MUST remove the prompt from client.py!
    
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
