import time
from typing import Dict, Tuple

import cv2
import numpy as np


class LocalOcrEngine:
    """
    本地 OCR 轻量封装（按可用性自动选择）：
    1) rapidocr_onnxruntime
    2) paddleocr
    3) pytesseract
    """

    def __init__(self):
        self.provider = None
        self._runner = None
        self.last_error = ""
        self._init_provider()

    def _init_provider(self):
        attempt_errors = []
        # rapidocr_onnxruntime
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            engine = RapidOCR()

            def _run_rapid(img_bgr):
                result, _ = engine(img_bgr)
                parts = []
                lines = []
                for item in result or []:
                    if not item or len(item) < 2:
                        continue
                    box = item[0]
                    text = str(item[1] or "").strip()
                    score = float(item[2]) if len(item) > 2 and item[2] is not None else None
                    if text:
                        parts.append(text)
                        rect = self._box_to_rect(box)
                        if rect:
                            lines.append({"text": text, "score": score, "rect": rect})
                return {"text": "\n".join(parts), "lines": lines}

            self.provider = "rapidocr"
            self._runner = _run_rapid
            self.last_error = ""
            return
        except Exception as e:
            attempt_errors.append(f"rapidocr:{str(e)[:180]}")

        # paddleocr
        try:
            from paddleocr import PaddleOCR  # type: ignore

            engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

            def _run_paddle(img_bgr):
                out = engine.ocr(img_bgr, cls=True)
                parts = []
                lines = []
                for block in out or []:
                    for line in block or []:
                        if not line or len(line) < 2:
                            continue
                        box = line[0]
                        txt = ""
                        score = None
                        seg = line[1]
                        if isinstance(seg, (list, tuple)) and seg:
                            txt = str(seg[0] or "").strip()
                            if len(seg) > 1:
                                try:
                                    score = float(seg[1])
                                except Exception:
                                    score = None
                        else:
                            txt = str(seg or "").strip()
                        if txt:
                            parts.append(txt)
                            rect = self._box_to_rect(box)
                            if rect:
                                lines.append({"text": txt, "score": score, "rect": rect})
                return {"text": "\n".join(parts), "lines": lines}

            self.provider = "paddleocr"
            self._runner = _run_paddle
            self.last_error = ""
            return
        except Exception as e:
            attempt_errors.append(f"paddleocr:{str(e)[:180]}")

        # pytesseract
        try:
            import pytesseract  # type: ignore

            def _run_tesseract(img_bgr):
                rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                data = pytesseract.image_to_data(rgb, lang="eng+chi_sim", output_type=pytesseract.Output.DICT)
                n = len(data.get("text", []))
                parts = []
                lines = []
                for i in range(n):
                    txt = str((data.get("text") or [""])[i] or "").strip()
                    if not txt:
                        continue
                    try:
                        conf = float((data.get("conf") or ["-1"])[i])
                    except Exception:
                        conf = None
                    x = int((data.get("left") or [0])[i])
                    y = int((data.get("top") or [0])[i])
                    w = int((data.get("width") or [0])[i])
                    h = int((data.get("height") or [0])[i])
                    if w <= 0 or h <= 0:
                        continue
                    parts.append(txt)
                    lines.append({
                        "text": txt,
                        "score": conf / 100.0 if conf is not None and conf >= 0 else None,
                        "rect": {"x1": x, "y1": y, "x2": x + w, "y2": y + h},
                    })
                return {"text": "\n".join(parts), "lines": lines}

            self.provider = "tesseract"
            self._runner = _run_tesseract
            self.last_error = ""
            return
        except Exception as e:
            attempt_errors.append(f"pytesseract:{str(e)[:180]}")
            self.last_error = f"ocr_provider_unavailable:{'; '.join(attempt_errors)}"

    def available(self) -> bool:
        return bool(self._runner)

    def _preprocess(self, img_bgr: np.ndarray) -> np.ndarray:
        # 简单增强：灰度 + 自适应阈值，再转回 BGR
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        thr = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 8
        )
        return cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR)

    @staticmethod
    def _box_to_rect(box):
        try:
            if not box:
                return None
            pts = np.array(box, dtype=np.float32).reshape(-1, 2)
            xs = pts[:, 0]
            ys = pts[:, 1]
            x1 = int(max(0, np.min(xs)))
            y1 = int(max(0, np.min(ys)))
            x2 = int(max(x1 + 1, np.max(xs)))
            y2 = int(max(y1 + 1, np.max(ys)))
            return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        except Exception:
            return None

    def recognize(self, img_bgr: np.ndarray, preprocess: bool = True) -> Dict:
        if img_bgr is None or not isinstance(img_bgr, np.ndarray) or img_bgr.size == 0:
            return {"ok": False, "text": "", "provider": self.provider or "", "error": "invalid_image"}
        if not self.available():
            return {
                "ok": False,
                "text": "",
                "provider": self.provider or "",
                "error": self.last_error or "ocr_provider_unavailable",
            }

        start = time.time()
        try:
            src = self._preprocess(img_bgr) if preprocess else img_bgr
            raw = self._runner(src) or {}
            if isinstance(raw, dict):
                text = str(raw.get("text") or "").strip()
                lines = raw.get("lines") or []
            else:
                text = str(raw or "").strip()
                lines = []
            return {
                "ok": bool(text),
                "text": text,
                "lines": lines if isinstance(lines, list) else [],
                "provider": self.provider or "",
                "error": "" if text else "empty_text",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        except Exception as e:
            self.last_error = str(e)
            return {
                "ok": False,
                "text": "",
                "provider": self.provider or "",
                "error": str(e),
                "elapsed_ms": int((time.time() - start) * 1000),
            }

    @staticmethod
    def decode_png_bytes(png_bytes: bytes) -> Tuple[bool, np.ndarray]:
        if not png_bytes:
            return False, None
        arr = np.frombuffer(png_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return (img is not None), img
