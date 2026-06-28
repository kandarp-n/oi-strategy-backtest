"""Bot process manager: start / stop the live trading bot as a subprocess.

We store the PID + start metadata so the web UI can show status across
page reloads (and across web-app restarts).
"""
from __future__ import annotations

import os, sys, json, subprocess, signal, time
from datetime import datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, "web", "bot.pid")
LOG_FILE = os.path.join(ROOT, "live", "bot.log")
RUNNER_SCRIPT = os.path.join(ROOT, "live", "run_live.py")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if handle == 0:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == 259  # STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def get_status() -> dict:
    """Read the PID file and check if the bot is running."""
    if not os.path.exists(PID_FILE):
        return {"running": False, "pid": None, "mode": None, "started_at": None}
    try:
        with open(PID_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid", 0)
        alive = _is_pid_alive(int(pid))
        return {
            "running": alive,
            "pid": pid if alive else None,
            "mode": data.get("mode"),
            "started_at": data.get("started_at"),
            "log_file": data.get("log_file", LOG_FILE),
        }
    except Exception as e:
        return {"running": False, "pid": None, "mode": None,
                "started_at": None, "error": str(e)}


def start(mode: str) -> dict:
    """Start the bot in the given mode (PAPER / DRY_RUN / LIVE)."""
    if mode not in ("PAPER", "DRY_RUN", "LIVE"):
        raise ValueError(f"invalid mode {mode}")
    st = get_status()
    if st["running"]:
        return {"ok": False, "error": f"bot already running (pid {st['pid']}, mode {st['mode']})"}

    # FIX: Remove any stale KILL file from a previous shutdown before starting.
    # Without this, the bot would see the leftover file on its first poll and
    # immediately exit, making it look like "Start doesn't work".
    kill_path = os.path.join(ROOT, "live", "KILL")
    if os.path.exists(kill_path):
        try:
            os.remove(kill_path)
        except Exception:
            pass

    # Map mode to CLI flag
    flag = {"PAPER": "--paper", "DRY_RUN": "--dry-run", "LIVE": "--live"}[mode]

    # Open log file for stdout/stderr capture
    log_fh = open(LOG_FILE, "a", buffering=1)
    log_fh.write(f"\n\n========== Bot started via web UI at {datetime.now().isoformat()} ({mode}) ==========\n\n")
    log_fh.flush()

    # For LIVE mode, we have to bypass the "YES I UNDERSTAND" prompt.
    # The web UI's start handler should ONLY allow this after explicit user
    # confirmation in the UI itself.
    env = os.environ.copy()
    env["DHAN_LIVE_CONFIRMED"] = "yes"   # marker -- runner can check this

    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS so the bot survives the web app shutting down
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    cmd = [sys.executable, RUNNER_SCRIPT, flag]
    # We need to provide "YES I UNDERSTAND" on stdin for LIVE mode (the runner
    # prompts for that). For PAPER/DRY_RUN there's no prompt.
    stdin = subprocess.PIPE if mode == "LIVE" else subprocess.DEVNULL

    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                              stdin=stdin, cwd=ROOT, env=env,
                              creationflags=creationflags)
    if mode == "LIVE":
        try:
            proc.stdin.write(b"YES I UNDERSTAND\n")
            proc.stdin.flush()
        except Exception:
            pass

    # Save PID metadata
    with open(PID_FILE, "w") as f:
        json.dump({
            "pid": proc.pid, "mode": mode,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "log_file": LOG_FILE,
        }, f, indent=2)
    time.sleep(1.0)
    return {"ok": True, "pid": proc.pid, "mode": mode}


def stop(graceful: bool = True) -> dict:
    """Stop the bot. Graceful = create KILL file. Else = SIGTERM."""
    st = get_status()
    kill_path = os.path.join(ROOT, "live", "KILL")
    if not st["running"]:
        # FIX: even if bot is not running, clean up any stale KILL file +
        # PID file so the next start is clean.
        cleaned = []
        if os.path.exists(kill_path):
            try:
                os.remove(kill_path); cleaned.append("KILL")
            except Exception:
                pass
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE); cleaned.append("bot.pid")
            except Exception:
                pass
        if cleaned:
            return {"ok": True, "error": "bot was already stopped",
                    "method": f"cleaned stale {', '.join(cleaned)}"}
        return {"ok": False, "error": "bot is not running"}
    pid = int(st["pid"])
    if graceful:
        # Touch the KILL file -- the runner polls for this and exits cleanly
        with open(kill_path, "w") as f:
            f.write(datetime.now().isoformat())
        # Wait up to 60s for graceful exit
        for _ in range(60):
            if not _is_pid_alive(pid):
                # Bot itself removes KILL on graceful exit, but clean up here
                # too as belt-and-braces (handles older bot versions).
                try: os.remove(kill_path)
                except Exception: pass
                try: os.remove(PID_FILE)
                except Exception: pass
                return {"ok": True, "method": "graceful (KILL file)"}
            time.sleep(1)
        # Fall through to forceful kill
    # Forceful kill
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        if _is_pid_alive(pid):
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except Exception as e:
        # FIX: Even on failure, attempt to clean up files so next start works.
        try: os.remove(kill_path)
        except Exception: pass
        try: os.remove(PID_FILE)
        except Exception: pass
        return {"ok": False, "error": str(e)}
    # FIX: Cleanup BOTH KILL and PID on force-kill success
    try: os.remove(kill_path)
    except Exception: pass
    try: os.remove(PID_FILE)
    except Exception: pass
    return {"ok": True, "method": "forceful"}


def tail_log(n_lines: int = 200) -> str:
    if not os.path.exists(LOG_FILE):
        return ""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read last 64 KB
            read_n = min(size, 65536)
            f.seek(size - read_n)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-n_lines:])
    except Exception:
        return ""
