"""automations: vetted, parameterized automations the agent can run.

write_script runs LLM-written code in a sandbox with NO network, so it cannot do git, calendars
or web APIs. This module covers that ground the safe way: every automation is FIXED code you can
read. The model only chooses WHICH automation to run and its parameters. The code then
  - validates every parameter (paths must stay inside allowed folders, API hosts are allow-listed),
  - keeps secrets (GitHub token, webhooks) in environment variables the model never sees,
  - and puts anything with side effects behind a two-step approval token: the first call only
    previews, the second call (with the token) runs exactly what was previewed.

MCP tools:  list_automations()   run_automation(name, params_json, approval_token)
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

_PROJECT = Path(__file__).resolve().parent.parent  # the folder that holds server.py
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))  # so analyze_data and pdf_utils are found from anywhere
try:  # read secrets from the project's .env (task scheduler runs have no terminal variables)
    from dotenv import load_dotenv

    load_dotenv(_PROJECT / ".env")
except ImportError:
    pass

from ..tools.analyze_data import WORKSPACE, _gemini_json, _gemini_text, _safe_path
from ..tools.pdf_utils import md_to_pdf

try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python
    ZoneInfo = None

# --- Config (environment variables, e.g. from your .env file) ----------------
DEV_ROOT = Path(os.environ.get("DEV_ROOT", Path.home() / "Projects")).expanduser().resolve()
USER_TZ = os.environ.get("USER_TZ", "Asia/Kolkata")
API_ALLOWLIST = {
    "api.open-meteo.com", "geocoding-api.open-meteo.com", "codeforces.com",
    "restcountries.com", "api.github.com",
} | {h.strip() for h in os.environ.get("API_ALLOWLIST", "").split(",") if h.strip()}
DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
APPROVAL_TTL_S = 600
# -----------------------------------------------------------------------------


# ----------------------------- shared helpers ---------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:50] or "item"


def _inside(path: Path, root: Path) -> Path:
    p = Path(path).expanduser().resolve()
    if not (p == root or p.is_relative_to(root)):
        raise ValueError(f"Access denied: {p} is outside {root}")
    return p


def _user_tz():
    if ZoneInfo is None:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(USER_TZ)
    except Exception:  # noqa: BLE001  (tzdata missing on Windows) -> use the system timezone
        return datetime.now().astimezone().tzinfo


def _http(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, timeout=20, **kw)


def _http_json(method: str, url: str, **kw):
    r = _http(method, url, **kw)
    if not r.ok:
        u = urlparse(url)
        raise RuntimeError(f"{u.netloc}{u.path} returned {r.status_code}: {r.text[:300]}")
    return r.json()


def _git(args: list[str], cwd: Path, extra_env: dict | None = None, timeout: int = 90) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[:400]}")
    return p.stdout.strip()


def _github_auth_env() -> dict:
    """Pass the token to git through the environment (never in the command line or .git/config)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set. Create a GitHub personal access token with "
                           "'repo' scope and put it in your .env file.")
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}"}


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# =============================== 1. DEV WORKFLOWS ===============================
TEMPLATES = ("python", "node", "empty")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_GITIGNORE = {
    "python": "__pycache__/\n*.pyc\n.venv/\nvenv/\n.env\n.pytest_cache/\ndist/\n*.egg-info/\n",
    "node": "node_modules/\n.env\ndist/\nnpm-debug.log*\n",
    "empty": ".env\n",
}
_MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


def _check_bootstrap(name: str, template: str) -> Path:
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError("name must be 1-60 characters: letters, digits, dot, dash or underscore")
    if template not in TEMPLATES:
        raise ValueError(f"template must be one of {TEMPLATES}")
    path = DEV_ROOT / name
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} already exists and is not empty")
    return path


