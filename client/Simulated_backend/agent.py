"""Terminal demo shell: a LangGraph agent that discovers the laptop's MCP tools and chains them.

The agent has NO tool code. It reads the tools AND the chaining instructions from the MCP
server, so the same server works with any MCP client (Alexa+, Claude, MCP Inspector...).

Run:  python agent.py      (server.py must already be running)
"""
import asyncio
import os

from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import ClientSession

from dotenv import load_dotenv
load_dotenv()

try:  # the function was renamed in newer MCP SDK versions
    from mcp.client.streamable_http import streamable_http_client as http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as http_client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

BASE_PROMPT = (
    "You are Alexa+, a voice assistant that works on the user's laptop through tools. "
    "Keep your final answers short and easy to say out loud."
)


async def fetch_server_instructions() -> str:
    """Read the chaining rules the MCP server sends when a client connects."""
    async with http_client(MCP_URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            init = await session.initialize()
            return init.instructions or ""


def build_agent(llm, tools, prompt: str):
    try:  # LangChain 1.x
        from langchain.agents import create_agent

        return create_agent(llm, tools, system_prompt=prompt)
    except ImportError:  # older LangGraph
        from langgraph.prebuilt import create_react_agent

        return create_react_agent(llm, tools, prompt=prompt)


def short(x, n: int = 300) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + " ..."


def as_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


async def ask(agent, history: list, question: str) -> str:
    """Run one user turn. `history` keeps EVERY message (including tool calls and tool results),
    so the agent still knows what a tool returned (like a script_id) on the next turn."""
    history.append(HumanMessage(question))
    new_messages, final = [], ""
    async for update in agent.astream({"messages": history}, stream_mode="updates"):
        for data in update.values():
            for m in (data or {}).get("messages", []):
                new_messages.append(m)
                for call in getattr(m, "tool_calls", None) or []:
                    print(f"  -> {call['name']}({short(call['args'])})")
                if m.type == "tool":
                    print(f"  <- {m.name}: {short(m.content)}")
                elif m.type == "ai" and m.content and not getattr(m, "tool_calls", None):
                    final = as_text(m.content)
    history.extend(new_messages)
    return final


async def main():
    rules = await fetch_server_instructions()
    client = MultiServerMCPClient({"laptop": {"url": MCP_URL, "transport": "streamable_http"}})
    tools = await client.get_tools()
    print(f"Connected to {MCP_URL}\nTools: {[t.name for t in tools]}")
    print(f"Server instructions loaded: {'yes' if rules else 'NO'}")

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    agent = build_agent(llm, tools, f"{BASE_PROMPT}\n\nSERVER INSTRUCTIONS:\n{rules}")

    history: list = []
    while True:
        question = input("\nYou> ").strip()
        if question.lower() in {"exit", "quit", ""}:
            break
        print(f"\nAlexa+> {await ask(agent, history, question)}")


if __name__ == "__main__":
    asyncio.run(main())