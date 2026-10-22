"""script_writer: plain-English task -> small Python script -> rehearsal on a COPY -> your yes -> guarded run -> undo.

MCP tools:
  write_script(task, folder="")  writes + checks the script, rehearses it on a copy and returns a REAL
                                 preview. `folder` is the NAME of a folder to work in (searched for on the
                                 computer); empty means the AlexaWorkspace folder.
  run_script(script_id)          after the user's yes: backs up (workspace) and runs the script in the sandbox
  undo_last_script()             puts everything back the way it was (script runs AND approved code fixes)

Two modes:
  workspace folder   scripts may create, move, edit and delete (everything is backed up, so undo works)
  any other folder   scripts may ONLY move, rename and create. Deleting, overwriting and moving folders are
                     blocked by the sandbox, so undo is exact and needs no copy of your files.
Protected places (system folders, drive roots, your whole user folder, credentials) are never allowed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from tools.analyze_data import WORKSPACE, _gemini_text
from tools.fs_tools import find_paths, is_blocked

SCRIPTS = WORKSPACE / "scripts"
UNDO = WORKSPACE / ".undo"
for _d in (SCRIPTS, UNDO):
    _d.mkdir(parents=True, exist_ok=True)
RUNNER = Path(__file__).with_name("sandbox_runner.py")
RUN_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 4000
PENDING_TTL_S = 600  # a written script can be approved for 10 minutes, then it expires
MAX_FILES = 3000
MAX_BYTES = 300 * 1024 * 1024  # workspace rehearsal + backup copy real files, so keep it modest
HASH_LIMIT = 50 * 1024 * 1024
HIDDEN = {"scripts", ".undo"}  # inside the workspace: never shown to the model, never diffed, read-only for scripts

ALLOWED_IMPORTS = {
    "pathlib", "os", "shutil", "json", "csv", "re", "datetime", "time", "math", "statistics",
    "collections", "itertools", "functools", "textwrap", "string", "zipfile", "glob", "fnmatch",
    "hashlib", "random", "typing", "io", "pandas", "numpy", "openpyxl", "pdf_utils",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
_ABS_PATH = re.compile(r"^([A-Za-z]:[\\/]|/[^/\s]|~[\\/])")
_WORKSPACE_WORDS = {"", "workspace", "alexaworkspace", "the workspace", "my workspace"}

RULES_WORKSPACE = ("- Never delete or overwrite existing files unless the task explicitly says to. To organize, MOVE\n"
                   "  files into subfolders; if the target name exists, add a numeric suffix.")
RULES_EXTERNAL = ("- This is the user's own folder. You may ONLY move, rename and create files. Deleting, overwriting,\n"
                  "  appending to existing files and moving folders are blocked and make the script fail. If a target\n"
                  "  name exists, add a numeric suffix. Work on the top level of the folder only, not its subfolders,\n"
                  "  unless the task says to include them. Never move the script's own helper files.")

WRITE_PROMPT = """You write small, safe Python 3 scripts that run on the user's laptop.
TASK: {task}

Environment:
- The script runs with the target folder as its working directory.
- Entries in the target folder (up to 100; names ending in / are folders): {files}

Rules:
- Use RELATIVE paths only (never absolute paths, never ~). No network, no subprocess.
- Allowed imports ONLY: {imports}. For PDFs use: from pdf_utils import md_to_pdf
  (md_to_pdf(markdown_text, "out.pdf") writes a PDF).
{mode_rules}
- Print a short, clear summary of exactly what was done (counts and names, under 40 lines).
- Keep it simple: under 80 lines, no input() prompts.

Reply in EXACTLY this format and nothing else:
SUMMARY: <one or two plain sentences saying what the script will do, no jargon>
DESTRUCTIVE: <yes or no>
```python
<the script>
```
DESTRUCTIVE is "yes" only if the script deletes or overwrites existing files. Moving files into
subfolders is "no"."""


# ----------------------------- static check ----------------------------------
def static_check(code: str) -> list[str]:
    """Return a list of problems; an empty list means the script passed."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "").split(".")[0]]
        else:
            mods = []
        problems += [f"import not allowed: {m}" for m in mods if m not in ALLOWED_IMPORTS]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            problems.append(f"call not allowed: {node.func.id}()")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _ABS_PATH.match(node.value):
            problems.append(f"absolute path not allowed (use relative paths): {node.value[:40]}")
    return problems


