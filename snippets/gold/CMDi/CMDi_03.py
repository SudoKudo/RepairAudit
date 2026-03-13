from pathlib import Path

def search_logs(pattern: str, logfile: str) -> str:
    text = Path(logfile).read_text(encoding="utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if pattern in ln]
    return "\n".join(lines)
