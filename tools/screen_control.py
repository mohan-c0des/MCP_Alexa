"""ask_about_screen & interact_with_screen: Multimodal Vision + Live GUI Computer-Use.

Why PyAutoGUI + OS Hooks instead of Playwright alone:
  Playwright can only click inside a brand-new sandboxed browser it launches itself—it CANNOT
  click inside your open VS Code window, close your active Chrome tabs, or open local files.
  By combining Gemini Vision 2D coordinate grounding (0..1000 scale) with PyAutoGUI, PyPerClip,
  DDGS video resolution, and native OS hooks, Alexa+ can physically move your mouse cursor,
  click buttons, close tabs, open files, and fix code lines live in VS Code.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import webbrowser
from pathlib import Path

import mss
import pyautogui
import pyperclip
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# Fail-safe: slamming the mouse into the top-left corner (0,0) aborts automation immediately
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.25

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
WORKSPACE = Path(os.environ.get("MCP_WORKSPACE", Path.home() / "AlexaWorkspace")).resolve()

SEARCH_DIRS = [
    WORKSPACE,
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
]


def _grab_screen(max_side: int = 1600) -> tuple[bytes, int, int, int, int]:
    """Capture the primary monitor.
    Returns (png_bytes, monitor_left, monitor_top, monitor_width, monitor_height).
    """
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), mon["left"], mon["top"], mon["width"], mon["height"]


def _norm_to_screen(x_norm: float, y_norm: float, left: int, top: int, width: int, height: int) -> tuple[int, int]:
    """Convert 0..1000 normalized vision coordinates to actual monitor pixel coordinates."""
    clamped_x = max(5.0, min(995.0, float(x_norm)))
    clamped_y = max(5.0, min(995.0, float(y_norm)))
    real_x = int(left + (clamped_x / 1000.0) * width)
    real_y = int(top + (clamped_y / 1000.0) * height)
    return real_x, real_y


def _call_gemini_vision(png: bytes, prompt: str, json_mode: bool = False) -> str:
    """Send the current screen PNG + prompt to Gemini Vision."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing in environment variables.")

    b64 = base64.b64encode(png).decode()
    gen_config: dict = {"maxOutputTokens": 2500, "temperature": 0.1}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/png", "data": b64}},
                    ]
                }
            ],
            "generationConfig": gen_config,
        },
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini Vision {r.status_code}: {r.text}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).strip()
    if json_mode:
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return text


def _find_local_file(query_name: str) -> Path | None:
    """Search Workspace, Desktop, Documents, and Downloads for a matching file or folder."""
    needle = query_name.strip().lower()
    if not needle:
        return None

    exact_matches = []
    fuzzy_matches = []

    for base in SEARCH_DIRS:
        if not base.exists():
            continue
        try:
            for p in base.rglob("*"):
                # Skip heavy hidden/venv/node_modules directories
                if any(part.startswith(".") or part in {"venv", "node_modules", "__pycache__"} for part in p.parts):
                    continue
                stem = p.stem.lower()
                name = p.name.lower()
                if needle in {stem, name}:
                    exact_matches.append(p)
                elif needle in name:
                    fuzzy_matches.append(p)
                if len(exact_matches) >= 3:
                    break
        except Exception:
            continue

    chosen = exact_matches[0] if exact_matches else (fuzzy_matches[0] if fuzzy_matches else None)
    return chosen


def _resolve_youtube_video_url(search_query: str) -> str:
    """Find the direct YouTube watch URL for a query (e.g. 'latest video from MrBeast')."""
    try:
        from ddgs import DDGS
        ddgs = DDGS()
        results = ddgs.videos(f"site:youtube.com {search_query}", max_results=5)
        for r in results or []:
            url = r.get("content") or r.get("url") or r.get("href") or ""
            if "youtube.com/watch" in url or "youtu.be/" in url:
                return url
        text_results = ddgs.text(f"site:youtube.com/watch {search_query}", max_results=5)
        for r in text_results or []:
            url = r.get("href") or r.get("url") or ""
            if "youtube.com/watch" in url:
                return url
    except Exception:
        pass
    # Fallback to YouTube search results page
    from urllib.parse import quote_plus
    return f"https://www.youtube.com/results?search_query={quote_plus(search_query)}"


