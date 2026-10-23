Alexa+ Laptop AssistantAn MCP server that gives an Alexa+-style voice assistant safe hands on your laptop.
Alexa+ can order groceries and book rides, but it cannot look at your screen, fix the bug in your editor,
organize a folder, or build a report from your data. This server adds those abilities, with approval before
anything changes, a rehearsal on a copy, and undo.Hackathon track: Alexa+ (self-hosted MCP server, Streamable HTTP, MCP spec 2025-11-25) with the
simulated Alexa+ experience (a web app and a terminal agent that talk to the server).What it can doSay something likeTool(s)What happens"Look at the error on my screen and fix it"interact_with_screen, approve_screen_planReads the error, finds the file, shows a diff, waits for yes, edits the file. "Undo that" restores it."Organize my Documents folder by file type"write_script, run_script, undo_last_scriptFinds the folder by name, rehearses on a copy, shows exactly what would move, waits for yes. In your own folders it can only move, rename and create (never delete). Undo is exact."From the chart on my screen, run a deep statistical analysis"ask_about_screen, analyze_dataReads the numbers, computes real statistics with pandas, explains them, saves a PDF."Research the Indian EV market leaders and build a report"research_topicPlans several searches, collects numbers, analyzes them, saves a research PDF and an analysis PDF."Open that file" / "find files like zenitsu" / "every csv in Documents"open_itemOpens or lists documents, images and folders by name (never programs)."Analyze this csv" (any path)analyze_dataRead-only analysis of csv, xlsx, json, txt or md files anywhere on the computer.How it fits together Web app (client/Simulated_frontend) --HTTP--> FastAPI backend (client/Simulated_backend/main.py) --+
 Terminal (client/Simulated_backend/agent.py) ------------------------------------------------------+
                                                       |   LangGraph agent + Gemini
                                                       v   (the "Alexa+" stand-in: decides which tool to call)
                                    MCP over Streamable HTTP  http://127.0.0.1:8000/mcp
                                                       |
                                       MCP server (server.py) on the laptop
        tools: screen control | open_item | analyze_data | research_topic | write_script / run_script / undo
The server contains tools only, no agent loop. The agent that chooses tools lives in the client
(client/Simulated_backend/agent.py and client/Simulated_backend/main.py, built with langchain-mcp-adapters).
Any MCP client can use the server, for example MCP Inspector, and the real Alexa+ would take the agent's place.Some tools call Gemini inside their own implementation (vision for screen reading, interpreting
computed statistics, planning searches, writing scripts, proposing a code fix). These are single calls,
not agents.The server rules for combining tools are sent to every client in the MCP instructions field
(instructions.py).Safety modelRisk is decided by code, not by the model. Opening links or scrolling runs at once. Clicking, typing,
shortcuts and code edits wait for an explicit yes (approve_screen_plan).Screen text is untrusted data. The assistant's own chat window is blacked out in screenshots, and a
click that would land in it is refused.Keyboard shortcuts come from a short allow-list. Typing into terminals and security prompts is refused.Before each click, the area around it is compared with the screenshot the plan was made from.Scripts are rehearsed on a copy first, and run in a sandbox (no network, no subprocess, writes only
inside the one approved folder). In user folders: move, rename and create only. Every run can be undone.Never allowed: drive roots, your whole user folder, Windows/Program Files/AppData, .ssh/.aws.Only documents, images and folders can be opened by name, never .exe, .bat, .py, .js, .lnk.Code fixes are edits on disk with a visible diff, a syntax check and a backup. Every screen action is
written to AlexaWorkspace/logs/screen_actions.jsonl.Privacy: screenshots and the files you ask it to analyze are sent to the Google Gemini API.RequirementsPython 3.11+, a Gemini API key (free at https://aistudio.google.com/apikey), internet accessWindows 10/11 is the tested platform (window detection and chat-window hiding use the Windows API).
The rest works on macOS and Linux. Screen actions need a normal desktop session.Node.js 18+ for the web app (optional, the terminal agent needs no Node)SetupThe server and the client need two separate Python environments (their MCP package versions conflict).# 1) Server environment
python -m venv .venv-server
.venv-server\Scripts\activate            # macOS/Linux: source .venv-server/bin/activate
pip install -r requirements-server.txt

# 2) Client environment (agent + web backend)
python -m venv .venv-client
.venv-client\Scripts\activate            # macOS/Linux: source .venv-client/bin/activate
pip install -r requirements-client.txt

# 3) Your key
copy .env.example .env                   # macOS/Linux: cp .env.example .env   then put your key in .env
RunTerminal 1: the MCP server (server environment)python server.py
It serves http://127.0.0.1:8000/mcp.Terminal 2, option A: terminal agent (client environment)cd client/Simulated_backend
python agent.py
Option B: the web appcd client/Simulated_backend
python -m uvicorn main:app --port 8001   # client environment
And in a separate terminal:cd client/Simulated_frontend && npm install && npm run dev
Open the page the dev server prints. Use the Voice mode in frontend and exit the simulation app, so you can ask about screen, going to other apps, while the Alexa hears you lively. Do not maximize the chat window and ask about screen as it will be completely black for model , and The page title must be Alexa+ Simulation (it is how the assistant
hides its own window); if you change it, set ASSISTANT_UI_TITLE in .env.Option C: no agent at all (proves it is a standalone MCP server)npx @modelcontextprotocol/inspector     # connect to http://127.0.0.1:8000/mcp (Streamable HTTP)
Try theseOpen a file that has a bug in VS Code, then: "Look at the error in my code on screen and fix it."
Say "yes" to apply, then "undo that"."Organize my Documents folder into subfolders by file type." Read the preview, say "yes", then "undo".Show a chart or table on screen: "From the chart on my screen, run a deep statistical analysis. Then open the analysis file.""Research the current Indian EV market leaders, compare their latest numbers and risks, and build a report. Open the file.""Find every csv file in my workspace", then "analyze the first one".Settings (.env)VariableDefaultMeaningGOOGLE_API_KEY / GEMINI_API_KEY(required)Google AI Studio keyGEMINI_MODELgemini-2.5-flashmodel for the tools and the agentMCP_WORKSPACE~/AlexaWorkspacewhere reports, scripts, backups and logs liveMCP_URLhttp://127.0.0.1:8000/mcpwhere the client finds the serverUSER_LOCATION, SEARCH_REGIONIndia, in-enresearch results are localized. Change for your country, for example United States and us-enASSISTANT_UI_TITLEalexa\+ simulationregex matching the chat window titleFRONTEND_ORIGINSlocalhost 5173 and 3000web origins allowed by the backendKnown limitsReal Alexa+ connection is not part of this demo. Registering the server as an Alexa+ add-on needs an
Alexa developer account, the alexa-ai CLI and a public HTTPS URL, and a production version would use a
small companion app on the laptop that connects outward to the cloud. The MCP server itself does not change.Rehearsal of chores in your own folders uses empty placeholder files, so tasks that depend on file
contents or sizes cannot be rehearsed exactly. The result reports whether the real run matched the preview.Web search uses DuckDuckGo snippets, so research reports are only as deep as the snippets. Gemini and
DuckDuckGo can rate-limit; the tools retry once or twice.Moving the mouse to the top-left screen corner aborts any screen action (PyAutoGUI fail-safe).Optional extrasextra_productivity/ holds optional git, calendar, notes, notification and web-data automations. They increase productivity incredibly. I set them as optional as they require a bit more setup, which takes some time. They are off by
default. See extras/README.md to switch them on.