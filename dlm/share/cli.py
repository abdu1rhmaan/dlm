from .app import DlmShareApp

def main(room_name="default", add_file=None, add_folder=None):
    """
    Main entry point for the dlm share TUI.
    
    Args:
        room_name (str): The name of the room to join/create.
        add_file (str, optional): Path to a file to add immediately.
        add_folder (str, optional): Path to a folder to add immediately.
    """
    # Note: We are ignoring the args for Phase 1 as per instructions (Static TUI only),
    # but we accept them to match the signature expected by repl.py
    app = DlmShareApp()
    app.run()

if __name__ == "__main__":
    main()
