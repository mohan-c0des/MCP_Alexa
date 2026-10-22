"""fs_tools: places the assistant must never touch, plus a bounded folder/file search.

Shared by script_writer (organize a folder you name) and screen_control (find a file to open or fix).
"""
from __future__ import annotations

import os
import re
import string
import time
from collections import deque
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent  # the folder that holds the assistant's own code

SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "env", "site-packages", "$recycle.bin",
    "system volume information", "appdata", "windows", "program files", "program files (x86)",
    "programdata", "dist", "build", "cache",
}
PROTECTED = {
    "windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
    "system volume information", ".ssh", ".aws", ".gnupg", ".kube", ".azure", ".config",
}
POSIX_SYSTEM = {"etc", "usr", "bin", "sbin", "lib", "lib32", "lib64", "boot", "dev", "proc", "sys",
                "var", "opt", "root", "snap", "run", "srv"}


def is_blocked(path, for_file: bool = False) -> str:
    """Why a path is off limits ('' means allowed). for_file=True is the lighter check used before
    editing a single code file (your own project's files and files in your user folder are fine)."""
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        return "that path is not valid"
    home = Path.home().resolve()
    if not for_file:
        if p.parent == p:
            return "that is a drive or system root"
        if p == home or p in home.parents:
            return "that is your whole user folder, which is too broad"
        if p == _PROJECT or _PROJECT in p.parents:
            return "that is the assistant's own program folder"
    if {x.lower() for x in p.parts} & PROTECTED:
        return "that is a protected system or credentials folder"
    if os.name != "nt" and home not in p.parents and len(p.parts) > 1 and p.parts[1].lower() in POSIX_SYSTEM:
        return "that is a system folder"
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-.]+", "", s.lower())


def default_roots() -> list[Path]:
    home = Path.home()
    first = [home / n for n in ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music")]
    first += sorted(home.glob("OneDrive*"))
    roots: list[Path] = []
    for p in first + [home]:
        if p.exists() and p not in roots:
            roots.append(p)
    if os.name == "nt":
        roots += [Path(f"{d}:\\") for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return roots


def find_paths(name: str, kind: str = "dir", roots=None, max_results: int = 8,
               seconds: float = 8.0, max_depth: int = 6, include_fuzzy: bool = False) -> list[Path]:
    """Breadth-first search for a folder (kind='dir') or file (kind='file') by name.
    Shallow matches come first; exact name matches beat partial ones; protected places are skipped."""
    needle = _norm(name)
    if not needle:
        return []
    start = [Path(r) for r in (roots if roots is not None else default_roots())]
    queue = deque((r, 0) for r in start if r.exists())
    deadline = time.monotonic() + seconds
    seen: set[str] = set()
    exact: list[Path] = []
    fuzzy: list[Path] = []
    while queue and time.monotonic() < deadline and len(exact) < 5:
        folder, depth = queue.popleft()
        try:
            key = os.path.normcase(os.path.realpath(folder))
            if key in seen:
                continue
            seen.add(key)
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_file = (not is_dir) and e.is_file(follow_symlinks=False)
            except OSError:
                continue
            n = _norm(e.name)
            if (kind == "dir" and is_dir) or (kind == "file" and is_file):
                if n == needle or (kind == "file" and _norm(os.path.splitext(e.name)[0]) == needle):
                    exact.append(Path(e.path))
                elif needle in n:
                    fuzzy.append(Path(e.path))
            if is_dir and depth < max_depth and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                queue.append((Path(e.path), depth + 1))
    out: list[Path] = []
    seen_paths: set[str] = set()
    for p in (exact + fuzzy if include_fuzzy else (exact or fuzzy)):
        k = os.path.normcase(str(p))
        if k not in seen_paths and not is_blocked(p, for_file=(kind == "file")):
            seen_paths.add(k)
            out.append(p)
    return out[:max_results]