# ----------------------------- snapshots & diffs ------------------------------
def _hidden(root: Path) -> set[str]:
    return HIDDEN if root == WORKSPACE else set()


def _files(root: Path):
    hidden = _hidden(root)
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] in hidden:
            continue
        try:
            if p.is_file():
                yield rel.as_posix(), p
        except OSError:
            continue


def _digest(p: Path, cheap: bool = False) -> str:
    st = p.stat()
    if cheap:  # user folders can be huge: size + modified time is enough to recognise a moved file
        return f"{st.st_size}:{st.st_mtime_ns}"
    if st.st_size > HASH_LIMIT:
        return f"big:{st.st_size}:{int(st.st_mtime)}"
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(root: Path, cheap: bool) -> dict[str, str]:
    return {rel: _digest(p, cheap) for rel, p in _files(root)}


def _dirs(root: Path) -> set[str]:
    hidden = _hidden(root)
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_dir() and p.relative_to(root).parts[0] not in hidden}


def _check_size(root: Path, external: bool) -> None:
    n = total = 0
    for _, p in _files(root):
        n += 1
        if not external:
            total += p.stat().st_size
        if n > MAX_FILES or total > MAX_BYTES:
            limit = f"{MAX_FILES} files" + ("" if external else f" or {MAX_BYTES // 2**20} MB")
            raise RuntimeError(f"That folder is too large to rehearse safely (limit {limit}). Try a smaller folder.")


def _copy_workspace(dst: Path) -> None:
    def ignore(directory, names):
        return [n for n in names if Path(directory).resolve() == WORKSPACE and n in HIDDEN]
    shutil.copytree(WORKSPACE, dst, ignore=ignore)


def _placeholder_copy(src: Path, dst: Path) -> None:
    """Copy a user folder as EMPTY files with the same names and dates (rehearsal never reads their content)."""
    for dirpath, _, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            try:
                st = (Path(dirpath) / name).stat()
                target = dst / rel / name
                target.touch()
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                continue


def _diff(before: dict[str, str], after: dict[str, str]) -> dict:
    """What changed between two snapshots, with moves/renames detected by matching content."""
    removed = {k: v for k, v in before.items() if k not in after}
    created = {k: v for k, v in after.items() if k not in before}
    modified = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    by_digest: dict[str, list[str]] = {}
    for src, d in removed.items():
        by_digest.setdefault(d, []).append(src)
    moved, used = [], set()
    for same_name in (True, False):  # first pass: same file name; second: renamed
        for dst in sorted(created):
            if dst in used:
                continue
            cands = by_digest.get(created[dst], [])
            pick = next((s for s in cands if Path(s).name == Path(dst).name), None) if same_name else (cands[0] if cands else None)
            if pick:
                cands.remove(pick)
                used.add(dst)
                moved.append({"from": pick, "to": dst})
    gone = {m["from"] for m in moved}
    return {"moved": sorted(moved, key=lambda m: m["to"]), "created": sorted(set(created) - used),
            "deleted": sorted(set(removed) - gone), "modified": modified}


def _lines(d: dict, limit: int = 40) -> list[str]:
    out = ([f"moved: {m['from']} -> {m['to']}" for m in d["moved"]] + [f"created: {x}" for x in d["created"]]
           + [f"DELETED: {x}" for x in d["deleted"]] + [f"MODIFIED: {x}" for x in d["modified"]])
    return out[:limit] + ([f"... and {len(out) - limit} more"] if len(out) > limit else [])


def _counts(d: dict) -> dict:
    return {k: len(v) for k, v in d.items()}


