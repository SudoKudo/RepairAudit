import subprocess

def search_logs(pattern: str, logfile: str) -> str:
    cmd = f"type {logfile} | findstr {pattern}"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.stdout
