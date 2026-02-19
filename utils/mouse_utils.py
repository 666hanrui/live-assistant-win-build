import math
import platform
import random
import time

from utils.logger import logger

try:
    import pyautogui
except Exception as e:  # pragma: no cover - 环境差异
    pyautogui = None
    _PYAUTOGUI_IMPORT_ERROR = str(e)
else:
    _PYAUTOGUI_IMPORT_ERROR = ""
    pyautogui.FAILSAFE = False
    # 关闭 pyautogui 的全局隐式暂停，避免每一步 move/click 额外 +0.1s 导致动作过慢。
    pyautogui.PAUSE = 0.0


def _is_available():
    return pyautogui is not None


def _ease_in_out(t):
    # smootherstep: 起步慢 -> 中段快 -> 收尾慢
    t = max(0.0, min(1.0, float(t)))
    return t * t * t * (t * (t * 6 - 15) + 10)


def _lerp(a, b, t):
    return a + (b - a) * t


def _bezier3(p0, p1, p2, p3, t):
    mt = 1.0 - t
    return (
        (mt ** 3) * p0
        + 3 * (mt ** 2) * t * p1
        + 3 * mt * (t ** 2) * p2
        + (t ** 3) * p3
    )


def _random_sleep(min_s, max_s):
    lo = max(0.0, float(min_s or 0.0))
    hi = max(lo, float(max_s or lo))
    time.sleep(random.uniform(lo, hi))


def _generate_track(start_pos, end_pos, duration, steps, jitter_px=1.5):
    x1, y1 = float(start_pos[0]), float(start_pos[1])
    x2, y2 = float(end_pos[0]), float(end_pos[1])
    dx, dy = (x2 - x1), (y2 - y1)
    dist = math.hypot(dx, dy)

    # 沿路径方向的控制点 + 垂直偏移，让轨迹更像人手弧线
    if dist < 1:
        return [(x2, y2)], []
    nx, ny = dx / dist, dy / dist
    px, py = -ny, nx

    c1_len = dist * random.uniform(0.22, 0.38)
    c2_len = dist * random.uniform(0.58, 0.82)
    side = random.choice([-1.0, 1.0])
    bend = min(42.0, max(6.0, dist * random.uniform(0.08, 0.18))) * side
    ctrl1 = (x1 + nx * c1_len + px * bend, y1 + ny * c1_len + py * bend)
    ctrl2 = (x1 + nx * c2_len - px * bend * random.uniform(0.6, 1.0), y1 + ny * c2_len - py * bend * random.uniform(0.6, 1.0))

    points = []
    # 时间片加入细小随机，避免恒定采样间隔
    base_step = max(0.0015, float(duration) / max(4, steps))
    step_intervals = []
    for i in range(steps + 1):
        t = _ease_in_out(i / float(steps))
        bx = _bezier3(x1, ctrl1[0], ctrl2[0], x2, t)
        by = _bezier3(y1, ctrl1[1], ctrl2[1], y2, t)
        # 抖动在起止两端衰减，避免落点漂移
        fade = 1.0 - abs(2.0 * (i / float(steps)) - 1.0)
        jitter = jitter_px * fade
        jx = random.uniform(-jitter, jitter)
        jy = random.uniform(-jitter, jitter)
        points.append((bx + jx, by + jy))
        step_intervals.append(base_step * random.uniform(0.72, 1.34))

    if points:
        points[-1] = (x2, y2)
    return points, step_intervals


def human_pause(min_seconds=0.03, max_seconds=0.14, reason=""):
    lo = max(0.0, float(min_seconds or 0.0))
    hi = max(lo, float(max_seconds or lo))
    slept = random.uniform(lo, hi)
    time.sleep(slept)
    if reason:
        logger.debug(f"human_pause: {reason}")
    return slept


def _is_mac():
    return (platform.system() or "").lower() == "darwin"