def _prompt_listing(root: Path, limit: int = 100) -> list[str]:
    if root == WORKSPACE:
        return [rel for rel, _ in _files(root)][:limit]
    try:
        return sorted(e.name + ("/" if e.is_dir() else "") for e in os.scandir(root))[:limit]
    except OSError:
        return []


# ----------------------------- sandbox execution ------------------------------
def _run_in_sandbox(script: Path, root: Path, external: bool) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", k, re.I)}
    env.update(SANDBOX_WORKSPACE=str(root), SANDBOX_NONDESTRUCTIVE="1" if external else "0",
               SANDBOX_PROTECT="" if external else ",".join(sorted(HIDDEN)), PYTHONPATH=str(RUNNER.parent),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", str(RUNNER), str(script)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=RUN_TIMEOUT_S, encoding="utf-8", errors="replace")


def _rehearse(script: Path, root: Path, external: bool) -> dict:
    """Run the script on a throw-away COPY and report exactly what it would change."""
    _check_size(root, external)
    tmp = Path(tempfile.mkdtemp(prefix="alexa_rehearsal_"))
    try:
        copy = tmp / "ws"
        if external:
            _placeholder_copy(root, copy)
        else:
            _copy_workspace(copy)
        before = _snapshot(copy, external)
        try:
            proc = _run_in_sandbox(script, copy, external)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"The rehearsal ran longer than {RUN_TIMEOUT_S} seconds."}
        d = _diff(before, _snapshot(copy, external))
        ok = proc.returncode == 0
        return {"ok": ok, "error": "" if ok else (proc.stderr or proc.stdout)[-800:],
                "output": (proc.stdout or "")[-1500:], "changes": d, "counts": _counts(d), "lines": _lines(d),
                "needs_care": bool(d["deleted"] or d["modified"]), "total_files": len(before)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------- folders & pending scripts -----------------------
def _label(p: Path) -> str:
    return str(Path(*p.parts[-2:])) if len(p.parts) > 1 else str(p)


def _resolve_folder(folder: str):
    """-> (root, None) when a folder was found and is allowed, else (None, result for the agent)."""
    name = folder.strip().strip("\"'")
    if name.lower() in _WORKSPACE_WORDS:
        return WORKSPACE, None
    p = Path(name).expanduser()
    matches = [p.resolve()] if (p.is_absolute() and p.is_dir()) else ([] if p.is_absolute() else find_paths(name, "dir"))
    if not matches:
        return None, {"status": "folder_not_found",
                      "message": f"I couldn't find a folder called '{name}' that I'm allowed to work in.",
                      "next_step": "Tell the user you could not find it and ask them to say the folder name again (or another name)."}
    if len(matches) > 1:
        return None, {"status": "choose_folder",
                      "candidates": [{"path": str(m), "label": _label(m)} for m in matches[:5]],
                      "next_step": "Several folders match. Read the labels to the user, ask which one, then call "
                                   "write_script again with the chosen full path as `folder`."}
    root = matches[0].resolve()
    reason = is_blocked(root)
    if not reason and root != WORKSPACE and WORKSPACE in root.parents and root.relative_to(WORKSPACE).parts[0] in HIDDEN:
        reason = "that is where the assistant keeps its own scripts and backups"
    if reason:
        return None, {"status": "folder_blocked", "message": f"I can't work in {_label(root)}: {reason}.",
                      "next_step": "Tell the user this folder is off limits and offer to work in a different folder."}
    return root, None


def _parse_reply(text: str):
    """Split the model reply into (summary, destructive, code, problems)."""
    summary = re.search(r"^SUMMARY:\s*(.+)$", text, re.M | re.I)
    destructive = re.search(r"^DESTRUCTIVE:\s*(yes|true)", text, re.M | re.I)
    code = re.search(r"```(?:python)?[ \t]*\n(.*?)```", text, re.S)
    if not code:
        return "", False, "", ["no complete ```python code block (reply may have been cut off)"]
    src = code.group(1).strip()
    problems = static_check(src) if src else ["empty script"]
    return (summary.group(1).strip() if summary else ""), bool(destructive), src, problems


def _new_script_id() -> str:
    """script_YYYYMMDD_HHMMSS, moved forward a second if that id is already taken."""
    t = datetime.now()
    while (SCRIPTS / f"script_{t:%Y%m%d_%H%M%S}.py").exists():
        t += timedelta(seconds=1)
    return f"script_{t:%Y%m%d_%H%M%S}"


def _is_pending(path: Path) -> bool:
    return (path.exists() and not path.with_suffix(".done").exists()
            and time.time() - path.stat().st_mtime <= PENDING_TTL_S)


def _latest_pending() -> Path | None:
    files = sorted(SCRIPTS.glob("script_*.py"), key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in files if _is_pending(p)), None)


