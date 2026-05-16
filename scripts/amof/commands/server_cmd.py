"""Server command for AMOF Control Plane API."""

import argparse
import os
import sys
import subprocess
import signal
import time
from pathlib import Path

def get_pid_file() -> Path:
    platform_root = Path(__file__).resolve().parents[3]
    return platform_root / ".amof" / "server.pid"

def get_server_pid() -> int:
    pid_file = get_pid_file()
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except ValueError:
            return 0
    return 0

def is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def cmd_serve(args: argparse.Namespace) -> int:
    """Manage the AMOF Control Plane FastAPI server."""
    action = getattr(args, "action", "start")
    if action is None:
        action = "start"

    pid = get_server_pid()
    running = is_running(pid)

    if action == "status":
        if running:
            print(f"AMOF server is running (PID: {pid})")
            return 0
        else:
            print("AMOF server is not running.")
            return 1

    elif action == "stop":
        if running:
            print(f"Stopping AMOF server (PID: {pid})...")
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not is_running(pid):
                    break
                time.sleep(0.1)
            if is_running(pid):
                os.kill(pid, signal.SIGKILL)
            get_pid_file().unlink(missing_ok=True)
            
            # also explicitly clear port 8000
            subprocess.run("fuser -k 8000/tcp", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            print("Server stopped.")
        else:
            print("Server is not running.")
        return 0

    elif action == "restart":
        args.action = "stop"
        cmd_serve(args)
        time.sleep(1)
        args.action = "start"
        return cmd_serve(args)

    elif action == "start":
        if running:
            print(f"AMOF server is already running (PID: {pid})")
            return 0

        # Kill any stray processes on port 8000
        subprocess.run("fuser -k 8000/tcp", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        try:
            import uvicorn
        except ImportError:
            sys.stderr.write("uvicorn is not installed. pip install uvicorn fastapi\n")
            return 1

        platform_root = Path(__file__).resolve().parents[3]
        os.chdir(platform_root)
        
        if str(platform_root / "scripts") not in sys.path:
            sys.path.insert(0, str(platform_root / "scripts"))

        print(f"Starting AMOF Control Plane on http://{args.host}:{args.port}")
        
        # Run Uvicorn in background using Popen if not reload, otherwise we have to foreground it
        # Actually for dev we might want to just run it and block. Let's block for now to keep logs.
        # But if the user wants 'start' to daemonize... the requirement was just a simple way to manage.
        # Let's write the PID.
        get_pid_file().parent.mkdir(parents=True, exist_ok=True)
        get_pid_file().write_text(str(os.getpid()))

        try:
            uvicorn.run(
                "amof.api.main:app",
                host=args.host,
                port=args.port,
                reload=args.reload,
                reload_dirs=[str(platform_root / "scripts" / "amof")] if args.reload else None,
            )
        finally:
            get_pid_file().unlink(missing_ok=True)
            
        return 0
