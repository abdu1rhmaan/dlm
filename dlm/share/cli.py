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
    
    # We need to re-parse or rely on main.py to pass specific args?
    # main.py will likely pass the whole 'args' object.
    # We expect 'share_action' (send/receive) and related.
    
    if args.share_action == 'send':
        _do_send()
    elif args.share_action == 'receive':
        _do_receive(args, bus)
    else:
        print("Usage: dlm share [send|receive]")

def _do_send():
    print("\n--- NEW SHARE SESSION ---")
    
    # 1. Pick File
    file_path = pick_file()
    if not file_path:
        return # User aborted or failed
        
    try:
        entry = FileEntry.from_path(file_path)
    except Exception as e:
        print(f"Error preparing file: {e}")
        return

    # 2. Start Server
    server = ShareServer(entry)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nStopped.")

def _do_receive(args, bus):
    print("\n--- RECEIVE FILE ---")
    
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

    # 2. Start Client
    client = ShareClient(bus)
    save_to = getattr(args, 'save_to', None)
    client.connect(ip, port, token, save_to=save_to)
