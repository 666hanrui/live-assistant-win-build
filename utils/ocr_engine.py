import base64
import json
import time
import urllib.error
import urllib.request
from typing import Dict, Tuple

import cv2
import numpy as np

import app_config.settings as settings


class LocalOcrEngine:
    """
    OCR 引擎统一接口。

    历史上这里是本地 OCR provider 封装；当前已切换为 Qwen 云端 OCR，
    继续保留类名以减少上层改动。
    """

    def __init__(self):
        self.provider = "qwen_ocr"
        self.last_error = ""
        self._available = bool(
            getattr(settings, "QWEN_OCR_ENABLED", True)
            and str(getattr(settings, "QWEN_OCR_API_KEY", "") or "").strip()
        )
        if not self._available:
            self.last_error = "qwen_ocr_not_configured"

    def available(self) -> bool:
        return bool(self._available)

    @staticmethod
    def decode_png_bytes(png_bytes: bytes) -> Tuple[bool, np.ndarray]:
        if not png_bytes:
            return False, None
        arr = np.frombuffer(png_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return (img is not None), img

    @staticmethod
    def _location_to_rect(location) -> Dict:
        if not isinstance(location, (list, tuple)) or len(location) < 8:
            return {}
        try:
            xs = [float(location[i]) for i in range(0, len(location), 2)]
            ys = [float(location[i]) for i in range(1, len(location), 2)]
            x1 = int(max(0.0, min(xs)))
            y1 = int(max(0.0, min(ys)))
            x2 = int(max(x1 + 1, max(xs)))
            y2 = int(max(y1 + 1, max(ys)))
            return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        except Exception:
            return {}

    @staticmethod
    def _extract_words_info(payload: Dict):
        try:
            choices = (((payload or {}).get("output") or {}).get("choices") or [])
            if not choices:
                return []
            message = (choices[0] or {}).get("message") or {}
            content = message.get("content") or []
            if not isinstance(content, list):
                return []
            for item in content:
                if not isinstance(item, dict):
                    continue
                ocr_result = item.get("ocr_result") or {}
                words_info = ocr_result.get("words_info") or []
                if isinstance(words_info, list):
                    return words_info
        except Exception:
            return []
        return []

    @staticmethod
    def _build_lines(words_info) -> Dict:
        parts = []
        lines = []
        for item in words_info or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("word") or "").strip()
            if not text:
                continue
            rect = LocalOcrEngine._location_to_rect(item.get("location"))
            if not rect:
                continue
            parts.append(text)
            lines.append({"text": text, "score": None, "rect": rect})

        lines.sort(
            key=lambda line: (
                float(((line or {}).get("rect") or {}).get("y1") or 0.0),
                float(((line or {}).get("rect") or {}).get("x1") or 0.0),
            )
        )
        return {"text": "\n".join(parts), "lines": lines}

    @staticmethod
    def _encode_png_data_uri(img_bgr: np.ndarray) -> str:
        ok, buf = cv2.imencode(".png", img_bgr)
        if not ok:
            raise RuntimeError("png_encode_failed")
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    def _request_qwen_ocr(self, img_bgr: np.ndarray) -> Dict:
        if not self.available():
            raise RuntimeError(self.last_error or "qwen_ocr_not_available")

        image_data_uri = self._encode_png_data_uri(img_bgr)
        body = {
            "model": str(getattr(settings, "QWEN_OCR_MODEL", "qwen-vl-ocr-latest") or "qwen-vl-ocr-latest"),
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "image": image_data_uri,
                                "min_pixels": int(getattr(settings, "QWEN_OCR_MIN_PIXELS", 32 * 32 * 3) or (32 * 32 * 3)),
                                "max_pixels": int(getattr(settings, "QWEN_OCR_MAX_PIXELS", 32 * 32 * 8192) or (32 * 32 * 8192)),
                            },
                            {"text": "OCR"},
                        ],
                    }
                ]
            },
            "parameters": {
                "temperature": 0,
                "top_p": 0.01,
                "result_format": "message",
                "ocr_options": {
                    "task": "advanced_recognition",
                    "enable_rotate": bool(getattr(settings, "QWEN_OCR_ENABLE_ROTATE", False)),
                },
            },
        }

        req = urllib.request.Request(
            str(getattr(settings, "QWEN_OCR_BASE_URL", "") or "").strip(),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {str(getattr(settings, 'QWEN_OCR_API_KEY', '') or '').strip()}",
            },
            method="POST",
        )
        timeout = float(getattr(settings, "QWEN_OCR_TIMEOUT_SECONDS", 12.0) or 12.0)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw) if raw else {}

    def recognize(self, img_bgr: np.ndarray, preprocess: bool = True) -> Dict:
        if img_bgr is None or not isinstance(img_bgr, np.ndarray) or img_bgr.size == 0:
            return {
                "ok": False,
                "text": "",
                "provider": self.provider,
                "error": "invalid_image",
            }
        if not self.available():
            return {
                "ok": False,
                "text": "",
                "provider": self.provider,
                "error": self.last_error or "qwen_ocr_not_configured",
            }

        start = time.time()
        try:
            src = img_bgr
            if preprocess:
                gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (3, 3), 0)
                thr = cv2.adaptiveThreshold(
                    gray,
                    255,
                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY,
                    31,
                    8,
                )
                src = cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR)

            payload = self._request_qwen_ocr(src)
            words_info = self._extract_words_info(payload)
            parsed = self._build_lines(words_info)
            text = str(parsed.get("text") or "").strip()
            lines = parsed.get("lines") or []
            if not text or not lines:
                self.last_error = "qwen_ocr_empty_result"
                return {
                    "ok": False,
                    "text": text,
                    "lines": lines,
                    "provider": self.provider,
                    "error": self.last_error,
                    "elapsed_ms": int((time.time() - start) * 1000),
                }
            self.last_error = ""
            return {
                "ok": True,
                "text": text,
                "lines": lines,
                "provider": self.provider,
                "error": "",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        except urllib.error.HTTPError as e:
            self.last_error = f"http_{getattr(e, 'code', 'error')}"
        except urllib.error.URLError as e:
            self.last_error = f"network_error:{e.reason}"
        except Exception as e:
            self.last_error = str(e)
        return {
            "ok": False,
            "text": "",
            "lines": [],
            "provider": self.provider,
            "error": self.last_error or "qwen_ocr_failed",
            "elapsed_ms": int((time.time() - start) * 1000),
        }
