import subprocess

_ALLOWED_TOOLS = {"echo", "where", "whoami"}

def run_tool(tool: str, arg: str) -> int:
    if tool not in _ALLOWED_TOOLS:
        raise ValueError("Tool not permitted")
    proc = subprocess.run([tool, arg], capture_output=True, text=True)
    return proc.returncode
