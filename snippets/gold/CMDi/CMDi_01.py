import subprocess

def ping_host(host: str, count: int = 1) -> int:
    proc = subprocess.run(["ping", "-n", str(count), host], capture_output=True, text=True)
    return proc.returncode
