import os
import subprocess
import psutil
from fastmcp import FastMCP

# Initialize the MCP Server
mcp = FastMCP("Alexa Laptop Productivity Hub")

@mcp.tool
def get_system_status() -> str:
    """Returns the actual CPU and Memory usage of the developer's laptop."""
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    return f"Laptop status: CPU is at {cpu}%. RAM is at {ram}%."

@mcp.tool
def read_local_file(file_path: str) -> str:
    """Reads the contents of a local file on the developer's machine."""
    try:
        # Resolve to an absolute path for safety
        full_path = os.path.abspath(file_path)
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
            # Truncate if the file is massive so we don't blow up the LLM context window
            return content[:2000] + "\n...(truncated)" if len(content) > 2000 else content
    except Exception as e:
        return f"Error reading file: {str(e)}"

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
    print("Starting Alexa+ MCP Server on http://127.0.0.1:8000/sse")
    mcp.run(transport="http", host="127.0.0.1", port=8000)