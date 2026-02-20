import time
import threading
from typing import Dict, Optional

import cv2
import numpy as np


class ScreenCapture:
    """
    跨平台屏幕采集：
    - 优先 mss（更快）
    - 回退 pyautogui.screenshot（兼容）
    """

    def __init__(self):
        self.backend = ""
        self.last_error = ""
        self._mss = None
        self._mss_instance = None
        self._mss_monitors = []
        self._lock = threading.Lock()
        self._init_backend()

    def _init_backend(self):
        try:
            import mss  # type: ignore

            self._mss = mss
            self.backend = "mss"
            return
        except Exception as e:
            self.last_error = f"mss_unavailable:{e}"

        try:
            import pyautogui  # type: ignore

            self._pyautogui = pyautogui
            self.backend = "pyautogui"
            return
        except Exception as e:
            self.last_error = f"pyautogui_unavailable:{e}"
            self.backend = ""

    def available(self) -> bool:
        return bool(self.backend)

    def _get_mss_instance(self):
        if not self._mss:
            return None
        if self._mss_instance is None:
            self._mss_instance = self._mss.mss()
            self._mss_monitors = list(getattr(self._mss_instance, "monitors", []) or [])
        return self._mss_instance

    def _reset_mss_instance(self):
        inst = self._mss_instance
        self._mss_instance = None
        self._mss_monitors = []
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self._reset_mss_instance()
        except Exception:
            pass

    def _capture_with_mss(self, monitor_index: int = 1) -> Optional[Dict]:
        if not self._mss:
            return None
        with self._lock:
            try:
                sct = self._get_mss_instance()
                monitors = list(self._mss_monitors or [])
                if len(monitors) <= 1:
                    self._reset_mss_instance()
                    sct = self._get_mss_instance()
                    monitors = list(self._mss_monitors or [])
                if len(monitors) <= 1:
                    return None
                idx = max(1, min(int(monitor_index), len(monitors) - 1))
                mon = monitors[idx]
                shot = sct.grab(mon)
                arr = np.array(shot)
                # BGRA -> BGR
                if arr.ndim == 3 and arr.shape[2] == 4:
                    img = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
                else:
                    img = arr
                return {
                    "image": img,
                    "left": int(mon.get("left", 0)),
                    "top": int(mon.get("top", 0)),
                    "width": int(mon.get("width", img.shape[1] if img is not None else 0)),
                    "height": int(mon.get("height", img.shape[0] if img is not None else 0)),
                    "backend": "mss",
                }
            except Exception:
                # mss 上下文可能在系统切屏/锁屏后失效，重建一次。
                self._reset_mss_instance()
                return None

    def _capture_with_pyautogui(self) -> Optional[Dict]:
        try:
            shot = self._pyautogui.screenshot()
            arr = np.array(shot)
            # RGB -> BGR
            img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            h, w = img.shape[:2]
            return {"image": img, "left": 0, "top": 0, "width": int(w), "height": int(h), "backend": "pyautogui"}
        except Exception:
            return None

    def capture(self, monitor_index: int = 1) -> Dict:
        start = time.time()
        if not self.available():
            return {"ok": False, "error": self.last_error or "screen_capture_unavailable", "backend": ""}

        try:
            frame = None
            if self.backend == "mss":
                frame = self._capture_with_mss(monitor_index=monitor_index)
                if frame is None:
                    # mss 获取失败时兜底 pyautogui
                    try:
                        import pyautogui  # type: ignore

                        self._pyautogui = pyautogui
                        frame = self._capture_with_pyautogui()
                    except Exception:
                        frame = None
            elif self.backend == "pyautogui":
                frame = self._capture_with_pyautogui()

            if not frame or frame.get("image") is None:
                return {
                    "ok": False,
                    "error": "capture_failed",
                    "backend": self.backend,
                    "elapsed_ms": int((time.time() - start) * 1000),
                }

            frame["ok"] = True
            frame["elapsed_ms"] = int((time.time() - start) * 1000)
            return frame
        except Exception as e:
            self.last_error = str(e)
            return {
                "ok": False,
                "error": str(e),
                "backend": self.backend,
                "elapsed_ms": int((time.time() - start) * 1000),
            }
