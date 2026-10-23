# Alexa+ Laptop Assistant

**An MCP server that gives an Alexa+-style voice assistant safe hands on your laptop.**

Alexa+ can order groceries and book rides, but it cannot look at your screen, fix the bug in your editor, organize a folder, or build a report from your data. This server adds those abilities, with approval before anything changes, a rehearsal on a copy, and undo.

* **Hackathon Track:** **Alexa+** (self-hosted MCP server, Streamable HTTP, MCP spec 2025-11-25) with the **simulated Alexa+ experience** (a web app and a terminal agent that talk to the server).

---

## What it can do

| Say something like... | Tool(s) Used | What happens |
| :--- | :--- | :--- |
| **"Look at the error on my screen and fix it"** | `interact_with_screen`, `approve_screen_plan` | Reads the error, finds the file, shows a **diff**, waits for your yes, and edits the file. Saying *"Undo that"* restores it. |
| **"Organize my Documents folder by file type"** | `write_script`, `run_script`, `undo_last_script` | Finds the folder by name, **rehearses on a copy**, shows exactly what would move, and waits for yes. In your own folders, it can only **move, rename, and create** (never delete). Undo is exact. |
| **"From the chart on my screen, run a deep statistical analysis"** | `ask_about_screen`, `analyze_data` | Reads the numbers, computes real statistics with pandas, explains them, and saves a PDF report. |
| **"Research the Indian EV market leaders and build a report"** | `research_topic` | Plans several searches, collects numbers, analyzes them, and saves both a research PDF and an analysis PDF. |
| **"Open that file" / "Find files like zenitsu"** | `open_item` | Opens or lists documents, images, and folders by name (never executable programs). |
| **"Analyze this csv" (any path)** | `analyze_data` | Read-only analysis of `.csv`, `.xlsx`, `.json`, `.txt`, or `.md` files anywhere on your computer. |

---

## How it fits together

```text
 Web app (client/Simulated_frontend) --HTTP--> FastAPI backend (client/Simulated_backend/main.py) --+
 Terminal (client/Simulated_backend/agent.py) ------------------------------------------------------+
                                                       │   LangGraph agent + Gemini
                                                       ▼   (the "Alexa+" stand-in: decides which tool to call)
                                    MCP over Streamable HTTP  http://127.0.0.1:8000/mcp
                                                       │
                                       MCP server (server.py) on the laptop
        tools: screen control | open_item | analyze_data | research_topic | write_script / run_script / undo
```

* **The server contains tools only, no agent loop.** The agent that chooses tools lives in the client (`client/Simulated_backend/agent.py` and `client/Simulated_backend/main.py`, built with `langchain-mcp-adapters`). Any MCP client can use the server (e.g., MCP Inspector), and the real Alexa+ would take the agent's place.
* Some tools call **Gemini** inside their own implementation (vision for screen reading, interpreting computed statistics, planning searches, writing scripts, proposing a code fix). These are single calls, not autonomous agents.
* The server rules for combining tools are sent to every client dynamically in the MCP `instructions` field (`instructions.py`).

---

## Safety Model

* **Risk is decided by code, not by the model.** Opening links or scrolling runs instantly. Clicking, typing, keyboard shortcuts, and code edits wait for an explicit yes (`approve_screen_plan`).
* **Screen text is untrusted data.** The assistant's own chat window is blacked out in screenshots, and any click that would land in it is automatically refused.
* Keyboard shortcuts come from a strict allow-list. Typing into terminals and security prompts is blocked.
* Before each click, the area around the target is compared with the screenshot the plan was made from (`ImageChops.difference`).
* Scripts are **rehearsed on a copy** first and run inside a restricted **sandbox** (no network, no subprocesses, writes only inside the approved folder). In user folders: move, rename, and create only. Every run can be undone.
* **Never allowed:** Drive roots, your entire user folder, Windows/Program Files/AppData, `.ssh`, `.aws`.
* Only documents, images, and folders can be opened by name—never `.exe`, `.bat`, `.py`, `.js`, or `.lnk`.
* Code fixes are edits on disk featuring a visible diff, a syntax check, and an automatic backup. Every screen action is logged to `AlexaWorkspace/logs/screen_actions.jsonl`.
* **Privacy:** Screenshots and files you ask it to analyze are sent to the Google Gemini API.

---

## Requirements

