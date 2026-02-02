"""CLI handler for the dlm launcher command."""

import sys
import subprocess
from .registry import FEATURES, get_feature
from .tui import LauncherTUI
from .installer import FeatureInstaller


def handle_launcher_command():
    """Main entry point for 'dlm launcher'."""
    while True:
        print("\033[2J\033[H", end="") # Clear screen
        print("DLM Feature Manager")
        print("-------------------\n")
        
        tui = LauncherTUI(FEATURES)
        selected_ids = tui.run()
        
        if not selected_ids:
            print("Launcher exited.")
            break

        # 1. Check for missing dependencies
        to_install = []
        for f_id in selected_ids:
            feature = get_feature(f_id)
            if feature and not feature.is_installed():
                to_install.append(feature)
        
        # 2. Install if needed
        if to_install:
            print("\nMissing dependencies found for:")
            for f in to_install:
                print(f" - {f.name}: {', '.join(f.dependencies)}")
            
            choice = input("\nInstall these features now? [Y/n] ").lower()
            if choice == 'n':
                print("Installation aborted.")
                continue
            else:
                for f in to_install:
                    print(f"\nInstalling {f.name}...")
                    success = FeatureInstaller.install_feature(f.id, registry=sys.modules[__name__], on_progress=print)
                    if not success:
                        print(f"❌ Failed to install {f.name}.")
                        input("Press Enter to continue...")
        
        # 3. Open Feature
        non_core = [fid for fid in selected_ids if fid != "downloader"]
        if non_core:
            target_id = non_core[-1]
            feature = get_feature(target_id)
            
            if feature and feature.is_installed():
                choice = input(f"\nOpen {feature.name} now? [Y/n] ").lower()
                if choice != 'n':
                    print(f"Launching {feature.entry_command}...")
                    try:
                        subprocess.call(feature.entry_command.split())
                    except KeyboardInterrupt:
                        pass
                    except Exception as e:
                        print(f"Error launching {feature.name}: {e}")
                    # After running a feature, we break or continue?
                    # "Open selected feature now? If NO -> return to checklist"
                    # If YES, it runs. After it exits, we return to launcher.
                    input("\nFeature session ended. Press Enter to return to launcher...")
        else:
            # If nothing to install/open, just exit or loop?
            # Checklist style implies we might want to stay in manager.
            pass
