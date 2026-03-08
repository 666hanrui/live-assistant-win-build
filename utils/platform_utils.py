import os
import platform
from pathlib import Path
import shlex
import shutil
import subprocess


def get_platform_key():
    sys_name = (platform.system() or "").lower()
    if "darwin" in sys_name:
        return "macos"
    if "windows" in sys_name:
        return "windows"
    return "linux"


def get_platform_label():
    key = get_platform_key()
    return {
        "macos": "macOS",
        "windows": "Windows",
        "linux": "Linux",
    }.get(key, key)


def _dedupe_keep_order(items):
    out = []
    seen = set()
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_exec_hint(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    # 兼容把可执行路径写成带引号或“路径+参数”的情况。
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    try:
        if " --" in s or (" -" in s and not os.path.exists(s)):
            tokens = shlex.split(s, posix=(get_platform_key() != "windows"))
            if tokens:
                s = tokens[0].strip()
    except Exception:
        pass
    s = s.strip('"').strip("'").strip()
    # 兼容 Windows 常见写法：%LOCALAPPDATA%\...\chrome.exe
    # 以及 Unix 风格：~/...
    s = os.path.expandvars(os.path.expanduser(s))
    return s.strip()


def _win_local_app_paths():
    local = os.getenv("LOCALAPPDATA", "").strip()
    if not local:
        return []
    return [
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]


def _resolve_browser_candidates(chrome_executable=""):
    hint = _normalize_exec_hint(chrome_executable)
    key = get_platform_key()
    if key == "macos":
        return _dedupe_keep_order([
            hint,
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "google-chrome",
            "microsoft-edge",
            "chromium",
        ])
    if key == "windows":
        return _dedupe_keep_order([
            hint,
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            *_win_local_app_paths(),
            "chrome.exe",
            "msedge.exe",
        ])
    return _dedupe_keep_order([
        hint,
        "google-chrome",
        "microsoft-edge",
        "chromium-browser",
        "chromium",
    ])


def _resolve_existing_executable(candidates):
    for c in candidates:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        resolved = shutil.which(c)
        if resolved:
            return resolved
        if os.path.exists(c):
            return c
    return ""


def resolve_chrome_executable(chrome_executable=""):
    key = get_platform_key()
    candidates = _resolve_browser_candidates(chrome_executable=chrome_executable)
    existing = _resolve_existing_executable(candidates)
    if existing:
        return existing
    if candidates:
        return candidates[0]
    if key == "macos":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if key == "windows":
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    return "google-chrome"


def _resolve_browser_family(exec_path):
    name = str(exec_path or "").lower()
    if "msedge" in name or "edge" in name:
        return "edge"
    if "chromium" in name:
        return "chromium"
    return "chrome"


def _split_cli_args(raw, platform_key):
    s = str(raw or "").strip()
    if not s:
        return []
    try:
        return [x for x in shlex.split(s, posix=(platform_key != "windows")) if str(x or "").strip()]
    except Exception:
        return [x.strip() for x in s.split() if x.strip()]


def _get_debug_extra_args(platform_key):
    # 允许通过环境变量扩展启动参数（示例：CHROME_DEBUG_EXTRA_ARGS="--disable-gpu --lang=zh-CN"）
    env_args = _split_cli_args(os.getenv("CHROME_DEBUG_EXTRA_ARGS", ""), platform_key)
    if platform_key != "windows":
        return env_args

    # Windows 默认启用前台计时与渲染策略，减少后台降频导致的动作抖动和延迟。
    win_defaults = [
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    return _dedupe_keep_order([*win_defaults, *env_args])


def _display_cmd(argv, platform_key):
    if platform_key == "windows":
        try:
            return subprocess.list2cmdline(list(argv))
        except Exception:
            return " ".join(argv)
    return " ".join(shlex.quote(x) for x in argv)


def build_chrome_debug_launch_args(port=9222, user_data_path="./user_data", chrome_executable="", startup_url=""):
    """
    返回可直接用于 subprocess.Popen 的参数数组，以及展示给用户的命令文本。
    """
    key = get_platform_key()
    candidates = _resolve_browser_candidates(chrome_executable=chrome_executable)
    resolved = _resolve_existing_executable(candidates)
    ordered_execs = _dedupe_keep_order([resolved, *candidates])
    exec_path = ordered_execs[0] if ordered_execs else resolve_chrome_executable(chrome_executable=chrome_executable)
    user_data_abs = str(Path(user_data_path).expanduser().resolve())
    extra_args = _get_debug_extra_args(key)

    def _make_argv(executable):
        argv = [
            str(executable),
            f"--remote-debugging-port={int(port)}",
            f"--user-data-dir={user_data_abs}",
        ]
        if extra_args:
            argv.extend(list(extra_args))
        if startup_url:
            argv.append(str(startup_url))
        return argv

    argv_candidates = [_make_argv(executable) for executable in ordered_execs]
    argv = argv_candidates[0] if argv_candidates else _make_argv(exec_path)
    display = _display_cmd(argv, key)
    return {
        "platform_key": key,
        "platform_label": get_platform_label(),
        "argv": argv,
        "argv_candidates": argv_candidates or [argv],
        "display": display,
        "browser_family": _resolve_browser_family(exec_path),
        "resolved_executable": exec_path,
    }


def build_chrome_debug_commands(port=9222, user_data_path="./user_data", chrome_executable="", startup_url=""):
    key = get_platform_key()
    user_data_abs = str(Path(user_data_path).expanduser().resolve())
    commands = []
    alternatives = []
    launch_plan = build_chrome_debug_launch_args(
        port=port,
        user_data_path=user_data_path,
        chrome_executable=chrome_executable,
        startup_url=startup_url,
    )
    primary_argv = launch_plan.get("argv") or []
    if primary_argv:
        commands.append(_display_cmd(primary_argv, key))

    if key == "macos":
        suffix = f" \"{startup_url}\"" if startup_url else ""
        alternatives.append(
            f"open -na \"Google Chrome\" --args --remote-debugging-port={port} --user-data-dir=\"{user_data_abs}\"{suffix}"
        )
        alternatives.append(
            f"open -na \"Microsoft Edge\" --args --remote-debugging-port={port} --user-data-dir=\"{user_data_abs}\"{suffix}"
        )
    elif key == "windows":
        user_data_win = user_data_abs.replace("/", "\\")
        suffix = f" \"{startup_url}\"" if startup_url else ""
        # 直接可执行命令（PowerShell/CMD 通用）
        alternatives.append(
            f"\"chrome.exe\" --remote-debugging-port={port} --user-data-dir=\"{user_data_win}\"{suffix}"
        )
        alternatives.append(
            f"\"msedge.exe\" --remote-debugging-port={port} --user-data-dir=\"{user_data_win}\"{suffix}"
        )
        # PowerShell 显式写法（更适合脚本化）
        ps_args = [f"'--remote-debugging-port={port}'", f"'--user-data-dir={user_data_win}'"]
        if startup_url:
            ps_args.append(f"'{startup_url}'")
        ps_arglist = ", ".join(ps_args)
        alternatives.append(
            f"Start-Process -FilePath \"chrome.exe\" -ArgumentList {ps_arglist}"
        )
        alternatives.append(
            f"Start-Process -FilePath \"msedge.exe\" -ArgumentList {ps_arglist}"
        )
    else:
        suffix = f" \"{startup_url}\"" if startup_url else ""
        alternatives.append(
            f"google-chrome --remote-debugging-port={port} --user-data-dir=\"{user_data_abs}\"{suffix}"
        )
        alternatives.append(
            f"microsoft-edge --remote-debugging-port={port} --user-data-dir=\"{user_data_abs}\"{suffix}"
        )
        alternatives.append(
            f"chromium --remote-debugging-port={port} --user-data-dir=\"{user_data_abs}\"{suffix}"
        )

    # 加入可执行候选命令，便于用户快速复制切换浏览器。
    for argv in launch_plan.get("argv_candidates") or []:
        cmd = _display_cmd(argv, key)
        if cmd not in commands and cmd not in alternatives:
            alternatives.append(cmd)

    return {
        "platform_key": key,
        "platform_label": get_platform_label(),
        "primary": commands[0] if commands else "",
        "alternatives": alternatives,
        "browser_family": launch_plan.get("browser_family", "chrome"),
        "resolved_executable": launch_plan.get("resolved_executable", ""),
    }


def get_microphone_permission_guide(browser_name="浏览器"):
    key = get_platform_key()
    if key == "macos":
        return f"macOS：系统设置 -> 隐私与安全性 -> 麦克风，允许 {browser_name} 与终端（运行本项目的程序）访问麦克风。"
    if key == "windows":
        return f"Windows：设置 -> 隐私和安全性 -> 麦克风，开启“麦克风访问”和“允许桌面应用访问麦克风”，并确认 {browser_name} 有权限。"
    return f"Linux：在系统音频/隐私设置中允许 {browser_name} 与当前终端访问麦克风。"
