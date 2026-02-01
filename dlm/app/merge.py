"""
DEPRECATED: Legacy Merge Utility
This module is no longer used in the Workspace-Based Split Design.
All splitting and finalization is now handled via the Workspace system and 'export --final'.
"""

def merge_parts(task_folder):
    raise RuntimeError(
        "The legacy 'merge' logic has been fully removed.\n"
        "Please use 'export --final' from within the workspace task folder."
    )
