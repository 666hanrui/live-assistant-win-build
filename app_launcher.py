import os
import socket
import sys
import threading
import time
import webbrowser
import importlib.util
from pathlib import Path


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _runtime_root() -> Path:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path.cwd())
    candidates.append(Path.home() / "AI_Live_Assistant")
    for path in candidates:
        if _is_writable_dir(path):
            return path
    return Path.cwd()


def _ensure_runtime_dirs(root: Path) -> None:
    for rel in ("logs", "run", "data", "user_data", "data/analytics", "data/reports"):
        (root / rel).mkdir(parents=True, exist_ok=True)


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _open_browser_later(url: str, port: int, delay: float = 1.0) -> None:
    def _worker():
        time.sleep(max(0.1, delay))
        deadline = time.time() + 15
        while time.time() < deadline:
            if _port_open(port):
                break
            time.sleep(0.3)
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _resolve_dashboard_script(bundle_root: Path, runtime_root: Path) -> Path | None:
    env_path = os.getenv("DASHBOARD_SCRIPT", "").strip()
    candidates = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                bundle_root / "dashboard.py",
                exe_dir / "dashboard.py",
                bundle_root.parent / "dashboard.py",
                bundle_root / "_internal" / "dashboard.py",
                exe_dir / "_internal" / "dashboard.py",
            ]
        )
    else:
        candidates.append(bundle_root / "dashboard.py")

    seen = set()
    for candidate in candidates:
        path = candidate.resolve()
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_file():
            return path

    spec = importlib.util.find_spec("dashboard")
    if spec is None:
        return None

    bootstrap_script = runtime_root / "run" / "dashboard_bootstrap.py"
    bootstrap_script.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_script.write_text(
        "import runpy\nrunpy.run_module('dashboard', run_name='__main__')\n",
        encoding="utf-8",
    )
    return bootstrap_script


def main() -> int:
    bundle_root = _bundle_root()
    runtime_root = _runtime_root()
    _ensure_runtime_dirs(runtime_root)
    os.chdir(runtime_root)

    os.environ.setdefault("LIVE_ASSISTANT_ENV", str(runtime_root / ".env"))
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("USER_DATA_PATH", str(runtime_root / "user_data"))

    dashboard_script = _resolve_dashboard_script(bundle_root=bundle_root, runtime_root=runtime_root)
    if dashboard_script is None:
        print(
            "dashboard.py not found and dashboard module unavailable. "
            f"bundle_root={bundle_root}, exe={Path(sys.executable).resolve()}"
        )
        return 2

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    url = f"http://127.0.0.1:{port}"
    auto_open = os.getenv("DASHBOARD_AUTO_OPEN", "true").lower() in ("1", "true", "yes", "on")
    if auto_open:
        _open_browser_later(url=url, port=port, delay=1.2)

    # 延迟导入，确保环境变量/工作目录已就位。
    from streamlit.web import cli as stcli  # pylint: disable=import-outside-toplevel

    sys.argv = [
        "streamlit",
        "run",
        str(dashboard_script),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.fileWatcherType",
        "none",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    return int(stcli.main())


if __name__ == "__main__":
    raise SystemExit(main())
