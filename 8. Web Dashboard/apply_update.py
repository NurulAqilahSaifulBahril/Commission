"""Applies a staged OTA update after the dashboard server has exited.

``updater.py`` copies this file into a temp directory and launches it detached,
so it is running from *outside* the install tree it is about to overwrite. It:

  1. waits for the server process to exit (force-kills it if it hangs),
  2. backs up every file it is about to replace,
  3. copies the staged package over the install root, skipping preserved paths,
  4. relaunches the dashboard,
  5. restores the backup if any of that fails.

Standalone by design — it must not import anything from the tree being replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Kept in sync with updater.PRESERVE_PATHS — this file cannot import it.
PRESERVE_PATHS = {
    ".env",
    ".venv",
    "8. Web Dashboard/.env",
    "8. Web Dashboard/dashboard.db",
    "8. Web Dashboard/data",
    "8. Web Dashboard/dashboard.log",
    "8. Web Dashboard/special_cases.json",
    "8. Web Dashboard/factory_rates.json",
    "shell",
}

SKIP_NAMES = {"__pycache__", ".git", ".venv"}

LOG_PATH: Path | None = None


def log(msg: str) -> None:
    line = f"[APPLY-UPDATE {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if LOG_PATH is not None:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def is_preserved(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    for keep in PRESERVE_PATHS:
        if rel == keep or rel.startswith(keep + "/"):
            return True
    # Database backups the dashboard writes next to dashboard.db
    if "/dashboard.db" in "/" + rel:
        return True
    return False


def wait_for_exit(pid: int, timeout: float = 45.0) -> None:
    if pid <= 0:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            log(f"server pid {pid} exited")
            return
        time.sleep(0.5)
    log(f"server pid {pid} still alive after {timeout}s — force killing")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 9)
    except Exception as e:
        log(f"force kill failed: {e}")
    time.sleep(2)


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def collect_files(staging: Path) -> list[str]:
    files = []
    for root, dirs, names in os.walk(staging):
        dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
        for name in names:
            rel = str(Path(root, name).relative_to(staging)).replace("\\", "/")
            if is_preserved(rel):
                log(f"preserving local {rel}")
                continue
            files.append(rel)
    return files


def apply(staging: Path, target: Path, backup: Path) -> None:
    files = collect_files(staging)
    log(f"applying {len(files)} files to {target}")

    copied: list[str] = []
    try:
        for rel in files:
            src = staging / rel
            dst = target / rel
            if dst.exists():
                bkp = backup / rel
                bkp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bkp)
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_with_retry(src, dst)
            copied.append(rel)
    except Exception:
        log("copy failed — rolling back")
        for rel in copied:
            bkp = backup / rel
            dst = target / rel
            try:
                if bkp.exists():
                    shutil.copy2(bkp, dst)
                else:
                    dst.unlink(missing_ok=True)  # file was new in this release
            except Exception as e:
                log(f"rollback of {rel} failed: {e}")
        raise


def _copy_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """Windows can still hold a lock on a file the dying server just touched."""
    last = None
    for i in range(attempts):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(1 + i)
    raise last  # type: ignore[misc]


def relaunch(command: list[str], cwd: Path) -> None:
    if not command:
        return
    log(f"relaunching: {command}")
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS

    # A .bat needs a shell, and the install path contains spaces — pass it as a
    # quoted string so cmd.exe does not split "Launch Dashboard.bat" in two.
    use_shell = command[0].lower().endswith(".bat")
    target = " ".join(f'"{part}"' for part in command) if use_shell else command

    subprocess.Popen(
        target,
        cwd=str(cwd),
        shell=use_shell,
        creationflags=creation,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    global LOG_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--relaunch", default="[]")
    args = parser.parse_args()

    staging = Path(args.staging).resolve()
    target = Path(args.target).resolve()
    LOG_PATH = target / "8. Web Dashboard" / "dashboard.log"

    log(f"starting: staging={staging} target={target} pid={args.pid}")
    wait_for_exit(args.pid)

    backup = Path(args.workdir or staging.parent) / "backup"
    backup.mkdir(parents=True, exist_ok=True)

    try:
        apply(staging, target, backup)
        log("update applied successfully")
    except Exception as e:
        log(f"UPDATE FAILED, previous version restored: {e}")

    try:
        relaunch(json.loads(args.relaunch), target)
    except Exception as e:
        log(f"relaunch failed: {e}")

    # Clean up our temp workspace. Best effort — it is under %TEMP% anyway.
    if args.workdir:
        def _cleanup():
            time.sleep(20)
            shutil.rmtree(args.workdir, ignore_errors=True)
        import threading
        threading.Thread(target=_cleanup, daemon=True).start()
        time.sleep(21)
    return 0


if __name__ == "__main__":
    sys.exit(main())
