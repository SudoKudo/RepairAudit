import subprocess

def make_archive(src_dir: str, out_file: str) -> int:
    cmd = f"tar -czf {out_file} {src_dir}"
    proc = subprocess.run(cmd, shell=True)
    return proc.returncode