# ----------------------------- MCP tools -------------------------------------
def write_script(task: str, folder: str = "") -> dict:
    """Use when the user wants something DONE with files: organize or rename files in a folder, move or
    sort files, generate a PDF or report from files, clean or convert a data file, and similar chores.
    If the user names a folder ('my Downloads', 'the math-unit1 folder'), pass just that NAME as `folder`;
    it is searched for on the computer. Leave `folder` empty for the AlexaWorkspace folder. Writes a small
    Python script, REHEARSES it on a copy and returns a real preview of what would change. It changes
    nothing yet. Check `status`: folder_not_found -> tell the user and ask them to say the folder name
    again; choose_folder -> read the candidate labels, ask which one, call again with the chosen full path
    as `folder`; folder_blocked -> explain that the folder is off limits; preview_failed -> say what went
    wrong; preview_ready -> tell the user the exact folder, the counts and a few examples, say clearly if
    anything would be DELETED or MODIFIED, and ask 'shall I run it?'. Never call run_script in the same
    turn; wait for the user's explicit yes. Scripts have no internet and cannot use git."""
    root, problem = _resolve_folder(folder)
    if problem:
        return problem
    external = root != WORKSPACE
    prompt = (WRITE_PROMPT.replace("{task}", task).replace("{files}", str(_prompt_listing(root)))
              .replace("{imports}", ", ".join(sorted(ALLOWED_IMPORTS)))
              .replace("{mode_rules}", RULES_EXTERNAL if external else RULES_WORKSPACE))
    problems: list[str] = []
    summary, destructive, code = "", False, ""
    for _ in range(3):
        retry = f"\n\nYour previous reply was rejected: {problems}. Fix this and reply again in the same format." if problems else ""
        summary, destructive, code, problems = _parse_reply(_gemini_text(prompt + retry))
        if not problems:
            break
    if problems:
        raise RuntimeError(f"Could not produce a safe script for this task: {problems}")

    script_id = _new_script_id()
    path = SCRIPTS / f"{script_id}.py"
    path.write_text(code, encoding="utf-8")
    try:
        preview = _rehearse(path, root, external)
    except RuntimeError as e:  # for example: folder too large to copy
        preview = {"ok": False, "error": str(e)}
    path.with_suffix(".preview.json").write_text(
        json.dumps({"summary": summary, "root": str(root), "external": external, **preview}), encoding="utf-8")

    note = (" Only moving, renaming and creating files is possible in this folder; nothing can be deleted or "
            "overwritten." if external else "")
    result = {
        "status": "preview_ready" if preview["ok"] else "preview_failed",
        "script_id": script_id,
        "folder": str(root),
        "summary_for_user": summary,
        "preview": {k: preview[k] for k in ("ok", "counts", "lines", "needs_care", "output", "error", "total_files") if k in preview},
        "code": code,
    }
    if preview["ok"]:
        result["next_step"] = (f"Tell the user the exact folder ({root}), the counts and a few examples from the preview"
                               f" (mention any DELETED or MODIFIED files).{note} Then ask for a clear yes before run_script.")
    else:
        result["next_step"] = ("The rehearsal on a copy FAILED, so this script cannot be run. Tell the user briefly "
                               "what went wrong and offer to try again with write_script.")
    return result


