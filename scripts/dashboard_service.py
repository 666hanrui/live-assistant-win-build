#!/usr/bin/env python3
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import errno
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
PID_FILE = RUN_DIR / "streamlit.pid"
LOG_FILE = LOG_DIR / "streamlit.out"

DEFAULT_PORT = int(os.getenv("DASHBOARD_PORT", "8501"))
DEFAULT_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")


def _venv_python() -> Path:
    if os.name == "nt":
        p = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        p = ROOT / ".venv" / "bin" / "python"
    return p


def _streamlit_cmd(port: int, host: str):
    py = _venv_python()
    if not py.exists():
        py = Path(sys.executable)
    return [
        str(py),
        "-m",
        "streamlit",
        "run",
        "dashboard.py",
        "--server.port",
        str(port),
        "--server.address",
        host,
        "--server.fileWatcherType",
        "none",
        "--server.headless",
        "true",
    ]


def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _write_pid(pid: int):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        # 在部分受限环境中，kill(0) 可能返回 EPERM（无权限），但进程实际存活。
        if getattr(e, "errno", None) == errno.EPERM:
            return True
        return False


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    # localhost 与 127.0.0.1 在不同系统/网络栈下可能表现不同，双探测避免误判。
    host = str(host or "").strip() or "127.0.0.1"
    if host == "127.0.0.1":
        hosts = ["127.0.0.1", "localhost"]
    elif host == "localhost":
        hosts = ["localhost", "127.0.0.1"]
    else:
        hosts = [host, "127.0.0.1", "localhost"]
    for h in hosts:
        try:
            with socket.create_connection((h, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False


def _listening_pids(port: int):
    pids = []

    # 优先 lsof（macOS/Linux）
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pids.append(int(line))
            except ValueError:
                continue
        if pids:
            return sorted(set(pids))
    except Exception:
        pass

    # Windows 回退：PowerShell 获取占用端口进程
    if os.name == "nt":
        try:
            ps_cmd = (
                f"Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
                "| Select-Object -ExpandProperty OwningProcess"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pids.append(int(line))
                except ValueError:
                    continue
            if pids:
                return sorted(set(pids))
        except Exception:
            pass

        # Windows 再兜底：netstat -ano
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            target = f":{int(port)}"
            for raw in (proc.stdout or "").splitlines():
                line = raw.strip()
                if not line:
                    continue
                # 示例: TCP    0.0.0.0:8501   0.0.0.0:0   LISTENING   12345
                parts = line.split()
                if len(parts) < 5:
                    continue
                proto = parts[0]
                local_addr = parts[1]
                remote_addr = parts[2] if len(parts) >= 3 else ""
                state = parts[3] if len(parts) >= 4 else ""
                pid_s = parts[-1]
                if not proto.upper().startswith("TCP"):
                    continue
                if not local_addr.endswith(target):
                    continue
                state_upper = state.upper()
                looks_listen = (
                    state_upper in {"LISTENING", "LISTEN"}
                    or ("LISTEN" in state_upper)
                    or str(remote_addr).endswith(":0")
                    or str(remote_addr).endswith(":*")
                    or str(remote_addr) in {"0.0.0.0:0", "[::]:0", "*:*"}
                )
                if not looks_listen:
                    continue
                try:
                    pids.append(int(pid_s))
                except ValueError:
                    continue
        except Exception:
            pass

    return sorted(set(pids))


def _tail_log(lines: int = 40):
    if not LOG_FILE.exists():
        return ""
    content = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(content[-lines:])


def _looks_like_bind_error(text: str) -> bool:
    raw = (text or "").lower()
    hints = [
        "operation not permitted",
        "permission denied",
        "cannot assign requested address",
        "address already in use",
        "errno 13",
        "errno 1",
        "bind(",
        "sock.bind",
    ]
    return any(h in raw for h in hints)


def _stop_pid(pid: int):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    pgid = None
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = None

    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            # 再兜底调用系统 kill，处理部分受限 shell 场景
            subprocess.run(["kill", "-TERM", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)

    try:
        if pgid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            subprocess.run(["kill", "-KILL", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # 给操作系统一点时间回收监听 socket，避免 stop 后立即检查端口时误判 still listening。
    deadline2 = time.time() + 2.5
    while time.time() < deadline2:
        if not _pid_alive(pid):
            return
        time.sleep(0.15)

    # 某些环境下 os.kill 返回成功但目标进程仍存活，再强制调用系统 kill 一次。
    if _pid_alive(pid):
        subprocess.run(["kill", "-9", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        deadline3 = time.time() + 2.0
        while time.time() < deadline3:
            if not _pid_alive(pid):
                return
            time.sleep(0.12)


def _stop_port_owners(port: int, exclude_pids=None):
    exclude = set(exclude_pids or [])
    owners = _listening_pids(port)
    stopped = []
    for owner in owners:
        if owner in exclude:
            continue
        _stop_pid(owner)
        if not _pid_alive(owner):
            stopped.append(owner)
    return stopped


def cmd_status(port: int):
    pid = _read_pid()
    owners = _listening_pids(port)
    if pid and _pid_alive(pid) and pid in owners:
        print(f"RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    # 在不支持端口归属探测的环境（如缺少 lsof / 受限沙箱）下，PID 存活即视为运行中。
    if pid and _pid_alive(pid) and not owners:
        print(f"RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    # 无法拿到 owners 时，用端口探活兜底，避免误报 STOPPED。
    if pid and _pid_alive(pid) and _port_open(port, "127.0.0.1"):
        print(f"RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    if owners:
        print(f"RUNNING unmanaged_pid={owners[0]} url=http://127.0.0.1:{port}")
        return 0
    if _port_open(port, "127.0.0.1"):
        print(f"RUNNING unmanaged_pid=unknown url=http://127.0.0.1:{port}")
        return 0
    if pid and not _pid_alive(pid):
        _remove_pid()
    print(f"STOPPED url=http://127.0.0.1:{port}")
    return 1


def cmd_start(port: int, host: str, wait_seconds: float):
    pid = _read_pid()
    owners_before = _listening_pids(port)
    if pid and _pid_alive(pid) and pid in owners_before:
        print(f"ALREADY RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    if pid and _pid_alive(pid) and not owners_before:
        print(f"ALREADY RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    if pid and _pid_alive(pid) and _port_open(port, "127.0.0.1"):
        _write_pid(pid)
        print(f"ALREADY RUNNING pid={pid} url=http://127.0.0.1:{port}")
        return 0
    if owners_before:
        # 端口已被其他进程监听：接管该进程 PID，避免后续 stop/restart 失控。
        owner_pid = owners_before[0]
        _write_pid(owner_pid)
        print(f"ALREADY RUNNING adopted_pid={owner_pid} url=http://127.0.0.1:{port}")
        return 0
    if _port_open(port, "127.0.0.1"):
        print(f"ALREADY RUNNING adopted_pid=unknown url=http://127.0.0.1:{port}")
        return 0
    if pid and not _pid_alive(pid):
        _remove_pid()

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _spawn_once(bind_host: str):
        cmd = _streamlit_cmd(port, bind_host)
        log_fp = LOG_FILE.open("a", encoding="utf-8")
        kwargs = {
            "cwd": str(ROOT),
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "close_fds": os.name != "nt",
        }

        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(cmd, **kwargs)
        else:
            proc = subprocess.Popen(cmd, preexec_fn=os.setsid, **kwargs)

        _write_pid(proc.pid)
        deadline = time.time() + max(2.0, wait_seconds)
        while time.time() < deadline:
            owners = _listening_pids(port)
            if owners:
                owner_pid = owners[0]
                _write_pid(owner_pid)
                print(f"STARTED pid={owner_pid} url=http://127.0.0.1:{port} host={bind_host}")
                return 0
            if _port_open(port, "127.0.0.1"):
                print(f"STARTED pid={proc.pid} url=http://127.0.0.1:{port} host={bind_host}")
                return 0
            if proc.poll() is not None:
                break
            time.sleep(0.25)

        tail = _tail_log(80)
        return 2, tail

    result = _spawn_once(host)
    if result == 0:
        return 0

    # 首次失败：常见为绑定地址受限，自动回退 localhost。
    tail = result[1] if isinstance(result, tuple) and len(result) > 1 else _tail_log(80)
    if host not in ("127.0.0.1", "localhost") and _looks_like_bind_error(tail):
        print(f"START RETRY host={host} -> 127.0.0.1")
        result2 = _spawn_once("127.0.0.1")
        if result2 == 0:
            return 0
        tail = result2[1] if isinstance(result2, tuple) and len(result2) > 1 else _tail_log(80)

    _remove_pid()
    print("START FAILED")
    print(tail)
    return 2


def cmd_stop(port: int, include_unmanaged: bool = False):
    pid = _read_pid()
    stopped_unmanaged = []
    if not pid:
        if include_unmanaged:
            stopped_unmanaged = _stop_port_owners(port)
        if stopped_unmanaged:
            print(f"STOPPED unmanaged={stopped_unmanaged}")
        else:
            print("STOPPED (no pid)")
        return 0

    _stop_pid(pid)
    managed_alive = _pid_alive(pid)
    if include_unmanaged:
        # 若托管 PID 仍存活，不应排除它，否则会残留 unmanaged 进程。
        excludes = [] if managed_alive else [pid]
        stopped_unmanaged = _stop_port_owners(port, exclude_pids=excludes)
    owners_after = _listening_pids(port)
    if owners_after:
        # 某些系统上 TERM 后端口释放存在抖动，补一次兜底清理再判定。
        extra_stopped = _stop_port_owners(port)
        if extra_stopped:
            stopped_unmanaged.extend([x for x in extra_stopped if x not in stopped_unmanaged])
            time.sleep(0.25)
            owners_after = _listening_pids(port)
    # 以端口占用为最终判据：即使主 PID 仍存活，只要目标端口已释放，就视为停止成功。
    if owners_after:
        if stopped_unmanaged:
            print(f"STOP FAILED pid={pid} unmanaged={stopped_unmanaged} owners={owners_after}")
        else:
            print(f"STOP FAILED pid={pid} owners={owners_after}")
        return 2
    _remove_pid()
    suffix = " (pid_alive_but_port_released)" if managed_alive else ""
    if stopped_unmanaged:
        print(f"STOPPED pid={pid} unmanaged={stopped_unmanaged}{suffix}")
    else:
        print(f"STOPPED pid={pid}{suffix}")
    return 0


def cmd_logs():
    print(_tail_log(120))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Manage Streamlit dashboard service.")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "ensure", "logs"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--wait-seconds", type=float, default=12.0)
    parser.add_argument(
        "--force-port",
        action="store_true",
        help="在 stop/restart 时一并清理占用目标端口的非托管进程。",
    )
    args = parser.parse_args()

    if args.action == "start":
        return cmd_start(args.port, args.host, args.wait_seconds)
    if args.action == "stop":
        return cmd_stop(args.port, include_unmanaged=args.force_port)
    if args.action == "restart":
        stop_code = cmd_stop(args.port, include_unmanaged=args.force_port)
        if stop_code != 0:
            return stop_code
        return cmd_start(args.port, args.host, args.wait_seconds)
    if args.action == "status":
        return cmd_status(args.port)
    if args.action == "ensure":
        if cmd_status(args.port) == 0:
            return 0
        return cmd_start(args.port, args.host, args.wait_seconds)
    if args.action == "logs":
        return cmd_logs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
