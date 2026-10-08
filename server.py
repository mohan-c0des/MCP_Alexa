from fastmcp import FastMCP

mcp = FastMCP("Agent_1: travel automation")

@mcp.tool()
def greet(name: str) -> str:
    """Greet a traveler by name. Use when the user introduces themselves."""
    return f"Hello {name}, ready to plan a trip?"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")