def run_script(script_id: str = "") -> dict:
    """Run the script that write_script just created, ONLY after the user has said yes to its preview.
    `script_id` is the id write_script returned; if you do not have it, leave it empty and the most
    recent script waiting for approval runs. Only a script that already ran cleanly on a copy can run.
    A script runs once and expires after 10 minutes. It runs in a sandbox: it can only change files
    inside the one approved folder and has no network. Returns what actually changed. Report it in one or
    two sentences and tell the user they can say 'undo' to put everything back."""
    path = SCRIPTS / f"{script_id}.py" if re.fullmatch(r"script_\d{8}_\d{6}", script_id or "") else None
    if path is None or not _is_pending(path):
        path = _latest_pending()
    if path is None:
        raise RuntimeError("No script is waiting for approval (they expire after 10 minutes). Call write_script first.")
    problems = static_check(path.read_text(encoding="utf-8"))
    if problems:
        raise RuntimeError(f"Script failed the safety check: {problems}")
    sidecar = path.with_suffix(".preview.json")
    preview = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    if not preview.get("ok"):
        raise RuntimeError("This script has not run cleanly on a copy, so it will not run for real. Call write_script again.")
    root, external = Path(preview["root"]), bool(preview.get("external"))
    if external and (not root.is_dir() or is_blocked(root)):
        raise RuntimeError("That folder is no longer available or is off limits.")
    _check_size(root, external)
    path.with_suffix(".done").write_text(datetime.now().isoformat())  # single use, even if it crashes

    sid = path.stem
    if not external:  # back up the workspace (pruned to what really changed afterwards)
        _copy_workspace(UNDO / sid / "before")
    (UNDO / sid).mkdir(parents=True, exist_ok=True)
    before, dirs_before = _snapshot(root, external), _dirs(root)
    try:
        proc, timed_out = _run_in_sandbox(path, root, external), False
    except subprocess.TimeoutExpired:
        proc, timed_out = None, True

    after = _snapshot(root, external)
    d = _diff(before, after)
    backup = UNDO / sid / "before"
    if not external:  # keep only the files undo needs: deleted and modified ones (moves are moved back)
        need = set(d["deleted"]) | set(d["modified"])
        for rel, p in list(_files(backup)):
            if rel not in need:
                p.unlink()
        for p in sorted((x for x in backup.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            try:
                p.rmdir()
            except OSError:
                pass
    post = {rel: after[rel] for rel in set(d["created"]) | {m["to"] for m in d["moved"]} | set(d["modified"])}
    (UNDO / sid / "manifest.json").write_text(json.dumps({
        "kind": "script", "script_id": sid, "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": preview.get("summary", ""), "root": str(root), "cheap": external, "diff": d, "post": post,
        "new_dirs": sorted(_dirs(root) - dirs_before), "undone": False}), encoding="utf-8")

    succeeded = proc is not None and proc.returncode == 0
    return {
        "script_id": sid, "folder": str(root), "succeeded": succeeded,
        "output": ((proc.stdout if proc else "") or "")[-MAX_OUTPUT_CHARS:],
        "errors": ("Stopped: it ran longer than %d seconds" % RUN_TIMEOUT_S if timed_out else (proc.stderr or "")[-MAX_OUTPUT_CHARS:]),
        "changes": {"counts": _counts(d), "lines": _lines(d)},
        "matches_preview": d == preview.get("changes"),
        "undo_available": True,
        "tip": "Tell the user they can say 'undo' to put everything back.",
    }


def record_code_fix(path: Path, old: bytes, new: bytes, summary: str) -> str:
    """Save a backup + undo record BEFORE a code file is changed (used by screen_control's code fixes)."""
    t = datetime.now()
    while (UNDO / f"fix_{t:%Y%m%d_%H%M%S}").exists():
        t += timedelta(seconds=1)
    sid = f"fix_{t:%Y%m%d_%H%M%S}"
    (UNDO / sid / "before").mkdir(parents=True)
    (UNDO / sid / "before" / "file.bak").write_bytes(old)
    (UNDO / sid / "manifest.json").write_text(json.dumps({
        "kind": "code_fix", "script_id": sid, "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary, "path": str(path), "post_digest": hashlib.sha1(new).hexdigest(), "undone": False}), encoding="utf-8")
    return sid


def _safe_rel(rel: str, root: Path) -> Path:
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"unsafe path in undo record: {rel}")
    return p


def _undo_code_fix(m: dict, manifest_path: Path) -> dict:
    p, backup = Path(m["path"]), manifest_path.parent / "before" / "file.bak"
    problems = []
    if not p.exists():
        problems.append(f"{p.name} no longer exists")
    elif hashlib.sha1(p.read_bytes()).hexdigest() != m["post_digest"]:
        problems.append(f"{p.name} was edited after the fix, so I did not overwrite it")
    elif not backup.exists():
        problems.append("the backup is missing")
    else:
        tmp = p.with_name(p.name + ".alexa_tmp")
        tmp.write_bytes(backup.read_bytes())
        os.replace(tmp, p)
    return {"restored": 0 if problems else 1, "removed": 0, "problems": problems}


def undo_last_script() -> dict:
    """Undo the most recent change the assistant made: a script run (put moved files back, restore deleted
    or modified files, remove what it created) or an approved code fix (restore the file). Use when the user
    says undo, put it back, or revert. Safe to call repeatedly: each call undoes one earlier change. Reports
    anything it could not restore."""
    candidates = sorted(UNDO.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    manifest_path, m = None, None
    for mp in candidates:
        data = json.loads(mp.read_text(encoding="utf-8"))
        if not data.get("undone"):
            manifest_path, m = mp, data
            break
    if m is None:
        raise RuntimeError("There is nothing to undo.")

    if m.get("kind") == "code_fix":
        r = _undo_code_fix(m, manifest_path)
        restored, removed, conflicts = r["restored"], 0, r["problems"]
    else:
        root, cheap = Path(m.get("root") or WORKSPACE), bool(m.get("cheap"))
        d, post, backup = m["diff"], m["post"], manifest_path.parent / "before"
        restored = removed = 0
        conflicts = []
        for mv in d["moved"]:  # 1) move files back to where they were
            dst, src = _safe_rel(mv["to"], root), _safe_rel(mv["from"], root)
            if not dst.exists():
                conflicts.append(f"{mv['to']} is gone, so I could not move it back")
            elif _digest(dst, cheap) != post.get(mv["to"]):
                conflicts.append(f"{mv['to']} was changed after the script ran, so I left it in place")
            elif src.exists():
                conflicts.append(f"{mv['from']}: a file with that name exists now, so I did not overwrite it")
            else:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                restored += 1
        for rel in d["created"]:  # 2) remove files the script created (if untouched since)
            p = _safe_rel(rel, root)
            if p.exists():
                if _digest(p, cheap) == post.get(rel):
                    p.unlink()
                    removed += 1
                else:
                    conflicts.append(f"{rel} was changed after the script ran, so I left it in place")
        for rel in d["deleted"] + d["modified"]:  # 3) bring back deleted and edited files from the backup
            dst, src = _safe_rel(rel, root), backup / rel
            if not src.exists():
                conflicts.append(f"{rel}: the backup is missing")
            elif rel in d["modified"] and dst.exists() and _digest(dst, cheap) != post.get(rel):
                conflicts.append(f"{rel} was edited after the script ran, so I did not overwrite it")
            elif rel not in d["modified"] and dst.exists():
                conflicts.append(f"{rel}: a file with that name exists now, so I did not overwrite it")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
        for rel in sorted(m["new_dirs"], key=len, reverse=True):  # 4) tidy up empty folders it created
            try:
                _safe_rel(rel, root).rmdir()
            except OSError:
                pass

    m["undone"] = True
    manifest_path.write_text(json.dumps(m), encoding="utf-8")
    if not conflicts:
        shutil.rmtree(manifest_path.parent / "before", ignore_errors=True)
    return {"undone": m["script_id"], "what_it_did": m.get("summary", ""), "files_restored": restored,
            "files_removed": removed, "problems": conflicts, "fully_undone": not conflicts}