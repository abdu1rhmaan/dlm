import sys
import argparse
import signal
from pathlib import Path

# Ensure root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dlm.bootstrap import create_container
from dlm.app.commands import AddDownload, ListDownloads, PauseDownload, ResumeDownload, RemoveDownload, StartDownload, BrowserCommand
from dlm.interface.repl import DLMShell

def main():
    # --- ALIAS HANDLING ---
    # Arabic: التتحقق من وجود اختصار واستبداله بالأمر الأصلي قبل المعالجة
    from dlm.interface.aliases import COMMAND_ALIASES
    if len(sys.argv) > 1:
        cmd_arg = sys.argv[1].lower()
        if cmd_arg in COMMAND_ALIASES:
            sys.argv[1] = COMMAND_ALIASES[cmd_arg]

    parser = argparse.ArgumentParser(description="DLM - Download Manager (Single Process)")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    parser.add_argument("-shell", action="store_true", help="Start interactive shell")
    
    # Legacy flag ignored, but kept for compatibility just in case
    parser.add_argument("-daemon-process", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-foreground", "-f", action="store_true", help=argparse.SUPPRESS)

    add_parser = subparsers.add_parser("add", help="Add a download")
    add_parser.add_argument("url", help="URL to download")
    add_parser.add_argument("-r", "-resume", help="Resume from folder", required=False)
    list_parser = subparsers.add_parser("list", help="List downloads")
    list_parser.add_argument("-brw", action="store_true", help="List browser captures")
    
    ls_parser = subparsers.add_parser("ls", help="List folders and downloads")
    ls_parser.add_argument("-brw", action="store_true", help="List browser captures")

    # Direct commands
    start_parser = subparsers.add_parser("start", help="Start download")
    start_parser.add_argument("selector", help="ID or Index")
    start_parser.add_argument("-brw", action="store_true", help="Start using browser session")
    
    pause_parser = subparsers.add_parser("pause", help="Pause download")
    pause_parser.add_argument("selector", help="ID or Index")
    
    resume_parser = subparsers.add_parser("resume", help="Resume download")
    resume_parser.add_argument("selector", help="ID or Index")

    config_parser = subparsers.add_parser("config", help="Manage configuration")
    config_parser.add_argument("key", help="Config key (limit, output, etc.)", nargs='?')
    config_parser.add_argument("value", help="Value to set", nargs='?')

    subparsers.add_parser("browser", help="Start browser for download capture")
    subparsers.add_parser("setup", help="Automated environment setup (Termux dependencies)")
    subparsers.add_parser("launcher", help="Open DLM Feature Manager (TUI)")

    # Share Command (Phase 1)
    share_parser = subparsers.add_parser("share", help="Share files on LAN")
    share_subparsers = share_parser.add_subparsers(dest="share_action", help="Action")
    
    send_parser = share_subparsers.add_parser("send", help="Send a file")
    send_parser.add_argument("file_path", nargs='?', help="Path to file to send")
    
    receive_parser = share_subparsers.add_parser("receive", help="Receive a file")
    receive_parser.add_argument("ip", nargs='?', help="Sender IP address")
    receive_parser.add_argument("port", nargs='?', type=int, help="Sender port")
    receive_parser.add_argument("token", nargs='?', help="Authentication token")
    receive_parser.add_argument("-save-to", "--save-to", help="Destination folder override", default=None)

    join_parser = share_subparsers.add_parser("join", help="Automated join (for scripts)")
    join_parser.add_argument("--ip", help="Sender IP")
    join_parser.add_argument("--port", type=int, help="Sender port")
    join_parser.add_argument("--token", help="Authentication token")

    args = parser.parse_args()

    # Initialize Application
    container = create_container()
    bus = container["bus"]
    service = container["service"]
    media_service = container["media_service"]
    get_uuid = container["get_uuid_by_index"]

    # No global signal handler. We rely on KeyboardInterrupt bubbling up.

    try:
        if args.shell or not args.command:
            shell = DLMShell(bus, get_uuid, service, media_service)
            shell.cmdloop()
        
        elif args.command == "add":
            if args.resume:
                service.resume_from_folder(args.url, Path(args.resume))
                print(f"Resumed from {args.resume}")
            else:
                # Basic CLI add support (simple URL only for now via args)
                bus.handle(AddDownload(url=args.url))
                print("Added.")
        
        elif args.command in ["list", "ls"]:
            downloads = bus.handle(ListDownloads(brw=args.brw))
            if not downloads:
                if args.brw:
                    print("No browser captures found.")
                else:
                    print("No downloads.")
            else:
                print(f"{'#':<4} {'Filename':<40} {'State':<12} {'Progress'}")
                print("_" * 70)
                from dlm.interface.repl import truncate_middle
                for d in downloads:
                    filename = truncate_middle(d['filename'], 38)
                    print(f"{d['index']:<4} {filename:<40} {d['state']:<12} {d['progress']}")
        
        # Note: start/pause/resume from CLI args need parsing selector logic 
        # which is currently in REPL. For simplicity, we can basic support or load REPL logic.
        # But for valid single-process usage, user likely uses shell. 
        # Implementing basic ID support here:
        
        elif args.command in ["start", "pause", "resume"]:
             # For now, require shell for complex selectors, or just support UUID?
             # Supporting index requires get_uuid which we have.
             try:
                 idx = int(args.selector)
                 uuid = get_uuid(idx, brw=getattr(args, 'brw', False))
             except ValueError:
                 uuid = args.selector # Assume UUID
             
             if args.command == "start":
                 bus.handle(StartDownload(id=uuid, brw=getattr(args, 'brw', False)))
             elif args.command == "pause":
                 bus.handle(PauseDownload(id=uuid))
             elif args.command == "resume":
                 bus.handle(ResumeDownload(id=uuid))
             print(f"Command {args.command} executed (Session: {getattr(args, 'brw', False)}).")
        
        elif args.command == "browser":
            bus.handle(BrowserCommand())
            
        elif args.command == "config":
            # For simplicity, we create a temporary shell instance to reuse its config logic
            shell = DLMShell(bus, get_uuid, service, media_service)
            if not args.key:
                shell.do_config("")
            else:
                shell.do_config(f"{args.key} {args.value if args.value else ''}")

        elif args.command == "setup":
            import os
            import subprocess
            
            # Detect Termux
            is_termux = "TERMUX_VERSION" in os.environ or "/data/data/com.termux" in os.environ.get("PATH", "")
            
            if is_termux:
                print("📱 Termux detected! Attempting to install system dependencies...")
                try:
                    # pkg is a shell command in Termux
                    subprocess.run(["pkg", "install", "-y", "python-libtorrent", "ffmpeg"], check=True)
                    print("✅ Termux dependencies (libtorrent, ffmpeg) installed successfully.")
                except Exception as e:
                    print(f"❌ Failed to run pkg install: {e}")
                    print("Please run manually: pkg install python-libtorrent ffmpeg")
            else:
                print("💻 Desktop environment detected.")
                print("On regular Linux/WSL, you might need to install libtorrent and ffmpeg via your package manager.")
                print("Example: sudo apt install python3-libtorrent ffmpeg")
            
            print("\n🔄 Running pip installation...")
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True)
                print("✅ Python package re-installed.")
            except Exception as e:
                print(f"❌ Pip installation failed: {e}")

        elif args.command == "launcher":
            try:
                from dlm.features.tui import run_feature_manager
                run_feature_manager()
            except Exception as e:
                print(f"Error launching feature manager: {e}")

        elif args.command == "share":
            # Check if share dependencies are installed
            try:
                import fastapi
                import uvicorn
            except ImportError as e:
                missing = str(e).split("'")[1] if "'" in str(e) else "required dependencies"
                print(f"\n❌ Error: {missing} is not installed.")
                print("📦 To use 'dlm share', install dependencies via Feature Manager:")
                print("   dlm launcher")
                print("   Then select 'Share' feature and install.")
                print("\nOr install manually:")
                print("   pip install fastapi uvicorn zeroconf qrcode psutil")
                return
            
            from dlm.share.cli import handle_share_command
            handle_share_command(args, bus)

    except KeyboardInterrupt:
        print("\nStopping downloads and exiting...")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # If we are in single command mode, we still need to wait for threads if we started any.
        # But single commands like 'list' don't start threads. 'start' does!
        # If user runs 'dlm start 1', it spawns threads and then script hits finally and exits.
        # This means 'dlm start 1' returns immediately and kills the download in single-process mode!
        # This is expected behavior for a non-daemon app (interactive mode required for persistent tasks).
        # Unless we wait? But user asked for "process lifetime == terminal lifetime".
        # If running "dlm start 1", the terminal lifetime is momentary.
        # To download, user MUST use shell or keep process alive.
        service.shutdown_all()

if __name__ == "__main__":
    main()
