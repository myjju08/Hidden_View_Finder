"""Storage-boundary checks shared by the optional building acquisition helpers."""
from __future__ import annotations
from contextlib import contextmanager
import math
from pathlib import Path
from collections.abc import Iterator, Sequence
from seoul_visibility.resources import preflight


def scoped_paths(data_root: Path, **paths: Path) -> tuple[Path, dict[str, Path]]:
    """Resolve symlinks before requiring every artifact inside its budget root."""
    root = data_root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError('--data-root must be a directory')
    resolved = {}
    for name, path in paths.items():
        candidate = path.resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError(f'{name} must be inside --data-root {root}; got {candidate}')
        resolved[name] = candidate
    if len(set(resolved.values())) != len(resolved):
        raise ValueError('Archive, output, metadata, and temporary paths must be distinct')
    return root, resolved


def validate_bounds(bounds: Sequence[float], name: str) -> None:
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError(f'{name} must be finite [xmin, ymin, xmax, ymax] with positive area')


def validate_download_cap(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('--max-download-mib must be a positive integer')


def budget_check(data_root: Path, **estimates: int) -> dict:
    return {'data_root': str(data_root.resolve()), **preflight(data_root, **estimates)}


@contextmanager
def owned_temporary(path: Path, *, gpkg: bool = False) -> Iterator[Path]:
    """Clean only newly created artifacts, preserving every pre-existing file."""
    paths = [path]
    if gpkg:
        paths += [Path(str(path) + suffix) for suffix in ('-journal', '-wal', '-shm')]
    for candidate in paths:
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(f'Previous or active temporary artifact is preserved: {candidate}')
    try:
        yield path
    except BaseException:
        for candidate in paths:
            candidate.unlink(missing_ok=True)
        raise