def _scaffold(path: Path, name: str, description: str, template: str, license_mit: bool) -> list[str]:
    files = {
        "README.md": f"# {name}\n\n{description or 'Project description goes here.'}\n",
        ".gitignore": _GITIGNORE[template],
    }
    pkg = re.sub(r"\W", "_", name.lower())
    pkg = pkg if pkg[0].isalpha() else f"app_{pkg}"
    if template == "python":
        files.update({
            f"src/{pkg}/__init__.py": '"""Package."""\n',
            "tests/test_smoke.py": "def test_smoke():\n    assert True\n",
            "requirements.txt": "# add dependencies here\n",
        })
    elif template == "node":
        files.update({
            "package.json": json.dumps({"name": name.lower(), "version": "0.1.0", "main": "src/index.js",
                                        "scripts": {"start": "node src/index.js"}}, indent=2) + "\n",
            "src/index.js": "console.log('hello');\n",
        })
    if license_mit:
        try:
            holder = _git(["config", "user.name"], DEV_ROOT) or "Author"
        except Exception:  # noqa: BLE001
            holder = "Author"
        files["LICENSE"] = _MIT.format(year=datetime.now().year, holder=holder)
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return sorted(files)


def repo_bootstrap(name: str, description: str = "", template: str = "python", private: bool = True,
                   push: bool = True, license_mit: bool = True) -> dict:
    path = _check_bootstrap(name, template)
    auth = _github_auth_env() if push else {}  # fail early, before creating anything
    DEV_ROOT.mkdir(parents=True, exist_ok=True)
    created = _scaffold(path, name, description, template, license_mit)
    _git(["init"], path)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], path)
    _git(["add", "-A"], path)
    try:
        _git(["commit", "-m", "Initial commit"], path)
    except RuntimeError as e:
        if "identity" in str(e).lower() or "who you are" in str(e).lower():
            raise RuntimeError("Git does not know who you are. Run: git config --global user.name "
                               "\"Your Name\" and git config --global user.email you@example.com") from e
        raise
    result = {"local_path": str(path), "files_created": created, "branch": "main", "pushed": False}
    if push:
        repo = _http_json(
            "POST", "https://api.github.com/user/repos",
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            json={"name": name, "description": (description or "")[:300], "private": bool(private)},
        )
        result["github_url"] = repo.get("html_url")
        try:
            _git(["remote", "add", "origin", repo["clone_url"]], path)
            _git(["push", "-u", "origin", "main"], path, extra_env=auth, timeout=120)
            result["pushed"] = True
        except RuntimeError as e:
            raise RuntimeError(f"GitHub repo was created at {result['github_url']} but the push failed: {e}") from e
    return result


def _preview_bootstrap(p: dict) -> str:
    name, template = p.get("name", ""), p.get("template", "python")
    path = _check_bootstrap(name, template)
    text = f"Create the folder {path} with a {template} starter layout"
    text += " and an MIT license" if p.get("license_mit", True) else ""
    text += ", run git init and make a first commit"
    if p.get("push", True):
        vis = "PRIVATE" if p.get("private", True) else "PUBLIC (anyone can see it)"
        text += f", then create a {vis} GitHub repository named '{name}' on your account and push to it"
    return text + "."


def _find_repos(root: Path) -> list[Path]:
    if (root / ".git").exists():
        return [root]
    return [d for d in sorted(root.iterdir()) if d.is_dir() and (d / ".git").exists()][:30]


def _sync_root(folder: str) -> Path:
    root = DEV_ROOT / folder if folder else DEV_ROOT
    root = _inside(root, DEV_ROOT)
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist")
    return root


