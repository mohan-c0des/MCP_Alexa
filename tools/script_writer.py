"""script_writer: plain-English task -> small Python script -> rehearsal on a COPY -> your yes -> guarded run -> undo.

Three MCP tools, so a human always sits between "write" and "run", and every run can be reversed:
  write_script(task)     writes + checks the script, rehearses it on a copy of the workspace and
                         returns a REAL preview (what would be moved / created / deleted / modified)
  run_script(script_id)  after the user's yes: backs up, runs the script for real in the sandbox
  undo_last_script()     puts everything back the way it was before the last run

Safety layers: (1) import allow-list + banned calls (static check), (2) rehearsal: nothing runs for
real unless it ran cleanly on a copy first, (3) runtime audit hook (writes only inside the workspace,
no network/subprocess, scripts/ and .undo/ are read-only), (4) secrets stripped from the environment,
(5) 60 second timeout, (6) the user hears the real preview and says yes first, (7) undo.
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

from analyze_data import WORKSPACE, _gemini_text

SCRIPTS = WORKSPACE / "scripts"
UNDO = WORKSPACE / ".undo"
for _d in (SCRIPTS, UNDO):
    _d.mkdir(parents=True, exist_ok=True)
RUNNER = Path(__file__).with_name("sandbox_runner.py")
RUN_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 4000
PENDING_TTL_S = 600  # a written script can be approved for 10 minutes, then it expires
MAX_FILES = 3000
MAX_BYTES = 300 * 1024 * 1024  # rehearsal and backup copy the workspace, so keep it modest
HASH_LIMIT = 50 * 1024 * 1024
HIDDEN = {"scripts", ".undo"}  # never shown to the model, never part of a rehearsal or a diff

ALLOWED_IMPORTS = {
    "pathlib", "os", "shutil", "json", "csv", "re", "datetime", "time", "math", "statistics",
    "collections", "itertools", "functools", "textwrap", "string", "zipfile", "glob", "fnmatch",
    "hashlib", "random", "typing", "io", "pandas", "numpy", "openpyxl", "pdf_utils",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
_ABS_PATH = re.compile(r"^([A-Za-z]:[\\/]|/[^/\s]|~[\\/])")

WRITE_PROMPT = """You write small, safe Python 3 scripts that run on the user's laptop.
TASK: {task}

Environment:
- The script runs with the workspace folder as its working directory.
- Files currently in the workspace (up to 80): {files}

Rules:
- Use RELATIVE paths only (never absolute paths, never ~). No network, no subprocess.
- Allowed imports ONLY: {imports}. For PDFs use: from pdf_utils import md_to_pdf
  (md_to_pdf(markdown_text, "out.pdf") writes a PDF).
- Never delete or overwrite existing files unless the task explicitly says to. To organize, MOVE
  files into subfolders; if the target name exists, add a numeric suffix.
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
def _files(root: Path):
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] not in HIDDEN and p.is_file():
            yield rel.as_posix(), p


def _digest(p: Path) -> str:
    st = p.stat()
    if st.st_size > HASH_LIMIT:
        return f"big:{st.st_size}:{int(st.st_mtime)}"
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    return {rel: _digest(p) for rel, p in _files(root)}


def _dirs(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_dir() and p.relative_to(root).parts[0] not in HIDDEN}


def _check_size(root: Path) -> None:
    n = total = 0
    for _, p in _files(root):
        n, total = n + 1, total + p.stat().st_size
        if n > MAX_FILES or total > MAX_BYTES:
            raise RuntimeError(f"The workspace is too large to rehearse and back up safely "
                               f"(limit {MAX_FILES} files or {MAX_BYTES // 2**20} MB).")


def _copy_workspace(dst: Path) -> None:
    def ignore(directory, names):
        return [n for n in names if Path(directory).resolve() == WORKSPACE and n in HIDDEN]
    shutil.copytree(WORKSPACE, dst, ignore=ignore)


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


