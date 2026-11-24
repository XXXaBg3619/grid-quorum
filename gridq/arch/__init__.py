"""The four architectures under comparison (see base.py for the interface)."""
from .a import ArchA
from .b import ArchB
from .base import Architecture
from .c import ArchC
from .d import ArchD

REGISTRY = {"A": ArchA, "B": ArchB, "C": ArchC, "D": ArchD}


def get(name: str):
    return REGISTRY[name]
