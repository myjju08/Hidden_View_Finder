"""Conservative storage/RAM preflights. Never remove inputs to make room."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import shutil
import psutil

from .errors import ResourceBudgetError

GiB = 1024**3

@dataclass(frozen=True)
class StoragePolicy:
    total_budget_bytes: int = 20 * GiB
    minimum_free_bytes: int = 8 * GiB
    disk_cache_bytes: int = GiB
    temporary_budget_bytes: int = 4 * GiB

    def __post_init__(self) -> None:
        if any(not isinstance(v, int) or v < 0 for v in asdict(self).values()):
            raise ResourceBudgetError("Storage limits must be nonnegative integer byte counts")

def tree_bytes(root: str | Path) -> int:
    root = Path(root)
    seen: set[tuple[int, int]] = set()
    total = 0
    if not root.exists():
        return 0
    paths = [root] if root.is_file() else root.rglob("*")
    for p in paths:
        if p.is_file():
            s = p.stat()
            key = (s.st_dev, s.st_ino)
            if key not in seen:
                total += s.st_size
                seen.add(key)
    return total

def preflight(root: str | Path, additional_bytes: int = 0,
              temporary_bytes: int = 0, policy: StoragePolicy | None = None) -> dict:
    policy = policy or StoragePolicy()
    root = Path(root).resolve()
    parent = root
    while not parent.exists():
        parent = parent.parent
    if additional_bytes < 0 or temporary_bytes < 0:
        raise ResourceBudgetError("Peak storage estimates must be nonnegative")
    current = tree_bytes(root)
    free = shutil.disk_usage(parent).free
    peak_extra = int(additional_bytes + temporary_bytes)
    report = {"current_bytes": current, "free_bytes": free,
              "additional_bytes": int(additional_bytes), "temporary_bytes": int(temporary_bytes),
              "peak_project_bytes": current + peak_extra, "policy": asdict(policy)}
    if temporary_bytes > policy.temporary_budget_bytes:
        raise ResourceBudgetError(f"Temporary estimate {temporary_bytes:,} exceeds cap {policy.temporary_budget_bytes:,} bytes")
    if current + peak_extra > policy.total_budget_bytes:
        raise ResourceBudgetError(f"Project peak {current + peak_extra:,} exceeds budget {policy.total_budget_bytes:,} bytes; reduce extent/resolution or revise budget")
    if free - peak_extra < policy.minimum_free_bytes:
        raise ResourceBudgetError(f"Free space after peak would be {free - peak_extra:,} bytes; required reserve {policy.minimum_free_bytes:,}")
    return report

def available_memory_bytes() -> int:
    available = int(psutil.virtual_memory().available)
    try:
        limit_text = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if limit_text != 'max':
            used = int(Path('/sys/fs/cgroup/memory.current').read_text())
            available = min(available, max(0, int(limit_text) - used))
    except (OSError, ValueError):
        pass
    return available

def memory_preflight(required_bytes: int) -> dict:
    available = available_memory_bytes()
    # One job, no nested worker pools. Reserve half of currently available RAM.
    if required_bytes > available // 2:
        raise ResourceBudgetError(f"Query working estimate {required_bytes:,} bytes exceeds half of available RAM {available:,}")
    return {"estimated_working_bytes": required_bytes, "available_memory_bytes": available,
            "concurrent_jobs": 1, "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()}
