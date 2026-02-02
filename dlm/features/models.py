from abc import ABC, abstractmethod
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional, Any
import shutil
import importlib.util
import sys
import subprocess

class FeatureStatus(Enum):
    INSTALLED = "installed"
    MISSING = "missing"
    PARTIAL = "partial"

# @dataclass (Removed to avoid inheritance issues with defaults)
class Dependency(ABC):
    """Abstract base class for a dependency."""
    name: str
    shared: bool # No default here to allow subclasses to add non-default fields

    @abstractmethod
    def is_met(self) -> bool:
        """Check if dependency is met."""
        pass

    @abstractmethod
    def install_command(self) -> List[str]:
        """Return generic install command (pip or pkg)."""
        pass

    @abstractmethod
    def uninstall_command(self) -> List[str]:
        """Return generic uninstall command."""
        pass

@dataclass
class PipDependency(Dependency):
    """A Python package installed via pip."""
    package_name: str
    
    def __init__(self, package_name: str, shared: bool = False):
        self.name = package_name
        self.package_name = package_name
        self.shared = shared

    def is_met(self) -> bool:
        """Check if package is importable or importlib.metadata finds it."""
        try:
            # Invalidate cache locally just in case
            import importlib
            importlib.invalidate_caches()
            
            from importlib.metadata import distribution, PackageNotFoundError
            distribution(self.package_name)
            return True
        except ImportError:
            # PackageNotFoundError inherits from ImportError? No, usually distinct or from metadata.
            # safe fallback
            return False
        except Exception:
            # If metadata fails, it's not installed properly
            return False

    def install_command(self) -> List[str]:
        return [sys.executable, "-m", "pip", "install", self.package_name]

    def uninstall_command(self) -> List[str]:
        return [sys.executable, "-m", "pip", "uninstall", "-y", self.package_name]

@dataclass
class PythonImportDependency(Dependency):
    """Check for a specific python module import."""
    module_name: str
    
    def __init__(self, module_name: str, shared: bool = False):
        self.name = f"import {module_name}"
        self.module_name = module_name
        self.shared = shared

    def is_met(self) -> bool:
        try:
            spec = importlib.util.find_spec(self.module_name)
            return spec is not None
        except:
            return False

    def install_command(self) -> List[str]:
        return [] # Cannot install generic import, usually tied to PipDependency

    def uninstall_command(self) -> List[str]:
        return []

@dataclass
class BinaryDependency(Dependency):
    """System binary (e.g., ffmpeg)."""
    binary_name: str
    
    def __init__(self, binary_name: str, shared: bool = False):
        self.name = binary_name
        self.binary_name = binary_name
        self.shared = shared
    
    def is_met(self) -> bool:
        return shutil.which(self.binary_name) is not None

    def install_command(self) -> List[str]:
        # Context-dependent, usually via system pkg manager
        return [] 

    def uninstall_command(self) -> List[str]:
        return [] 

@dataclass
class Feature:
    id: str
    name: str
    description: str
    dependencies: List[Dependency] = field(default_factory=list)
    is_core: bool = False
    category: str = "General"
    
    def check_status(self) -> FeatureStatus:
        if not self.dependencies:
            return FeatureStatus.INSTALLED
            
        met_count = sum(1 for d in self.dependencies if d.is_met())
        
        if met_count == len(self.dependencies):
            return FeatureStatus.INSTALLED
        elif met_count == 0:
            return FeatureStatus.MISSING
        else:
            return FeatureStatus.PARTIAL
