"""FastAPI backend for the simulated Alexa+ web app.

It wraps the same LangGraph agent as agent.py, over HTTP:
  POST /chat     {message} -> {response, steps, artifacts}
  POST /reset    forget the conversation
  POST /connect  (re)connect to the MCP server (use it after restarting server.py)
  GET  /status   connection info
  GET  /pdf?path=...   serve a PDF from the AlexaWorkspace folder

Run:  uvicorn main:app --reload --port 8001
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

from client.Simulated_backend.agent import (
    BASE_PROMPT,
    MCP_URL,
    MODEL,
    as_text,
    build_agent,
    fetch_server_instructions,
    short,
)

WORKSPACE = Path(os.environ.get("MCP_WORKSPACE", Path.home() / "AlexaWorkspace")).resolve()
MAX_HISTORY = 60  # messages kept in memory (cut only at the start of a user turn)
ORIGINS = [o.strip() for o in os.environ.get(
    "FRONTEND_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
).split(",") if o.strip()]

agent = None
mcp_client = None
loaded_tools: list = []
connect_error = ""
history: list = []  # EVERY message: user, AI tool calls, tool results (script_id, plan_id...), AI answers
lock = asyncio.Lock()  # one chat turn at a time, so two requests cannot interleave in the history


def parse_tool_json(content):
    """Return the tool output as a dict when it is JSON, else None."""
    try:
        data = json.loads(as_text(content).strip())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def connect() -> None:
    """Connect to the MCP server and build the agent. Safe to call again at any time."""
    global agent, mcp_client, loaded_tools, connect_error
    try:
        print(f"Connecting to MCP server at {MCP_URL}...")
        rules = await fetch_server_instructions()
        mcp_client = MultiServerMCPClient({"laptop": {"url": MCP_URL, "transport": "streamable_http"}})
        tools = await mcp_client.get_tools()
        key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY is not set (put it in your .env file)")
        llm = ChatGoogleGenerativeAI(model=MODEL, google_api_key=key, temperature=0)
        agent = build_agent(llm, tools, f"{BASE_PROMPT}\n\nSERVER INSTRUCTIONS:\n{rules}")
        loaded_tools, connect_error = [t.name for t in tools], ""
        print(f"Connected! Tools: {loaded_tools}")
    except Exception as e:  # noqa: BLE001
        agent, loaded_tools, connect_error = None, [], str(e)
        print(f"Could not connect to the MCP server: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect()
    yield


app = FastAPI(title="Alexa+ Simulation Backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str


def trim_history() -> None:
    """Keep the last MAX_HISTORY messages, cutting only at a user message so every tool call
    stays paired with its result (Gemini rejects a history that starts mid-way)."""
    global history
    if len(history) <= MAX_HISTORY:
        return
    for i in range(len(history) - MAX_HISTORY, len(history)):
        if isinstance(history[i], HumanMessage):
            history = history[i:]
            return


@app.get("/status")
async def get_status():
    return {"connected": agent is not None, "mcp_url": MCP_URL, "tools": loaded_tools,
            "workspace": str(WORKSPACE), "error": connect_error, "history_messages": len(history)}


@app.post("/connect")
async def reconnect():
    await connect()
    return await get_status()


@app.post("/reset")
async def reset_history():
    global history
    history = []
    return {"status": "cleared"}


@app.get("/pdf")
async def serve_pdf(path: str = Query(..., description="Absolute path to a PDF inside AlexaWorkspace")):
    """Serves generated PDFs from AlexaWorkspace so the web app can open them in a tab."""
    file_path = Path(path).resolve()
    if not file_path.is_relative_to(WORKSPACE) or file_path.suffix.lower() != ".pdf" or not file_path.is_file():
        raise HTTPException(status_code=404, detail="PDF not found inside AlexaWorkspace")
    return FileResponse(file_path, media_type="application/pdf", filename=file_path.name)


@app.post("/chat")
async def chat(request: ChatRequest):
    if agent is None:
        await connect()  # the MCP server may have been started after this backend
    if agent is None:
        raise HTTPException(status_code=503, detail=f"Agent not connected. Is server.py running? ({connect_error})")

    async with lock:
        human = HumanMessage(request.message)
        new_messages, steps, artifacts, final = [], [], [], ""
        try:
            async for update in agent.astream({"messages": history + [human]}, stream_mode="updates"):
                for data in update.values():
                    for m in (data or {}).get("messages", []):
                        new_messages.append(m)
                        for call in getattr(m, "tool_calls", None) or []:
                            args = call.get("args", {})
                            steps.append({"type": "call", "tool": call["name"], "args": args, "summary": short(args)})
                            print(f"  -> {call['name']}({short(args)})")
                        if m.type == "tool":
                            text = as_text(m.content)
                            steps.append({"type": "result", "tool": m.name, "summary": short(text)})
                            artifacts.append({"tool": m.name, "data": parse_tool_json(m.content) or {"text": text}})
                            print(f"  <- {m.name}: {short(text)}")
                        elif m.type == "ai" and m.content and not getattr(m, "tool_calls", None):
                            final = as_text(m.content)
        except Exception as e:  # noqa: BLE001  e.g. Gemini 503: drop the half-finished turn, keep the history clean
            print(f"Chat turn failed: {e}")
            return {"response": f"Sorry, something went wrong: {e}", "error": str(e), "steps": steps, "artifacts": artifacts}

        history.append(human)
        history.extend(new_messages)  # tool calls and results included, so 'ok' can find the plan_id / script_id
        trim_history()
    return {"response": final or "(no answer)", "steps": steps, "artifacts": artifacts}