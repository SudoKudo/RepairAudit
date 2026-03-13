import subprocess

def make_archive(src_dir: str, out_file: str) -> int:
    proc = subprocess.run(["tar", "-czf", out_file, src_dir], capture_output=True, text=True)
    return proc.returncode