* **Python 3.11+** and a **Gemini API key** (free at [Google AI Studio](https://aistudio.google.com/apikey)).
* **Windows 10/11** is the tested platform (window detection and chat-window hiding use the Windows API). The rest works on macOS and Linux (screen actions require a normal desktop session).
* **Node.js 18+** for the web app (optional; the terminal agent needs no Node).

---

## ⚙️ Setup

The server and the client require **two separate Python environments** because their MCP package versions conflict.

```bash
# 1) Server environment
python -m venv .venv-server
.venv-server\Scripts\activate            # macOS/Linux: source .venv-server/bin/activate
pip install -r requirements-server.txt

# 2) Client environment (agent + web backend)
python -m venv .venv-client
.venv-client\Scripts\activate            # macOS/Linux: source .venv-client/bin/activate
pip install -r requirements-client.txt

# 3) Your key
copy .env.example .env                   # macOS/Linux: cp .env.example .env   then put your key in .env
```

---

## Run the System

### Terminal 1: The MCP Server (Server Environment)
```bash
python server.py
```
*Serves MCP Streamable HTTP at `http://127.0.0.1:8000/mcp`.*

### Terminal 2 (Option A): The Web App: Recommended
```bash
cd client/Simulated_backend
python -m uvicorn main:app --port 8001   # client environment
```
And in a separate terminal:
```bash
cd client/Simulated_frontend && npm install && npm run dev
```
*Open the page the dev server prints. Use the Voice mode in frontend and exit the simulation app, so you can ask about screen, going to other apps, while the Alexa hears you lively. Do not maximize the chat window and ask about screen as it will be completely black for model, and The page title must be `Alexa+ Simulation` (it is how the assistant hides its own window); if you change it, set `ASSISTANT_UI_TITLE` in `.env`.*

### Terminal 2 (Option B): Terminal Agent (Client Environment)
```bash
cd client/Simulated_backend
python agent.py
```

### Terminal 2 (Option C): No Agent (Proves Standalone MCP Server)
```bash
npx @modelcontextprotocol/inspector     # connect to http://127.0.0.1:8000/mcp (Streamable HTTP)
```

---

## Try These Commands

1. Open a file that has a bug in VS Code, then: *"Look at the error in my code on screen and fix it."* Say *"yes"* to apply, then *"undo that"*.
2. *"Organize my Documents folder into subfolders by file type."* Read the preview, say *"yes"*, then *"undo"*.
3. Show a chart or table on screen: *"From the chart on my screen, run a deep statistical analysis. Then open the analysis file."*
4. *"Research the current Indian EV market leaders, compare their latest numbers and risks, and build a report. Open the file."*
5. *"Find every csv file in my workspace"*, then *"analyze the first one"*.
6. **"Open YouTube and play the latest video from MrBeast."** 
7. **"Analyze performance_metrics.xlsx in my workspace, focusing specifically on the revenue column."**
8. **"Research the top 5 smartphone models in India right now, compare their prices, and highlight any potential risks or downsides."**
9. **"Find my biology assignment folder and open it."**
10. **"Look at the error message on my terminal screen right now and explain what is causing it."**
11. *"What is my git status right now."*
    
    and more...play around with it.
    **With extra_productivity(folder) tools enabled, You can expect more automated skills.
    Its Disabled by default to reduce set-up time. To switch that on, go through readme in extra_productivity folder.**
## ⚙️ Settings (`.env`)

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `GOOGLE_API_KEY` | *(required)* | Google AI Studio key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | model for the tools and the agent |
| `MCP_WORKSPACE` | `~/AlexaWorkspace` | where reports, scripts, backups and logs live |
| `MCP_URL` | `http://127.0.0.1:8000/mcp` | where the client finds the server |
| `USER_LOCATION`, `SEARCH_REGION` | `India`, `in-en` | research results are localized. Change for your country, for example `United States` and `us-en` |
| `ASSISTANT_UI_TITLE` | `alexa\+ simulation` | regex matching the chat window title |
| `FRONTEND_ORIGINS` | localhost 5173 and 3000 | web origins allowed by the backend |

---

## Known Limits

* Real Alexa+ connection is not part of this demo. Registering the server as an Alexa+ add-on needs an Alexa developer account, the `alexa-ai` CLI and a public HTTPS URL, and a production version would use a small companion app on the laptop that connects outward to the cloud. The MCP server itself does not change.
* Rehearsal of chores in your own folders uses empty placeholder files, so tasks that depend on file contents or sizes cannot be rehearsed exactly. The result reports whether the real run matched the preview.
* Web search uses DuckDuckGo snippets, so research reports are only as deep as the snippets. Gemini and DuckDuckGo can rate-limit; the tools retry once or twice.
* Moving the mouse to the top-left screen corner aborts any screen action (PyAutoGUI fail-safe).

---

## Optional Extras

`extra_productivity/` holds optional git, calendar, notes, notification and web-data automations. They increase productivity incredibly. I set them as optional as they require a bit more setup, which takes some time. They are off by default. See `extra_productivity/README.md` to switch them on.
