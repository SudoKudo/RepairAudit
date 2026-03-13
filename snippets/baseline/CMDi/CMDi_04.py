import subprocess

def run_tool(tool: str, arg: str) -> int:
    cmd = tool + " " + arg
    proc = subprocess.run(cmd, shell=True)
    return proc.returncode