def _list_workspace(limit: int = 80) -> list[str]:
    return [rel for rel, _ in _files(WORKSPACE)][:limit]


# ----------------------------- sandbox execution ------------------------------
def _run_in_sandbox(script: Path, workspace: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", k, re.I)}
    env.update(SANDBOX_WORKSPACE=str(workspace), PYTHONPATH=str(RUNNER.parent),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", str(RUNNER), str(script)], cwd=workspace, env=env,
                          capture_output=True, text=True, timeout=RUN_TIMEOUT_S, encoding="utf-8", errors="replace")


def _rehearse(script: Path) -> dict:
    """Run the script on a throw-away COPY of the workspace and report exactly what it would change."""
    _check_size(WORKSPACE)
    tmp = Path(tempfile.mkdtemp(prefix="alexa_rehearsal_"))
    try:
        copy = tmp / "ws"
        _copy_workspace(copy)
        before = _snapshot(copy)
        try:
            proc = _run_in_sandbox(script, copy)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"The rehearsal ran longer than {RUN_TIMEOUT_S} seconds."}
        d = _diff(before, _snapshot(copy))
        ok = proc.returncode == 0
        return {"ok": ok, "error": "" if ok else (proc.stderr or proc.stdout)[-800:],
                "output": (proc.stdout or "")[-1500:], "changes": d, "counts": _counts(d),
                "lines": _lines(d), "needs_care": bool(d["deleted"] or d["modified"])}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------- pending scripts --------------------------------
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
def write_script(task: str) -> dict:
    """Use when the user wants something DONE with files on the laptop: organize or rename files
    in a folder, move or sort files, generate a PDF or report from files, clean or convert a data
    file, and similar chores. Writes a small Python script, REHEARSES it on a copy of the workspace,
    and returns a real preview of what would change (moved, created, DELETED, MODIFIED files). It does
    NOT change anything yet. Tell the user the counts and a few examples from the preview, say clearly
    if any files would be deleted or modified, and ask 'shall I run it?'. Never call run_script in the
    same turn; wait for the user's explicit yes. Scripts have no internet and cannot use git."""
    prompt = (WRITE_PROMPT.replace("{task}", task).replace("{files}", str(_list_workspace()))
              .replace("{imports}", ", ".join(sorted(ALLOWED_IMPORTS))))
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
        preview = _rehearse(path)
    except RuntimeError as e:  # for example: workspace too large to copy
        preview = {"ok": False, "error": str(e)}
    path.with_suffix(".preview.json").write_text(json.dumps({"summary": summary, **preview}), encoding="utf-8")

    result = {
        "script_id": script_id,
        "summary_for_user": summary,
        "preview": {k: preview[k] for k in ("ok", "counts", "lines", "needs_care", "output", "error") if k in preview},
        "code": code,
    }
    if preview["ok"]:
        result["next_step"] = ("Tell the user what the preview shows (counts plus a few examples; mention any "
                               "DELETED or MODIFIED files) and ask for a clear yes before run_script.")
    else:
        result["next_step"] = ("The rehearsal on a copy FAILED, so this script cannot be run. Tell the user briefly "
                               "what went wrong and offer to try again with write_script.")
    return result


