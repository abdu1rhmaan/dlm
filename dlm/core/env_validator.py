"""
Environment validation for vocals feature.
Ensures correct versions of NumPy, torchaudio, and demucs are installed.
"""

def validate_vocals_environment():
    """
    Validate environment for vocals feature.
    
    Returns:
        tuple: (errors: list[str], warnings: list[str])
    """
    errors = []
    warnings = []
    
    # Check NumPy version
    try:
        import numpy as np
        major_version = int(np.__version__.split('.')[0])
        if major_version >= 2:
            errors.append(
                "NumPy >= 2 is not supported for vocals (breaks diffq dependency). "
                "Please reinstall: pip install 'numpy<2'"
            )
    except ImportError:
        errors.append("NumPy not installed. Required for vocals feature.")
    except (ValueError, IndexError):
        warnings.append(f"Could not parse NumPy version: {np.__version__}")
    
    # Check torchaudio version
    try:
        import torchaudio
        if not torchaudio.__version__.startswith("2.1"):
            warnings.append(
                f"torchaudio {torchaudio.__version__} is not officially supported. "
                f"Recommended version: 2.1.0 (versions >= 2.2 force torchcodec usage)"
            )
    except ImportError:
        errors.append("torchaudio not installed. Required for vocals feature.")
    
    # Check for torchcodec (should NOT be present)
    try:
        import torchcodec
        warnings.append(
            "torchcodec is installed but not used by DLM. "
            "Consider uninstalling to avoid conflicts: pip uninstall torchcodec"
        )
    except ImportError:
        pass  # Good - torchcodec not present
    
    # Check soundfile
    try:
        import soundfile
    except ImportError:
        errors.append("soundfile not installed. Required backend for torchaudio.")
    
    return errors, warnings


def print_validation_results(errors, warnings):
    """Print validation results in a user-friendly format."""
    if errors:
        print("\n❌ Environment Validation Errors:")
        for error in errors:
            print(f"  - {error}")
    
    if warnings:
        print("\n⚠️  Environment Validation Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    
    if not errors and not warnings:
        print("✅ Vocals environment validated successfully")
