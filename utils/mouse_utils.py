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
    # 关闭 pyautogui 的内部最小时延阈值，避免 Windows 下 move/click 被强制放慢。
    try:
        pyautogui.MINIMUM_DURATION = 0.0
    except Exception:
        pass
    try:
        pyautogui.MINIMUM_SLEEP = 0.0
    except Exception:
        pass
    try:
        pyautogui.DARWIN_CATCH_UP_TIME = 0.0
    except Exception:
        pass


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


def _build_hand_noise(steps, amplitude):
    """生成平滑手抖噪声，首尾衰减到 0，避免起止点漂移。"""
    steps = max(1, int(steps or 1))
    amp = max(0.0, float(amplitude or 0.0))
    if amp <= 0:
        return [0.0 for _ in range(steps + 1)]

    seq = [0.0]
    vel = 0.0
    for _ in range(steps):
        vel = 0.66 * vel + random.gauss(0.0, amp * 0.24)
        seq.append(vel)

    s0 = float(seq[0])
    s1 = float(seq[-1])
    for i in range(steps + 1):
        t = i / float(steps)
        # 去线性漂移 + 首尾门控，保证落点稳定
        drift = _lerp(s0, s1, t)
        gate = math.sin(math.pi * t) ** 1.2
        seq[i] = (seq[i] - drift) * gate
    return seq


def _build_step_intervals(duration, steps):
    """构造非匀速时间片：中段更快，两端更慢，总时长接近 duration。"""
    total = max(0.01, float(duration or 0.01))
    steps = max(1, int(steps or 1))
    weights = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        # 人类移动常见的“起步慢-中段快-收尾慢”
        v = 0.30 + (math.sin(math.pi * t) ** 1.35)
        v *= random.uniform(0.84, 1.16)
        weights.append(max(0.05, v))
    inv = total / max(1e-6, sum(weights))
    return [max(0.0012, w * inv) for w in weights]


def _generate_track(start_pos, end_pos, duration, steps, jitter_px=1.5):
    x1, y1 = float(start_pos[0]), float(start_pos[1])
    x2, y2 = float(end_pos[0]), float(end_pos[1])
    dx, dy = (x2 - x1), (y2 - y1)
    dist = math.hypot(dx, dy)

    # 沿路径方向的控制点 + 垂直偏移，让轨迹更像人手弧线
    if dist < 1:
        return [(x2, y2)], [max(0.0015, float(duration or 0.01))]
    nx, ny = dx / dist, dy / dist
    px, py = -ny, nx

    c1_len = dist * random.uniform(0.22, 0.38)
    c2_len = dist * random.uniform(0.58, 0.82)
    side = random.choice([-1.0, 1.0])
    bend = min(42.0, max(6.0, dist * random.uniform(0.08, 0.18))) * side
    ctrl1 = (x1 + nx * c1_len + px * bend, y1 + ny * c1_len + py * bend)
    ctrl2 = (x1 + nx * c2_len - px * bend * random.uniform(0.6, 1.0), y1 + ny * c2_len - py * bend * random.uniform(0.6, 1.0))

    points = []
    step_intervals = _build_step_intervals(duration, steps)
    # 两轴噪声独立，形成更自然的细抖动
    noise_x = _build_hand_noise(steps, max(0.0, float(jitter_px or 0.0)))
    noise_y = _build_hand_noise(steps, max(0.0, float(jitter_px or 0.0)))
    for i in range(1, steps + 1):
        u = i / float(steps)
        t = _ease_in_out(u)
        bx = _bezier3(x1, ctrl1[0], ctrl2[0], x2, t)
        by = _bezier3(y1, ctrl1[1], ctrl2[1], y2, t)
        # 起止点降低噪声，中段略提升，尾段便于微调
        fade = math.sin(math.pi * u) ** 1.1
        jitter = max(0.0, float(jitter_px or 0.0)) * (0.24 + 0.76 * fade)
        jx = noise_x[i] + random.gauss(0.0, jitter * 0.18)
        jy = noise_y[i] + random.gauss(0.0, jitter * 0.18)
        points.append((bx + jx, by + jy))

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
    jitter_px = max(0.0, float(jitter_px or 0.0))

    if duration is None:
        # 距离越长耗时越长，加入随机扰动
        duration = _lerp(0.18, 0.82, min(1.0, dist / 1200.0)) + random.uniform(0.01, 0.09)
    duration = max(0.12, min(1.25, float(duration)))

    # 长距离时先轻微过冲，再回落到目标点，更像真人收手
    waypoints = [(target_x, target_y)]
    if overshoot and dist >= 140:
        ux = (target_x - sx) / dist
        uy = (target_y - sy) / dist
        px, py = -uy, ux
        overshoot_px = random.uniform(4.0, min(22.0, dist * 0.08))
        lateral = random.uniform(-7.0, 7.0)
        ox = int(target_x + ux * overshoot_px + px * lateral + random.uniform(-2.0, 2.0))
        oy = int(target_y + uy * overshoot_px + py * lateral + random.uniform(-2.0, 2.0))
        waypoints = [(ox, oy), (target_x, target_y)]

    cur = (sx, sy)
    total_path = 0.0
    last = cur
    for wp in waypoints:
        total_path += math.hypot(float(wp[0]) - float(last[0]), float(wp[1]) - float(last[1]))
        last = wp
    remain = duration
    for idx, wp in enumerate(waypoints):
        seg_dist = math.hypot(float(wp[0]) - float(cur[0]), float(wp[1]) - float(cur[1]))
        if seg_dist < 1:
            cur = wp
            continue
        seg_ratio = seg_dist / max(1.0, total_path)
        seg_dur = max(0.035, remain if idx == len(waypoints) - 1 else duration * seg_ratio)
        seg_steps = int(max(8, min(88, seg_dist * random.uniform(0.058, 0.105))))
        seg_jitter = min(max(0.5, jitter_px), max(0.8, seg_dist * 0.02))
        pts, intervals = _generate_track(cur, wp, seg_dur, seg_steps, jitter_px=seg_jitter)
        for p, dt in zip(pts, intervals):
            pyautogui.moveTo(int(p[0]), int(p[1]))
            time.sleep(dt)
            # 偶发短停顿，模拟“目测校准”的微停
            if random.random() < 0.045:
                time.sleep(random.uniform(0.004, 0.019))
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


def human_scroll(
    units,
    x=None,
    y=None,
    pre_delay=(0.02, 0.10),
    post_delay=(0.02, 0.09),
    move_jitter_px=1.2,
):
    """
    拟人化滚轮：
    1) 可选移动到目标位置
    2) 滚动前后随机短停顿
    3) 执行滚轮（正数向上，负数向下）
    """
    if not _is_available():
        raise RuntimeError(f"pyautogui_unavailable:{_PYAUTOGUI_IMPORT_ERROR}")
    if x is not None and y is not None:
        human_move_to(int(x), int(y), jitter_px=move_jitter_px, overshoot=False)
    _random_sleep(pre_delay[0], pre_delay[1])
    pyautogui.scroll(int(units or 0))
    _random_sleep(post_delay[0], post_delay[1])
    logger.info(f"滚轮位置: {pyautogui.position()}, units={int(units or 0)}")


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