def _open_path_on_os(target: Path) -> None:
    """Open a file or folder natively on Windows, macOS, or Linux."""
    if hasattr(os, "startfile"):
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif os.name == "posix":
        import subprocess
        cmd = "open" if "darwin" in os.uname().sysname.lower() else "xdg-open"
        subprocess.Popen([cmd, str(target)])


INTERACT_PROMPT = """You are controlling the user's laptop screen (mouse, keyboard, editor, browser, files).
Look closely at the screenshot and the USER GOAL:
"{goal}"

Coordinate system:
- x and y are normalized from 0 to 1000 (0,0 is top-left of screenshot; 1000,1000 is bottom-right).

Available action types (choose 1 to 4 sequential actions that accomplish the goal):
1. "click": {"type": "click", "x": 500, "y": 300, "button": "left", "clicks": 1, "reason": "..."}
2. "type_text": {"type": "type_text", "x": 500, "y": 300, "text": "...", "press_enter": true, "clear_first": false, "reason": "..."}
   (Note: x and y are optional if the input box is already focused; set x and y to null to type right where the cursor is.)
3. "hotkey": {"type": "hotkey", "keys": ["ctrl", "w"], "repeat": 1, "reason": "..."}
   Common hotkeys:
   - Close current browser tab: ["ctrl", "w"] (set "repeat" to N if closing N tabs)
   - Switch window: ["alt", "tab"]
   - Save file: ["ctrl", "s"]
   - Undo: ["ctrl", "z"]
4. "fix_code_in_editor": {
     "type": "fix_code_in_editor",
     "editor_x": 500,
     "editor_y": 500,
     "line_number": 14,
     "lines_to_select": 1,
     "replacement_code": "    return total + tax",
     "reason": "Fix the syntax/logic error on line 14 in VS Code"
   }
   Use this whenever the user asks to fix an error or edit code visible in VS Code or an editor on screen!
   Read the exact line number from the editor gutter in the screenshot, click inside the editor (editor_x, editor_y),
   and provide the exact replacement line(s) preserving indentation.
5. "open_youtube": {"type": "open_youtube", "query": "MrBeast latest video", "reason": "..."}
   Use when the user asks to open YouTube and play/search a video or channel.
6. "open_url": {"type": "open_url", "url": "https://...", "reason": "..."}
7. "find_and_open_file": {"type": "find_and_open_file", "filename": "math-unit1", "reason": "..."}
   Use when the user asks to search for a local file or folder and open it.
8. "scroll": {"type": "scroll", "amount": -500, "reason": "scroll down (-500) or up (500)"}
9. "wait": {"type": "wait", "seconds": 1.5, "reason": "..."}

Return ONLY valid JSON with this exact structure:
{
  "screen_observation": "1 short sentence describing what is currently visible on screen",
  "spoken_summary": "2 to 3 natural sentences telling the user what you found and the live actions you performed",
  "actions": [ ... ]
}"""