def run_script(script_id: str = "") -> dict:
    """Run the script that write_script just created, ONLY after the user has said yes to its preview.
    `script_id` is the id write_script returned; if you do not have it, leave it empty and the most
    recent script waiting for approval runs. Only a script that already ran cleanly on a copy can run.
    It first backs up what it could change, so the user can say 'undo'. A script runs once and expires
    after 10 minutes. It runs in a sandbox: it can only change files inside the workspace folder and has
    no network. Returns what actually changed. Report it in one or two sentences and tell the user they
    can say 'undo' to put everything back."""
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
        raise RuntimeError("This script has not run cleanly on a copy, so it will not run for real. "
                           "Call write_script again.")
    _check_size(WORKSPACE)
    path.with_suffix(".done").write_text(datetime.now().isoformat())  # single use, even if it crashes

    # 1) back up everything (pruned to what actually changed after the run)
    sid = path.stem
    backup = UNDO / sid / "before"
    _copy_workspace(backup)
    before, dirs_before = _snapshot(WORKSPACE), _dirs(WORKSPACE)

    # 2) run for real
    try:
        proc = _run_in_sandbox(path, WORKSPACE)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc, timed_out = None, True

    # 3) what really changed, then keep only the backups that undo needs
    after = _snapshot(WORKSPACE)
    d = _diff(before, after)
    need = set(d["deleted"]) | set(d["modified"]) | {m["from"] for m in d["moved"]}
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
        "script_id": sid, "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": preview.get("summary", ""), "diff": d, "post": post,
        "new_dirs": sorted(_dirs(WORKSPACE) - dirs_before), "undone": False}), encoding="utf-8")

    succeeded = proc is not None and proc.returncode == 0
    return {
        "script_id": sid,
        "succeeded": succeeded,
        "output": ((proc.stdout if proc else "") or "")[-MAX_OUTPUT_CHARS:],
        "errors": ("Stopped: it ran longer than %d seconds" % RUN_TIMEOUT_S if timed_out
                   else ((proc.stderr if proc is not None else "") or "")[-MAX_OUTPUT_CHARS:]),
        "changes": {"counts": _counts(d), "lines": _lines(d)},
        "matches_preview": d == preview.get("changes"),
        "undo_available": True,
        "tip": "Tell the user they can say 'undo' to put everything back.",
    }


def _safe_rel(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ValueError(f"unsafe path in undo record: {rel}")
    return p


def undo_last_script() -> dict:
    """Undo the most recent script run: put moved files back, restore deleted or modified files, and
    remove files and folders the script created. Use when the user says undo, put it back, or revert.
    Safe to call repeatedly: each call undoes one earlier run. Reports anything it could not restore."""
    candidates = sorted(UNDO.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    manifest_path, m = None, None
    for mp in candidates:
        data = json.loads(mp.read_text(encoding="utf-8"))
        if not data.get("undone"):
            manifest_path, m = mp, data
            break
    if m is None:
        raise RuntimeError("There is nothing to undo.")

    assert manifest_path is not None
    d, post, backup = m["diff"], m["post"], manifest_path.parent / "before"
    removed, restored, conflicts = [], [], []
    for rel in d["created"] + [x["to"] for x in d["moved"]]:  # 1) remove what the script made
        p = _safe_rel(rel)
        if not p.exists():
            continue
        if _digest(p) == post.get(rel):
            p.unlink()
            removed.append(rel)
        else:
            conflicts.append(f"{rel} was changed after the script ran, so I left it in place")
    for rel in d["deleted"] + [x["from"] for x in d["moved"]] + d["modified"]:  # 2) bring the originals back
        dst, src = _safe_rel(rel), backup / rel
        if not src.exists():
            conflicts.append(f"{rel}: the backup is missing")
        elif rel in d["modified"] and dst.exists() and _digest(dst) != post.get(rel):
            conflicts.append(f"{rel} was edited after the script ran, so I did not overwrite it")
        elif rel not in d["modified"] and dst.exists():
            conflicts.append(f"{rel}: a file with that name exists now, so I did not overwrite it")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel)
    for rel in sorted(m["new_dirs"], key=len, reverse=True):  # 3) tidy up empty folders it created
        try:
            _safe_rel(rel).rmdir()
        except OSError:
            pass

    m["undone"] = True
    manifest_path.write_text(json.dumps(m), encoding="utf-8")
    if not conflicts:
        shutil.rmtree(manifest_path.parent / "before", ignore_errors=True)
    return {"undone_script": m["script_id"], "what_it_did": m.get("summary", ""),
            "files_restored": len(restored), "files_removed": len(removed), "problems": conflicts,
            "fully_undone": not conflicts}