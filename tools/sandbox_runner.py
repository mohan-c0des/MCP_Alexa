"""Runs ONE approved script inside guard rails. Used by script_writer.

Usage:  python sandbox_runner.py <script.py>
Env:    SANDBOX_WORKSPACE=<the only folder the script may change>
        SANDBOX_NONDESTRUCTIVE=1   (user folders: move, rename and create only)
        SANDBOX_PROTECT=a,b        (sub-folders of the workspace that scripts cannot change)

Guards (a Python audit hook, so they also catch tricks the static check misses):
  - writes, deletes, renames and moves are only allowed INSIDE the one approved folder
  - no network, no subprocesses, no shell commands
  - reading well-known secret locations (.ssh, .aws, .env, ...) is blocked
  - in NONDESTRUCTIVE mode: no deleting, no overwriting or appending to existing files, no moving folders
This is defence in depth for a hackathon demo, not a perfect jail.
"""
import os
import runpy
import sys
from pathlib import Path

WORKSPACE = os.path.normcase(os.path.realpath(os.environ["SANDBOX_WORKSPACE"]))
NONDESTRUCTIVE = os.environ.get("SANDBOX_NONDESTRUCTIVE") == "1"
PROTECTED_SUBDIRS = {os.path.normcase(x) for x in os.environ.get("SANDBOX_PROTECT", "").split(",") if x}
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".azure"}
SECRET_FILES = {"credentials", "id_rsa", "id_ed25519", "login data", "cookies"}

BLOCKED_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile",
    "socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
}
# event name -> positions of the path arguments that must stay inside the approved folder
PATH_ARGS = {
    "os.remove": [0], "os.rmdir": [0], "os.mkdir": [0], "os.truncate": [0], "os.chmod": [0],
    "os.rename": [0, 1], "os.symlink": [1], "os.link": [0, 1],
    "shutil.copyfile": [1], "shutil.copytree": [1], "shutil.move": [0, 1], "shutil.rmtree": [0],
    "shutil.copymode": [1], "shutil.copystat": [1], "shutil.make_archive": [0],
    "shutil.unpack_archive": [1],
}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(os.fsdecode(p)))


def _writable(p) -> bool:
    """Inside the approved folder, and not in one of its protected sub-folders."""
    n = _norm(p)
    if not (n == WORKSPACE or n.startswith(WORKSPACE + os.sep)):
        return False
    rel = n[len(WORKSPACE):].lstrip(os.sep)
    return not rel or rel.split(os.sep)[0] not in PROTECTED_SUBDIRS


def _secret(p) -> bool:
    n = _norm(p)
    parts = {x.lower() for x in Path(n).parts}
    name = Path(n).name.lower()
    return bool(parts & SECRET_DIRS) or name in SECRET_FILES or name.startswith(".env")


def _deny(what: str):
    raise PermissionError(f"Blocked by sandbox: {what}")


def _overwrites(path, mode, flags) -> bool:
    """Would this open() change a file that already exists?"""
    if not os.path.exists(os.fsdecode(path)):
        return False
    if mode is not None:
        return any(c in str(mode) for c in "wa+")
    return bool(isinstance(flags, int) and flags & (os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR))


def _nondestructive(event: str, args: tuple) -> None:
    if event in ("os.remove", "os.truncate", "shutil.rmtree"):
        _deny(f"{event}: this folder allows move, rename and create only")
    if event == "os.rename" and len(args) >= 2:
        if os.path.isdir(os.fsdecode(args[0])):
            _deny("moving or renaming folders is not allowed here")
        if os.path.lexists(os.fsdecode(args[1])):
            _deny("overwriting an existing file is not allowed here")
    elif event == "shutil.move" and os.path.isdir(os.fsdecode(args[0])):
        _deny("moving folders is not allowed here")
    elif event in ("shutil.copyfile", "shutil.copytree") and os.path.lexists(os.fsdecode(args[1])):
        _deny("overwriting an existing file is not allowed here")


def _hook(event: str, args: tuple) -> None:
    if event in BLOCKED_EVENTS:
        _deny(event)
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        if isinstance(path, int):
            return
        writing = any(c in str(mode or "") for c in "wax+") or (isinstance(flags, int) and flags & WRITE_FLAGS)
        if writing and not _writable(path):
            _deny(f"writing outside the approved folder or into a protected folder ({os.fsdecode(path)})")
        if writing and NONDESTRUCTIVE and _overwrites(path, mode, flags):
            _deny("changing an existing file (this folder allows move, rename and create only)")
        if not writing and _secret(path):
            _deny(f"reading a protected location ({os.fsdecode(path)})")
    elif event in PATH_ARGS:
        for i in PATH_ARGS[event]:
            if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)) and not _writable(args[i]):
                _deny(f"{event} outside the approved folder or into a protected folder ({os.fsdecode(args[i])})")
        if NONDESTRUCTIVE:
            _nondestructive(event, args)


if __name__ == "__main__":
    script = os.path.realpath(sys.argv[1])
    sys.addaudithook(_hook)
    sys.argv = [script]
    runpy.run_path(script, run_name="__main__")