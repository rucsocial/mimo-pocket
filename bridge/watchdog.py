"""Keep the phone-remote bridge reachable.

Checks http://127.0.0.1:8765/api/status every INTERVAL seconds; when the HTTP
service is down (process killed, crash, stale pid file), clears stale state and
relaunches it with logging so the next failure leaves a trace.

Portable: derives every path from this file's location / environment.
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(
    os.environ.get("PHONE_REMOTE_HOME")
    or (Path.home() / ".local" / "share" / "mimocode" / "phone-remote")
)
PY = os.environ.get("MIMO_PYTHON") or sys.executable
BRIDGE = os.environ.get("PHONE_REMOTE_BRIDGE") or str(HERE / "bridge.py")
LOCK = HOME / "watchdog.pid"
LOG = HOME / "watchdog.log"
STDOUT_LOG = HOME / "stdout.log"
PORT = int(os.environ.get("PHONE_REMOTE_PORT", "8765"))
INTERVAL = 8
MAX_LOG = 512 * 1024
DETACHED = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED|NO_WINDOW


def log(msg: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))


def reachable() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/status" % PORT, timeout=5
        ) as r:
            json.loads(r.read().decode("utf-8"))
        return True
    except Exception:
        return False


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def rotate_stdout() -> None:
    try:
        if STDOUT_LOG.exists() and STDOUT_LOG.stat().st_size > MAX_LOG:
            STDOUT_LOG.write_bytes(STDOUT_LOG.read_bytes()[-MAX_LOG // 2 :])
    except Exception:
        pass


def relaunch() -> None:
    rotate_stdout()
    try:
        subprocess.run([PY, BRIDGE, "stop"], capture_output=True, timeout=30)
    except Exception as e:
        log("stop failed: %s" % e)
    time.sleep(1)
    out = STDOUT_LOG.open("a", encoding="utf-8")
    kwargs = {"stdout": out, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED
    subprocess.Popen([PY, BRIDGE, "start"], cwd=str(HERE), **kwargs)


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text(encoding="utf-8").strip())
            if pid != os.getpid() and process_alive(pid):
                print("watchdog already running pid=%d" % pid)
                return
        except Exception:
            pass
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    log("watchdog start pid=%d" % os.getpid())
    misses = 0
    try:
        while True:
            time.sleep(INTERVAL)
            if reachable():
                if misses:
                    log("recovered")
                misses = 0
                continue
            misses += 1
            log("unreachable (miss %d) -> relaunch" % misses)
            try:
                relaunch()
            except Exception as e:
                log("relaunch failed: %s" % e)
            time.sleep(4)
            log("after relaunch reachable=%s" % reachable())
    finally:
        try:
            if LOCK.read_text(encoding="utf-8").strip() == str(os.getpid()):
                LOCK.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