def repos_sync(folder: str = "", pull: bool = True, push: bool = False) -> dict:
    root = _sync_root(folder)
    report = []
    for d in _find_repos(root):
        info: dict = {"repo": d.name}
        try:
            info["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], d)
            try:
                _git(["fetch", "--prune"], d, timeout=90)
            except RuntimeError as e:
                info["fetch"] = f"failed ({str(e)[:120]})"
            dirty = bool(_git(["status", "--porcelain"], d))
            info["uncommitted_changes"] = dirty
            try:
                ahead, behind = (int(x) for x in _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], d).split())
            except RuntimeError:
                info["action"] = "no upstream branch to compare with"
                report.append(info)
                continue
            info.update(ahead=ahead, behind=behind)
            actions = []
            if behind and not ahead and not dirty and pull:
                _git(["pull", "--ff-only"], d)
                actions.append(f"pulled {behind} commit(s)")
            elif behind and (ahead or dirty):
                actions.append("not pulled: local changes or diverged history")
            elif behind:
                actions.append(f"{behind} behind (pull not requested)")
            if ahead and push:
                origin = _git(["remote", "get-url", "origin"], d)
                env = _github_auth_env() if origin.startswith("https://github.com/") and os.environ.get("GITHUB_TOKEN") else {}
                _git(["push"], d, extra_env=env, timeout=120)
                actions.append(f"pushed {ahead} commit(s)")
            elif ahead:
                actions.append(f"{ahead} ahead (push not requested)")
            info["action"] = "; ".join(actions) or "up to date"
        except Exception as e:  # noqa: BLE001  one broken repo must not stop the others
            info["error"] = str(e)[:200]
        report.append(info)
    if not report:
        raise RuntimeError(f"No git repositories found in {root}")
    return {"root": str(root), "repositories": report}


def _preview_sync(p: dict) -> str:
    root = _sync_root(p.get("folder", ""))
    n = len(_find_repos(root))
    text = f"Check {n} git repositor{'y' if n == 1 else 'ies'} in {root}: fetch updates"
    if p.get("pull", True):
        text += ", and fast-forward pull the ones that are behind and have no local changes"
    if p.get("push", False):
        text += ", and push commits that are ahead"
    return text + ". Nothing is force-pushed or overwritten."


