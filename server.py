import subprocess
from fastmcp import FastMCP

from instructions import SERVER_INSTRUCTIONS
from tools.analyze_data import analyze_data
from tools.research_topic import research_topic
from tools.script_writer import undo_last_script, write_script, run_script
from tools.screen_control import approve_screen_plan, ask_about_screen, interact_with_screen, open_item

from dotenv import load_dotenv
load_dotenv()


mcp = FastMCP("laptop-assistant", instructions=SERVER_INSTRUCTIONS)

mcp.tool()(research_topic)
mcp.tool()(analyze_data)

mcp.tool()(write_script)
mcp.tool()(run_script)
mcp.tool()(undo_last_script)
mcp.tool()(open_item)

mcp.tool()(ask_about_screen)
mcp.tool()(interact_with_screen)
mcp.tool()(approve_screen_plan)

@mcp.tool
def run_git_status(repo_path: str = ".") -> str:
    """Runs 'git status' in the specified directory to check for uncommitted work."""
    try:
        result = subprocess.run(
            ["git", "status"], 
            cwd=repo_path, 
            capture_output=True, 
            text=True,
            check=True
        )
        return result.stdout
    except Exception as e:
        return f"Failed to run git status: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")  # serves at http://127.0.0.1:8000/mcp