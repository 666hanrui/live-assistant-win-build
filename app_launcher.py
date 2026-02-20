from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
import importlib.util
import importlib
import traceback
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
    hosts = []
    normalized = str(host or "").strip().lower()
    if normalized in {"", "0.0.0.0", "::"}:
        hosts = ["127.0.0.1", "localhost"]
    elif normalized == "127.0.0.1":
        hosts = ["127.0.0.1", "localhost"]
    elif normalized == "localhost":
        hosts = ["localhost", "127.0.0.1"]
    else:
        hosts = [str(host), "127.0.0.1", "localhost"]

    for item in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            s.connect((item, port))
            return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def _pick_available_port(preferred: int, host: str = "127.0.0.1") -> int:
    if preferred <= 0:
        preferred = 8501
    if not _port_open(preferred, host=host):
        return preferred
    for p in range(preferred + 1, preferred + 21):
        if not _port_open(p, host=host):
            return p
    return preferred


def _browser_host_for_bind_host(bind_host: str) -> str:
    host = str(bind_host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _open_browser_later(
    url: str,
    port: int,
    host: str = "127.0.0.1",
    delay: float = 1.0,
    timeout_seconds: float = 45.0,
    on_timeout=None,
) -> None:
    def _worker():
        time.sleep(max(0.1, delay))
        deadline = time.time() + max(3.0, float(timeout_seconds))
        ready = False
        while time.time() < deadline:
            if _port_open(port, host=host):
                ready = True
                break
            time.sleep(0.3)
        if not ready:
            if callable(on_timeout):
                try:
                    on_timeout()
                except Exception:
                    pass
            return
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _append_boot_log(runtime_root: Path, message: str) -> None:
    try:
        log_dir = runtime_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "launcher_boot.log").open("a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")
    except Exception:
        pass


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _inject_bundle_site_packages(bundle_root: Path) -> list[str]:
    injected = []
    if not getattr(sys, "frozen", False):
        return injected
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        bundle_root / "site-packages",
        exe_dir / "_internal" / "site-packages",
        exe_dir / "site-packages",
    ]
    seen = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except Exception:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if not path.exists() or not path.is_dir():
            continue
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
            injected.append(text)
    return injected


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
    _append_boot_log(runtime_root, f"boot: bundle_root={bundle_root}")
    _append_boot_log(runtime_root, f"boot: runtime_root={runtime_root}")
    injected_paths = _inject_bundle_site_packages(bundle_root)
    if injected_paths:
        _append_boot_log(runtime_root, f"injected_site_packages={injected_paths}")

    os.environ.setdefault("LIVE_ASSISTANT_ENV", str(runtime_root / ".env"))
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("USER_DATA_PATH", str(runtime_root / "user_data"))
    os.environ.setdefault("STREAMLIT_SUPPRESS_CONFIG_WARNINGS", "true")
    # Packaged app must run in non-dev mode, otherwise Streamlit rejects --server.port.
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"

    dashboard_script = _resolve_dashboard_script(bundle_root=bundle_root, runtime_root=runtime_root)
    if dashboard_script is None:
        err = (
            "dashboard.py not found and dashboard module unavailable. "
            f"bundle_root={bundle_root}, exe={Path(sys.executable).resolve()}"
        )
        print(err)
        _append_boot_log(runtime_root, err)
        return 2
    _append_boot_log(runtime_root, f"dashboard_script={dashboard_script}")

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    browser_host = _browser_host_for_bind_host(host)
    self_check = _is_truthy(os.getenv("APP_LAUNCHER_SELF_CHECK", ""))
    try:
        browser_timeout = float(os.getenv("DASHBOARD_OPEN_BROWSER_TIMEOUT_SECONDS", "45"))
    except Exception:
        browser_timeout = 45.0
    try:
        configured_port = int(os.getenv("DASHBOARD_PORT", "8501"))
    except Exception:
        configured_port = 8501
    port = _pick_available_port(configured_port, host=browser_host)
    if port != configured_port:
        _append_boot_log(runtime_root, f"port_busy_fallback: {configured_port} -> {port}")
    os.environ["DASHBOARD_PORT"] = str(port)
    url = f"http://{browser_host}:{port}"
    auto_open = os.getenv("DASHBOARD_AUTO_OPEN", "true").lower() in ("1", "true", "yes", "on")
    if auto_open and not self_check:
        _open_browser_later(
            url=url,
            port=port,
            host=browser_host,
            delay=1.2,
            timeout_seconds=browser_timeout,
            on_timeout=lambda: _append_boot_log(
                runtime_root,
                (
                    f"skip_open_browser: server_not_ready host={browser_host} "
                    f"port={port} wait_seconds={browser_timeout}"
                ),
            ),
        )

    # 延迟导入，确保环境变量/工作目录已就位。
    try:
        from streamlit.web import cli as stcli  # pylint: disable=import-outside-toplevel
    except Exception as e:
        _append_boot_log(runtime_root, f"import_streamlit_failed: {e}")
        _append_boot_log(runtime_root, traceback.format_exc())
        raise

    if self_check:
        try:
            importlib.import_module("streamlit")
            print("APP_LAUNCHER_SELF_CHECK_OK")
            _append_boot_log(runtime_root, "APP_LAUNCHER_SELF_CHECK_OK")
            return 0
        except Exception as e:
            _append_boot_log(runtime_root, f"self_check_failed: {e}")
            _append_boot_log(runtime_root, traceback.format_exc())
            raise

    sys.argv = [
        "streamlit",
        "run",
        str(dashboard_script),
        "--global.developmentMode",
        "false",
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
    _append_boot_log(runtime_root, f"launch_streamlit: {' '.join(sys.argv)}")
    try:
        return int(stcli.main())
    except Exception as e:
        _append_boot_log(runtime_root, f"streamlit_main_failed: {e}")
        _append_boot_log(runtime_root, traceback.format_exc())
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        runtime_root = _runtime_root()
        _append_boot_log(runtime_root, f"fatal_exception: {e}")
        _append_boot_log(runtime_root, traceback.format_exc())
        raise