def _execute_gui_action(act: dict, left: int, top: int, width: int, height: int) -> str:
    """Execute a single planned action and return a human-readable log entry."""
    kind = (act.get("type") or "").strip().lower()
    reason = act.get("reason") or kind

    if kind == "click":
        rx, ry = _norm_to_screen(act.get("x", 500), act.get("y", 500), left, top, width, height)
        clicks = int(act.get("clicks", 1))
        button = act.get("button", "left")
        pyautogui.moveTo(rx, ry, duration=0.45)
        pyautogui.click(rx, ry, clicks=clicks, button=button)
        return f"Clicked ({rx}, {ry}) [{button} x{clicks}] — {reason}"

    if kind == "type_text":
        if act.get("x") is not None and act.get("y") is not None:
            rx, ry = _norm_to_screen(act["x"], act["y"], left, top, width, height)
            pyautogui.moveTo(rx, ry, duration=0.4)
            pyautogui.click(rx, ry)
            time.sleep(0.2)
        if act.get("clear_first"):
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
        text = str(act.get("text", ""))
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        if act.get("press_enter", True):
            pyautogui.press("enter")
        return f"Typed '{text}' — {reason}"

    if kind == "hotkey":
        keys = [str(k).lower() for k in (act.get("keys") or []) if k]
        repeat = max(1, min(10, int(act.get("repeat", 1))))
        for _ in range(repeat):
            pyautogui.hotkey(*keys)
            time.sleep(0.25)
        return f"Pressed hotkey {'+'.join(keys)} (x{repeat}) — {reason}"

    if kind == "fix_code_in_editor":
        ex = act.get("editor_x", 500)
        ey = act.get("editor_y", 500)
        rx, ry = _norm_to_screen(ex, ey, left, top, width, height)
        line_no = int(act.get("line_number", 1))
        num_lines = max(1, int(act.get("lines_to_select", 1)))
        replacement = str(act.get("replacement_code", ""))

        # 1. Focus the code editor window
        pyautogui.moveTo(rx, ry, duration=0.4)
        pyautogui.click(rx, ry)
        time.sleep(0.25)

        # 2. Jump to the exact line using Ctrl+G (works in VS Code, Notepad++, Sublime, etc.)
        pyautogui.hotkey("ctrl", "g")
        time.sleep(0.25)
        pyautogui.typewrite(str(line_no), interval=0.04)
        pyautogui.press("enter")
        time.sleep(0.25)

        # 3. Select the broken line(s) cleanly
        pyautogui.press("home")
        pyautogui.press("home")  # twice ensures column 0 even on wrapped/indented lines
        for _ in range(num_lines - 1):
            pyautogui.hotkey("shift", "down")
        pyautogui.hotkey("shift", "end")
        time.sleep(0.15)

        # 4. Paste the fixed code and save
        pyperclip.copy(replacement)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "s")
        return f"Fixed code at line {line_no} in editor and saved — {reason}"

    if kind == "open_youtube":
        query = str(act.get("query") or "trending").strip()
        url = _resolve_youtube_video_url(query)
        webbrowser.open(url)
        return f"Opened YouTube video for '{query}' ({url}) — {reason}"

    if kind == "open_url":
        url = str(act.get("url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened URL {url} — {reason}"

    if kind == "find_and_open_file":
        fname = str(act.get("filename") or "").strip()
        found = _find_local_file(fname)
        if not found:
            return f"File '{fname}' not found in Workspace, Desktop, Documents, or Downloads."
        _open_path_on_os(found)
        return f"Found and opened '{found}' — {reason}"

    if kind == "scroll":
        amt = int(act.get("amount", -500))
        pyautogui.scroll(amt)
        return f"Scrolled {amt} — {reason}"

    if kind == "wait":
        secs = max(0.2, min(5.0, float(act.get("seconds", 1.0))))
        time.sleep(secs)
        return f"Waited {secs}s — {reason}"

    return f"Skipped unknown action '{kind}'"


def ask_about_screen(question: str) -> str:
    """Look at the user's laptop screen right now and answer a question about it (READ-ONLY).
    Use when the user asks what is visible on screen, wants an error explained without editing,
    or wants numbers/tables transcribed from a chart or page on screen. Do NOT use this tool
    if the user wants you to click, type, fix code on screen, close tabs, or open apps/files."""
    png, *_ = _grab_screen()
    return _call_gemini_vision(png, question, json_mode=False)


def interact_with_screen(goal: str) -> dict:
    """Actively control the user's laptop screen (mouse clicks, typing, keyboard shortcuts,
    fixing code errors directly in VS Code, opening YouTube videos, closing browser tabs, or
    searching and opening local files). Use whenever the user wants Alexa+ to DO something on
    the screen or interact with an open app."""
    png, left, top, width, height = _grab_screen()
    prompt = INTERACT_PROMPT.replace("{goal}", goal)
    raw_json = _call_gemini_vision(png, prompt, json_mode=True)

    try:
        plan = json.loads(raw_json)
    except Exception as e:
        raise RuntimeError(f"Could not parse screen interaction plan: {e}") from e

    actions = plan.get("actions") or []
    executed_logs = []

    for act in actions[:5]:
        try:
            log_line = _execute_gui_action(act, left, top, width, height)
            executed_logs.append({"status": "ok", "detail": log_line, "action": act})
        except Exception as exc:
            executed_logs.append({"status": "error", "detail": f"{act.get('type')}: {exc}", "action": act})

    spoken = plan.get("spoken_summary") or "I completed the requested actions on your screen."
    return {
        "spoken_summary": spoken,
        "screen_observation": plan.get("screen_observation", ""),
        "actions_executed": executed_logs,
    }