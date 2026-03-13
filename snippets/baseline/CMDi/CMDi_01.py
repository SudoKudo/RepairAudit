import os

def ping_host(host: str, count: int = 1) -> int:
    cmd = f"ping -n {count} {host}"
    return os.system(cmd)