def human_move_to(x, y, duration=None, jitter_px=1.8, overshoot=True):
    """
    模拟真人鼠标移动轨迹（曲线 + 微抖动 + 末端微调）。
    """
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")

    target_x, target_y = int(x), int(y)
    sx, sy = pyautogui.position()
    dist = math.hypot(target_x - sx, target_y - sy)
    if dist < 2:
        return

    if duration is None:
        # 距离越长耗时越长，加入随机扰动
        duration = _lerp(0.18, 0.82, min(1.0, dist / 1200.0)) + random.uniform(0.01, 0.09)
    duration = max(0.12, min(1.25, float(duration)))
    steps = int(max(10, min(68, dist * random.uniform(0.065, 0.11))))

    # 长距离时先轻微过冲，再回落到目标点，更像真人收手
    waypoints = [(target_x, target_y)]
    if overshoot and dist >= 140:
        ux = (target_x - sx) / dist
        uy = (target_y - sy) / dist
        overshoot_px = random.uniform(4.0, min(18.0, dist * 0.07))
        ox = int(target_x + ux * overshoot_px + random.uniform(-2.0, 2.0))
        oy = int(target_y + uy * overshoot_px + random.uniform(-2.0, 2.0))
        waypoints = [(ox, oy), (target_x, target_y)]

    cur = (sx, sy)
    remain = duration
    for idx, wp in enumerate(waypoints):
        seg_ratio = 0.72 if idx == 0 and len(waypoints) > 1 else 1.0
        seg_dur = max(0.04, remain * seg_ratio)
        seg_steps = max(6, int(steps * seg_ratio))
        pts, intervals = _generate_track(cur, wp, seg_dur, seg_steps, jitter_px=jitter_px)
        for p, dt in zip(pts, intervals):
            pyautogui.moveTo(int(p[0]), int(p[1]))
            time.sleep(dt)
        cur = wp
        remain = max(0.04, remain - seg_dur)

    # 末端微抖 + 矫正
    if jitter_px > 0:
        pyautogui.moveRel(random.uniform(-jitter_px, jitter_px), random.uniform(-jitter_px, jitter_px), duration=random.uniform(0.01, 0.05))
    pyautogui.moveTo(target_x, target_y, duration=random.uniform(0.02, 0.08), tween=pyautogui.easeOutQuad)


def human_click(
    x=None,
    y=None,
    button="left",
    clicks=1,
    jitter_px=1.6,
    pre_delay=(0.03, 0.16),
    down_up_delay=(0.02, 0.09),
    post_delay=(0.02, 0.11),
):
    """
    拟人化点击：
    1) 曲线移动到目标点
    2) 点击前轻微停顿
    3) mouseDown / mouseUp 间隔随机
    """
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")

    if x is not None and y is not None:
        human_move_to(int(x), int(y), jitter_px=jitter_px, overshoot=True)

    _random_sleep(pre_delay[0], pre_delay[1])
    for idx in range(max(1, int(clicks or 1))):
        pyautogui.mouseDown(button=button)
        _random_sleep(down_up_delay[0], down_up_delay[1])
        pyautogui.mouseUp(button=button)
        if idx + 1 < clicks:
            _random_sleep(0.04, 0.12)
    _random_sleep(post_delay[0], post_delay[1])
    logger.info(f"点击位置: {pyautogui.position()}")


def human_select_all_and_delete():
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")
    if _is_mac():
        pyautogui.hotkey("command", "a")
    else:
        pyautogui.hotkey("ctrl", "a")
    _random_sleep(0.02, 0.06)
    pyautogui.press("backspace")
    _random_sleep(0.02, 0.06)


def human_typewrite(
    text,
    min_interval=0.018,
    max_interval=0.055,
    pause_prob=0.08,
    pause_min=0.04,
    pause_max=0.14,
):
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")
    s = str(text or "")
    if not s:
        return
    lo = max(0.001, float(min_interval or 0.018))
    hi = max(lo, float(max_interval or lo))
    for ch in s:
        pyautogui.write(ch, interval=random.uniform(lo, hi))
        if ch in {",", ".", "!", "?", "，", "。", "！", "？"}:
            _random_sleep(0.01, 0.05)
        if random.random() < float(pause_prob or 0.0):
            _random_sleep(pause_min, pause_max)


def human_paste(text):
    """
    将文本写入系统剪贴板并模拟粘贴快捷键。
    返回 True 表示已执行粘贴动作，False 表示当前环境不支持。
    """
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")
    s = str(text or "")
    if not s:
        return False
    try:
        import pyperclip  # type: ignore
    except Exception:
        return False
    try:
        pyperclip.copy(s)
    except Exception:
        return False
    if _is_mac():
        pyautogui.hotkey("command", "v")
    else:
        pyautogui.hotkey("ctrl", "v")
    _random_sleep(0.02, 0.08)
    return True


def human_press(key, min_delay=0.015, max_delay=0.07):
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")
    _random_sleep(min_delay, max_delay)
    pyautogui.press(key)