# =============================== 2. CALENDAR & NOTES ============================
def _ics_text(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r\n", "\n").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """RFC 5545 line folding: max 75 octets per line, never splitting a UTF-8 character."""
    raw, parts, limit = line.encode("utf-8"), [], 75
    while len(raw) > limit:
        cut = limit
        while (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw, limit = raw[cut:], 74
    parts.append(raw.decode("utf-8"))
    return [parts[0]] + [" " + p for p in parts[1:]]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_ics(events: list[dict], name: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Alexa Laptop Assistant//EN",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{_ics_text(name)}"]
    stamp = _utc(datetime.now(timezone.utc))
    for e in events:
        uid = hashlib.sha1(f"{e['title']}|{e['start']}".encode()).hexdigest()[:24] + "@alexa-laptop"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            end = e.get("end") or e["start"]
            lines += [f"DTSTART;VALUE=DATE:{e['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{end + timedelta(days=1):%Y%m%d}"]
        else:
            end = e.get("end") or e["start"] + timedelta(hours=1)
            lines += [f"DTSTART:{_utc(e['start'])}", f"DTEND:{_utc(end)}"]
        lines.append(f"SUMMARY:{_ics_text(e['title'])}")
        if e.get("notes"):
            lines.append(f"DESCRIPTION:{_ics_text(e['notes'])}")
        if e.get("remind") is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder",
                      f"TRIGGER:-PT{int(e['remind'])}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"


def _save_ics(events: list[dict], label: str, calendar_name: str) -> dict:
    out = WORKSPACE / "calendar" / f"{_slug(label)}_{datetime.now():%Y%m%d_%H%M%S}.ics"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_build_ics(events, calendar_name).encode("utf-8"))
    def show(v):
        return v.isoformat(timespec="minutes") if isinstance(v, datetime) else str(v)
    return {
        "ics_path": str(out),
        "event_count": len(events),
        "events": [{"title": e["title"], "start": show(e["start"])} for e in events[:15]],
        "how_to_import": "Double-click the .ics file, or in Google Calendar use Settings > Import & export > Import.",
    }


EVENTS_PROMPT = """Extract calendar events from the TEXT below. Today is {today}; the user's timezone is {tz}.
Return ONLY JSON:
{"events": [{"title": "...", "start": "YYYY-MM-DD or YYYY-MM-DDTHH:MM", "end": "same format or null",
             "all_day": true, "notes": "...", "remind_minutes": 60}]}
Rules:
- Only events that are explicitly in the text. Never invent dates or times.
- A date without a time means all_day true. If the year is missing, use the next occurrence after today.
- notes: short extra detail (course, link, room) if present, else an empty string.
- remind_minutes: 60 for timed events, 1440 for all-day events."""


def _read_source_text(source: str) -> tuple[str, str]:
    s = source.strip()
    if "\n" not in s and len(s) < 300 and Path(s).suffix.lower() in {".csv", ".txt", ".md"}:
        p = _safe_path(s)
        if not p.exists():
            raise FileNotFoundError(f"'{s}' not found in the workspace")
        return p.read_text(encoding="utf-8", errors="ignore"), p.stem
    return s, "events"


def events_to_ics(source: str, calendar_name: str = "Imported events") -> dict:
    text, label = _read_source_text(source)
    prompt = (EVENTS_PROMPT.replace("{today}", datetime.now(_user_tz()).strftime("%A, %d %B %Y"))
              .replace("{tz}", USER_TZ) + "\n\nTEXT:\n" + text[:20000])
    tz, events, seen = _user_tz(), [], set()
    for r in _gemini_json(prompt).get("events") or []:
        try:
            title, s_raw = str(r["title"]).strip()[:200], str(r["start"]).strip()
            all_day = bool(r.get("all_day")) or len(s_raw) == 10
            if all_day:
                start = date.fromisoformat(s_raw[:10])
                end = date.fromisoformat(str(r["end"])[:10]) if r.get("end") else None
            else:
                start = datetime.fromisoformat(s_raw)
                start = start if start.tzinfo else start.replace(tzinfo=tz)
                end = datetime.fromisoformat(str(r["end"])) if r.get("end") else None
                end = end.replace(tzinfo=tz) if end and not end.tzinfo else end
            if (title, start) in seen:
                continue
            seen.add((title, start))
            events.append({"title": title, "start": start, "end": end, "all_day": all_day,
                           "notes": str(r.get("notes") or ""), "remind": r.get("remind_minutes")})
        except (KeyError, ValueError, TypeError):
            continue  # skip anything the model got wrong instead of writing a bad calendar
    if not events:
        raise RuntimeError("No dated events found in that text.")
    return _save_ics(events, label, calendar_name)


def codeforces_contests(days: int = 14) -> dict:
    days = max(1, min(int(days), 60))
    data = _http_json("GET", "https://codeforces.com/api/contest.list", params={"gym": "false"})
    now = time.time()
    events = []
    for c in data.get("result", []):
        start_ts = c.get("startTimeSeconds")
        if c.get("phase") == "BEFORE" and start_ts and now <= start_ts <= now + days * 86400:
            start = datetime.fromtimestamp(start_ts, timezone.utc)
            events.append({"title": c["name"], "start": start, "all_day": False,
                           "end": start + timedelta(seconds=c.get("durationSeconds", 7200)),
                           "notes": f"https://codeforces.com/contests/{c['id']}", "remind": 60})
    if not events:
        raise RuntimeError(f"No Codeforces contests start in the next {days} days.")
    events.sort(key=lambda e: e["start"])
    return _save_ics(events, "codeforces", "Codeforces contests")


def notes_compile(folder: str = "notes", date_str: str = "", summarize: bool = True, pdf: bool = False) -> dict:
    root = _inside(WORKSPACE / folder, WORKSPACE)
    if not root.exists():
        raise FileNotFoundError(f"Folder '{folder}' does not exist in the workspace")
    day = date_str or date.today().isoformat()
    date.fromisoformat(day)  # validates the format
    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt"}
         and "daily" not in p.relative_to(root).parts
         and datetime.fromtimestamp(p.stat().st_mtime).date().isoformat() == day),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        raise RuntimeError(f"No notes in '{folder}' were saved on {day}.")
    parts = [(p, p.read_text(encoding="utf-8", errors="ignore")[:8000]) for p in files]
    body = "\n\n".join(f"### {p.relative_to(root)}\n\n{t.strip()}" for p, t in parts)
    index = "\n".join(f"- {p.relative_to(root)} ({datetime.fromtimestamp(p.stat().st_mtime):%H:%M})" for p, _ in parts)
    summary = ""
    if summarize:
        try:
            summary = _gemini_text(
                "Summarize these notes using ONLY their content. Output markdown with two parts: "
                "'Key points' (max 6 bullets) and 'Open questions and TODOs' (max 5 bullets).\n\n" + body[:20000],
                max_tokens=2000)
        except Exception:  # noqa: BLE001  the merged file is still useful without a summary
            summary = ""
    md = f"# Daily review - {day}\n\n" + (f"## Summary\n\n{summary}\n\n" if summary else "") \
         + f"## Contents\n\n{index}\n\n## Notes\n\n{body}\n"
    out = root / "daily" / f"{day}_master.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    result = {"master_file": str(out), "notes_merged": len(files), "summary_included": bool(summary)}
    if pdf:
        result["pdf"] = str(md_to_pdf(md, out.with_suffix(".pdf"), title=f"Daily review {day}"))
    return result


# =============================== 3. APIs & WEBHOOKS =============================
def notify(message: str, title: str = "") -> dict:
    text = ((f"**{title}**\n" if title else "") + message.strip())[:1800]
    sent = []
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        if (urlparse(hook).hostname or "") not in DISCORD_HOSTS:
            raise ValueError("DISCORD_WEBHOOK_URL must point to discord.com")
        r = _http("POST", hook, json={"content": text})
        if not r.ok:
            raise RuntimeError(f"Discord returned {r.status_code}")
        sent.append("discord")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        r = _http("POST", f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": text.replace("**", "")})
        if not r.ok:  # do not echo the URL: it contains the bot token
            raise RuntimeError(f"Telegram returned {r.status_code}")
        sent.append("telegram")
    if not sent:
        raise RuntimeError("No notification channel is configured. Set DISCORD_WEBHOOK_URL, or "
                           "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, in your .env file.")
    return {"sent_to": sent, "characters": len(text)}


def api_snapshot(url: str, name: str = "") -> dict:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "") not in API_ALLOWLIST:
        raise ValueError(f"Host not allowed: {u.hostname}. Allowed: {sorted(API_ALLOWLIST)}")
    r = _http("GET", url)
    if not r.ok:
        raise RuntimeError(f"{u.hostname} returned {r.status_code}")
    if len(r.content) > 2_000_000:
            raise RuntimeError("Response is larger than 2 MB")
    slug_source = str(name or u.hostname or "api")
    out = WORKSPACE / "data" / f"{_slug(slug_source)}_{datetime.now():%Y%m%d_%H%M%S}.json"
    _write_json(out, {"fetched_at": datetime.now().isoformat(timespec="seconds"), "source": url, "data": r.json()})
    return {"saved_to": str(out), "bytes": len(r.content)}


def weather_snapshot(city: str, days: int = 3) -> dict:
    city, days = city.strip()[:80], max(1, min(int(days), 7))
    if not city:
        raise ValueError("city is required")
    geo = _http_json("GET", "https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1})
    if not geo.get("results"):
        raise RuntimeError(f"City not found: {city}")
    g = geo["results"][0]
    fc = _http_json("GET", "https://api.open-meteo.com/v1/forecast", params={
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": days,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"})
    out = WORKSPACE / "data" / "weather" / f"{_slug(city)}_{date.today().isoformat()}.json"
    _write_json(out, {"city": g.get("name", city), "country": g.get("country"), "latitude": g["latitude"],
                      "longitude": g["longitude"], "fetched_at": datetime.now().isoformat(timespec="seconds"),
                      "forecast": fc})
    d = fc.get("daily", {})
    lines = [f"{t}: {lo} to {hi} C, rain {rain} mm" for t, lo, hi, rain in zip(
        d.get("time", []), d.get("temperature_2m_min", []), d.get("temperature_2m_max", []),
        d.get("precipitation_sum", []))]
    return {"saved_to": str(out), "city": g.get("name", city), "forecast": lines}


# =============================== registry & MCP tools ===========================
EXTRA_INSTRUCTIONS = """
OPTIONAL TOOLS (automations)
- For chores that need git, GitHub, the internet, calendars, notes or messaging (start a project and GitHub
  repo, sync repositories, turn deadlines or Codeforces contests into a calendar file, compile daily notes,
  send a Discord/Telegram message, save weather or API data): call run_automation. Call list_automations
  first if you are unsure which one fits.
- If run_automation returns status needs_approval, tell the user in plain words what will happen, ask for a
  clear yes, and only after a yes in a LATER message call run_automation again with the same name,
  params_json and the approval_token.
- write_script has no internet and cannot use git: use it only for file chores.
"""

@dataclass
class Automation:
    fn: Callable
    summary: str
    params: str
    needs_approval: Callable[[dict], bool] = lambda p: False
    preview: Callable[[dict], str] | None = None
    setup: str = ""
    ready: Callable[[], bool] = lambda: True


AUTOMATIONS: dict[str, Automation] = {
    "repo_bootstrap": Automation(
        repo_bootstrap,
        "Start a new project: create the folder with starter files and a license, git init, first commit, "
        "create the GitHub repository and push.",
        "name (required), description, template (python|node|empty), private (default true), "
        "push (default true), license_mit (default true)",
        needs_approval=lambda p: bool(p.get("push", True)), preview=_preview_bootstrap,
        setup="GITHUB_TOKEN with 'repo' scope (only needed when push is true)",
        ready=lambda: bool(os.environ.get("GITHUB_TOKEN"))),
    "repos_sync": Automation(
        repos_sync,
        "Check every git repository in your projects folder: fetch, report ahead/behind/uncommitted, "
        "fast-forward pull clean ones, optionally push.",
        "folder (optional subfolder of the projects folder), pull (default true), push (default false)",
        needs_approval=lambda p: bool(p.get("pull", True)) or bool(p.get("push", False)), preview=_preview_sync),
    "events_to_ics": Automation(
        events_to_ics,
        "Turn deadlines, exam schedules or any dated list (file in the workspace, or pasted text) into a "
        "calendar .ics file that imports into Google Calendar.",
        "source (file name in workspace or the text itself), calendar_name"),
    "codeforces_contests": Automation(
        codeforces_contests,
        "Create a calendar .ics file with upcoming Codeforces contests (with reminders).",
        "days (default 14, max 60)"),
    "notes_compile": Automation(
        notes_compile,
        "Merge the notes saved in a workspace folder on one day into a structured daily master review file "
        "(optional AI summary and PDF).",
        "folder (default notes), date_str (YYYY-MM-DD, default today), summarize (default true), pdf (default false)"),
    "notify": Automation(
        notify,
        "Send a short message to your Discord or Telegram (for example when a long task finishes).",
        "message (required), title",
        setup="DISCORD_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
        ready=lambda: bool(os.environ.get("DISCORD_WEBHOOK_URL") or
                           (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")))),
    "weather_snapshot": Automation(
        weather_snapshot,
        "Fetch a weather forecast for a city from Open-Meteo and save it as a JSON dataset.",
        "city (required), days (1-7, default 3)"),
    "api_snapshot": Automation(
        api_snapshot,
        "Fetch a JSON response from an allow-listed public API and save it as a dataset file.",
        "url (https, host must be allow-listed), name"),
}

_PENDING: dict[str, tuple[str, str, float]] = {}  # token -> (automation, params hash, expires at)


def _params_hash(name: str, params: dict) -> str:
    return hashlib.sha1(f"{name}|{json.dumps(params, sort_keys=True, default=str)}".encode()).hexdigest()


def _coerce(fn: Callable, params: dict) -> dict:
    """Models often send 'true' or '7' as text; convert to the type the function expects."""
    sig, out = inspect.signature(fn), dict(params)
    for k, v in params.items():
        default = sig.parameters[k].default if k in sig.parameters else inspect.Parameter.empty
        if isinstance(default, bool) and isinstance(v, str):
            out[k] = v.strip().lower() in {"true", "yes", "1"}
        elif isinstance(default, int) and not isinstance(default, bool) and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            out[k] = int(v)
    return out


def _short(x, n: int = 600) -> str:
    s = json.dumps(x, default=str) if not isinstance(x, str) else x
    return s if len(s) <= n else s[:n] + " ..."


def execute(name: str, params: dict, notify_when_done: bool = False) -> dict:
    """Run an automation (no approval check; callers decide who has approved)."""
    try:
        result = AUTOMATIONS[name].fn(**params)
    except Exception as e:  # noqa: BLE001
        if notify_when_done:
            _quiet_notify(f"{name} FAILED: {e}")
        raise
    if notify_when_done:
        _quiet_notify(f"{name} finished: {_short(result, 400)}")
    return {"status": "done", "automation": name, "result": result}


def _quiet_notify(text: str) -> None:
    try:
        notify(text, title="Laptop assistant")
    except Exception:  # noqa: BLE001  a failed notification must never hide the real result
        pass


def list_automations() -> list[dict]:
    """List the automations you can run with run_automation: starting projects and GitHub repos,
    syncing git repositories, making calendar files from deadlines or Codeforces contests, compiling
    daily notes, sending Discord/Telegram messages, and saving weather or public API data. Use this
    when the user asks what you can automate or you are unsure which one fits."""
    return [{"name": n, "summary": a.summary, "params": a.params,
             "ready": a.ready(), "setup_needed": "" if a.ready() else a.setup} for n, a in AUTOMATIONS.items()]


def run_automation(name: str, params_json: str = "{}", approval_token: str = "", notify_when_done: bool = False) -> dict:
    """Run one automation from list_automations. `params_json` is a JSON object written as text, for
    example '{"city": "Vijayawada", "days": 3}'. Some automations change things outside the laptop
    (create GitHub repos, push or pull code). For those, the first call returns status 'needs_approval'
    with what_will_happen and an approval_token: tell the user in plain words what will happen and ask
    for a clear yes. Only after they say yes in a LATER message, call run_automation again with the SAME
    name and params_json plus that approval_token. Set notify_when_done to true to also send the result to
    the user's Discord/Telegram when it finishes."""
    if name not in AUTOMATIONS:
        raise ValueError(f"Unknown automation '{name}'. Available: {sorted(AUTOMATIONS)}")
    auto = AUTOMATIONS[name]
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"params_json is not valid JSON: {e}") from e
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON object")
    params = _coerce(auto.fn, params)
    try:
        inspect.signature(auto.fn).bind(**params)
    except TypeError as e:
        raise ValueError(f"Wrong parameters for {name}: {e}. Expected: {auto.params}") from e

    now = time.time()
    for t in [t for t, (_, _, exp) in _PENDING.items() if exp < now]:
        _PENDING.pop(t)

    if auto.needs_approval(params):
        if not approval_token:
            what = auto.preview(params) if auto.preview else f"{auto.summary} Parameters: {params}"
            token = secrets.token_hex(4)
            _PENDING[token] = (name, _params_hash(name, params), now + APPROVAL_TTL_S)
            return {"status": "needs_approval", "automation": name, "what_will_happen": what,
                    "approval_token": token,
                    "instructions": "Tell the user what will happen and ask for a clear yes. After a yes in a later "
                                    "message, call run_automation again with the same name, params_json and this token."}
        pending = _PENDING.pop(approval_token, None)
        if not pending or pending[0] != name or pending[1] != _params_hash(name, params) or pending[2] < now:
            raise ValueError("Approval token is invalid, expired, already used, or the parameters changed. "
                             "Call run_automation without a token to get a new preview.")
    return execute(name, params, notify_when_done)