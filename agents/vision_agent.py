from DrissionPage import ChromiumPage, ChromiumOptions
from utils.logger import logger
from utils.page_ocr_reader import PageOCRReader
from utils.screen_capture import ScreenCapture
import app_config.settings as settings
import time
import re
from pathlib import Path
import cv2

class VisionAgent:
    def __init__(self, port=settings.BROWSER_PORT):
        self.port = port
        self.page = None
        self.info_source_mode = self._normalize_info_source_mode(getattr(settings, "WEB_INFO_SOURCE_MODE", "screen_ocr"))
        self.ocr_reader = PageOCRReader()
        self.last_message_ids = set()
        self.last_reconnect_at = 0.0
        self.last_alive_probe_at = 0.0
        self.last_alive_probe_ok = False
        self.alive_probe_interval = 0.6
        self.reconnect_cooldown = 3.0
        self.last_disconnect_log_at = 0.0
        self.last_wrong_tab_log_at = 0.0
        self.no_chat_rounds = 0
        self._last_ocr_scan = {}
        self.screen_capture = ScreenCapture()
        self.screen_monitor_index = max(1, int(getattr(settings, "SCREEN_CAPTURE_MONITOR_INDEX", 1) or 1))
        self._last_capture = {}
        self._last_screen_scan_at = 0.0
        self._screen_scan_cache_ttl = 0.55
        self._chat_roi = None
        self._fixed_chat_roi_enabled = bool(getattr(settings, "SCREEN_OCR_FIXED_CHAT_REGION_ENABLED", True))
        self._fixed_chat_roi_ratios = {
            "x1": float(getattr(settings, "SCREEN_OCR_FIXED_CHAT_REGION_X1_RATIO", 0.44) or 0.44),
            "y1": float(getattr(settings, "SCREEN_OCR_FIXED_CHAT_REGION_Y1_RATIO", 0.43) or 0.43),
            "x2": float(getattr(settings, "SCREEN_OCR_FIXED_CHAT_REGION_X2_RATIO", 0.72) or 0.72),
            "y2": float(getattr(settings, "SCREEN_OCR_FIXED_CHAT_REGION_Y2_RATIO", 0.90) or 0.90),
        }
        self._chat_roi_debug_enabled = bool(getattr(settings, "SCREEN_OCR_CHAT_ROI_DEBUG_ENABLED", False))
        self._chat_roi_debug_dir = str(getattr(settings, "SCREEN_OCR_CHAT_ROI_DEBUG_DIR", "logs/screen_chat_roi_debug") or "logs/screen_chat_roi_debug")
        self._chat_roi_debug_interval = max(
            0.2,
            float(getattr(settings, "SCREEN_OCR_CHAT_ROI_DEBUG_INTERVAL_SECONDS", 2.0) or 2.0),
        )
        self._chat_roi_debug_max_files = max(
            20,
            int(getattr(settings, "SCREEN_OCR_CHAT_ROI_DEBUG_MAX_FILES", 240) or 240),
        )
        self._last_chat_roi_debug_at = 0.0

    def _normalize_info_source_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode in {"ocr_only", "screen_ocr"}:
            return mode
        return "screen_ocr"

    def set_info_source_mode(self, mode):
        self.info_source_mode = self._normalize_info_source_mode(mode)
        return self.info_source_mode

    def get_info_source_mode(self):
        return self.info_source_mode

    def _is_ocr_info_enabled(self):
        return self.info_source_mode in {"ocr_only", "screen_ocr"}

    def _is_ocr_only_info_mode(self):
        return self.info_source_mode == "ocr_only"

    def _is_screen_ocr_mode(self):
        return self.info_source_mode == "screen_ocr"

    def _clamp_rect(self, rect, width, height):
        if not isinstance(rect, dict):
            return None
        try:
            x1 = int(max(0, min(int(rect.get("x1", 0)), max(0, width - 1))))
            y1 = int(max(0, min(int(rect.get("y1", 0)), max(0, height - 1))))
            x2 = int(max(1, min(int(rect.get("x2", width)), width)))
            y2 = int(max(1, min(int(rect.get("y2", height)), height)))
            if x2 - x1 < 16 or y2 - y1 < 16:
                return None
            return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        except Exception:
            return None

    @staticmethod
    def _ratio01(value, default):
        try:
            v = float(value)
        except Exception:
            v = float(default)
        return max(0.0, min(1.0, v))

    def _fixed_chat_roi_for_size(self, width, height):
        if (not self._fixed_chat_roi_enabled) or width <= 0 or height <= 0:
            return None
        x1 = self._ratio01(self._fixed_chat_roi_ratios.get("x1"), 0.44)
        y1 = self._ratio01(self._fixed_chat_roi_ratios.get("y1"), 0.43)
        x2 = self._ratio01(self._fixed_chat_roi_ratios.get("x2"), 0.72)
        y2 = self._ratio01(self._fixed_chat_roi_ratios.get("y2"), 0.90)
        # 比例配置兜底，避免反向区间导致空 ROI。
        if x2 <= x1:
            x1, x2 = min(x1, x2), max(x1, x2)
        if y2 <= y1:
            y1, y2 = min(y1, y2), max(y1, y2)
        if (x2 - x1) < 0.08:
            x2 = min(1.0, x1 + 0.08)
        if (y2 - y1) < 0.12:
            y2 = min(1.0, y1 + 0.12)
        return self._clamp_rect(
            {
                "x1": int(round(width * x1)),
                "y1": int(round(height * y1)),
                "x2": int(round(width * x2)),
                "y2": int(round(height * y2)),
            },
            width,
            height,
        )

    def _resolve_preferred_chat_roi(self, width, height):
        fixed = self._fixed_chat_roi_for_size(width, height)
        if fixed:
            return fixed, "fixed"
        if isinstance(self._chat_roi, dict):
            learned = self._clamp_rect(self._chat_roi, width, height)
            if learned:
                return learned, "learned"
        return None, ""

    def _dump_chat_roi_debug(self, full_img, roi_rect, source, scan):
        if not self._chat_roi_debug_enabled:
            return
        now = time.time()
        if (now - self._last_chat_roi_debug_at) < self._chat_roi_debug_interval:
            return
        if full_img is None or getattr(full_img, "size", 0) <= 0:
            return
        try:
            debug_dir = Path(self._chat_roi_debug_dir)
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
            ms = int((now - int(now)) * 1000)
            tag = f"{stamp}_{ms:03d}"

            full = full_img.copy()
            h, w = full.shape[:2]
            x1 = y1 = x2 = y2 = 0
            if isinstance(roi_rect, dict):
                clamped = self._clamp_rect(roi_rect, w, h)
                if clamped:
                    x1, y1, x2, y2 = clamped["x1"], clamped["y1"], clamped["x2"], clamped["y2"]
                    cv2.rectangle(full, (x1, y1), (x2, y2), (48, 245, 255), 2)

            chat_count = int(len((scan or {}).get("chat_messages") or []))
            line_count = int((scan or {}).get("line_count") or 0)
            page_type = str((scan or {}).get("page_type") or "")
            roi_origin = str((scan or {}).get("capture_roi_origin") or "")
            head = f"src={source} roi={roi_origin or 'none'} chat={chat_count} lines={line_count} page={page_type}"
            cv2.putText(full, head[:180], (14, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 255), 2, cv2.LINE_AA)

            full_file = debug_dir / f"{tag}_{source}_full.jpg"
            cv2.imwrite(str(full_file), full)

            if (x2 - x1) >= 16 and (y2 - y1) >= 16:
                roi_img = full_img[y1:y2, x1:x2]
                if roi_img is not None and getattr(roi_img, "size", 0) > 0:
                    roi_file = debug_dir / f"{tag}_{source}_roi.jpg"
                    cv2.imwrite(str(roi_file), roi_img)

            files = sorted(
                [p for p in debug_dir.glob("*.jpg") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
            )
            overflow = len(files) - self._chat_roi_debug_max_files
            if overflow > 0:
                for old in files[:overflow]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            self._last_chat_roi_debug_at = now
        except Exception as e:
            logger.debug(f"chat roi debug dump failed: {e}")

    def _update_chat_roi_from_scan(self, scan):
        try:
            blocks = list((scan or {}).get("blocks") or [])
            if not blocks:
                return
            best = sorted(blocks, key=lambda b: float((b or {}).get("chat_score") or 0.0), reverse=True)
            if not best:
                return
            top = best[0]
            score = float((top or {}).get("chat_score") or 0.0)
            rect = (top or {}).get("rect")
            if score < 0.62 or not isinstance(rect, dict):
                return
            source = str((scan or {}).get("source") or "").strip().lower()
            vis = (scan or {}).get("visual") or {}
            vis_w = int(vis.get("w") or 0)
            vis_h = int(vis.get("h") or 0)
            clamped = self._clamp_rect(rect, vis_w, vis_h)
            if not clamped:
                return
            mapped = clamped
            if source in {"screen_chat_roi", "screen_fixed_chat_roi"}:
                roi = (scan or {}).get("capture_roi") or {}
                if not isinstance(roi, dict):
                    return
                mapped = {
                    "x1": int(roi.get("x1") or 0) + int(clamped.get("x1") or 0),
                    "y1": int(roi.get("y1") or 0) + int(clamped.get("y1") or 0),
                    "x2": int(roi.get("x1") or 0) + int(clamped.get("x2") or 0),
                    "y2": int(roi.get("y1") or 0) + int(clamped.get("y2") or 0),
                }
                full_w = int((scan or {}).get("screen_full_width") or 0)
                full_h = int((scan or {}).get("screen_full_height") or 0)
                if full_w > 0 and full_h > 0:
                    mapped = self._clamp_rect(mapped, full_w, full_h)
                else:
                    mapped = self._clamp_rect(mapped, int(roi.get("x2") or 0), int(roi.get("y2") or 0))
                if not mapped:
                    return
            elif source != "screen_capture":
                return
            # 略微外扩，减少漏识别
            pad_x = max(6, int((mapped["x2"] - mapped["x1"]) * 0.04))
            pad_y = max(6, int((mapped["y2"] - mapped["y1"]) * 0.03))
            full_w = int((scan or {}).get("screen_full_width") or 0)
            full_h = int((scan or {}).get("screen_full_height") or 0)
            if source == "screen_capture" and (full_w <= 0 or full_h <= 0):
                full_w = max(0, vis_w)
                full_h = max(0, vis_h)
            expanded = self._clamp_rect(
                {
                    "x1": mapped["x1"] - pad_x,
                    "y1": mapped["y1"] - pad_y,
                    "x2": mapped["x2"] + pad_x,
                    "y2": mapped["y2"] + pad_y,
                },
                max(1, full_w),
                max(1, full_h),
            )
            if expanded:
                self._chat_roi = expanded
        except Exception:
            return

    def _scan_from_screen(self, use_cache=True, max_chat_messages=6, prefer_chat_roi=False):
        if (
            use_cache
            and (not prefer_chat_roi)
            and self._last_ocr_scan
            and str((self._last_ocr_scan or {}).get("source") or "") == "screen_capture"
            and (time.time() - self._last_screen_scan_at) <= self._screen_scan_cache_ttl
        ):
            cached = dict(self._last_ocr_scan)
            cached["cached"] = True
            return cached

        if not self.screen_capture.available():
            err = self.screen_capture.last_error or "screen_capture_unavailable"
            return {
                "ok": False,
                "available": False,
                "error": err,
                "text": "",
                "lines": [],
                "line_count": 0,
                "chat_messages": [],
                "page_type": "non_target",
                "source": "screen_capture",
                "capture_backend": "",
                "capture_error": err,
            }
        cap = self.screen_capture.capture(monitor_index=self.screen_monitor_index)
        self._last_capture = dict(cap or {})
        if not cap.get("ok"):
            err = str(cap.get("error") or "capture_failed")
            return {
                "ok": False,
                "available": False,
                "error": err,
                "text": "",
                "lines": [],
                "line_count": 0,
                "chat_messages": [],
                "page_type": "non_target",
                "source": "screen_capture",
                "capture_backend": cap.get("backend") or "",
                "capture_error": err,
            }
        source = "screen_capture"
        img = cap.get("image")
        full_img = img
        capture_meta = dict(cap)
        h, w = img.shape[:2]
        capture_meta["screen_full_width"] = int(w)
        capture_meta["screen_full_height"] = int(h)
        if prefer_chat_roi:
            roi, roi_origin = self._resolve_preferred_chat_roi(w, h)
        else:
            roi, roi_origin = (None, "")
        if roi:
            h, w = img.shape[:2]
            roi = self._clamp_rect(roi, w, h)
            if roi:
                x1, y1, x2, y2 = roi["x1"], roi["y1"], roi["x2"], roi["y2"]
                cropped = img[y1:y2, x1:x2]
                if cropped is not None and getattr(cropped, "size", 0) > 0:
                    img = cropped
                    source = "screen_fixed_chat_roi" if roi_origin == "fixed" else "screen_chat_roi"
                    capture_meta["left"] = int(cap.get("left") or 0) + x1
                    capture_meta["top"] = int(cap.get("top") or 0) + y1
                    capture_meta["width"] = int(x2 - x1)
                    capture_meta["height"] = int(y2 - y1)
                    capture_meta["roi"] = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
                    capture_meta["roi_origin"] = roi_origin

        scan = self.ocr_reader.scan_image(
            img,
            use_cache=False,
            max_chat_messages=max_chat_messages,
            source=source,
            capture_meta=capture_meta,
        )
        if isinstance(scan, dict):
            scan.setdefault("capture_backend", cap.get("backend") or "")
            scan.setdefault("capture_error", "")
            scan.setdefault("capture_elapsed_ms", int(cap.get("elapsed_ms") or 0))
            if capture_meta.get("roi"):
                scan.setdefault("capture_roi", dict(capture_meta.get("roi") or {}))
            if capture_meta.get("roi_origin"):
                scan.setdefault("capture_roi_origin", str(capture_meta.get("roi_origin") or ""))
            scan.setdefault("screen_full_width", int(capture_meta.get("screen_full_width") or 0))
            scan.setdefault("screen_full_height", int(capture_meta.get("screen_full_height") or 0))
            if source == "screen_capture":
                self._last_screen_scan_at = time.time()
            self._dump_chat_roi_debug(
                full_img=full_img,
                roi_rect=(capture_meta.get("roi") or self._chat_roi),
                source=source,
                scan=scan,
            )
        return scan

    def get_info_source_status(self):
        scan = dict(self._last_ocr_scan or {})
        cap = dict(self._last_capture or {})
        live_state = dict(scan.get("live_state") or {})
        return {
            "mode": self.get_info_source_mode(),
            "ocr_available": bool(getattr(self.ocr_reader, "available", lambda: False)()),
            "ocr_page_type": scan.get("page_type") or "",
            "ocr_error": scan.get("error") or "",
            "ocr_ms": int(scan.get("elapsed_ms") or 0),
            "ocr_line_count": int(scan.get("line_count") or 0),
            "ocr_text": str(scan.get("text") or "")[:280],
            "ocr_chat_count": len(scan.get("chat_messages") or []),
            "ocr_live": bool(live_state.get("is_live")),
            "ocr_live_phase": str(live_state.get("phase") or ""),
            "ocr_live_confidence": float(live_state.get("confidence") or 0.0),
            "ocr_live_has_timer": bool(live_state.get("has_timer")),
            "ocr_scene_tags": list(scan.get("scene_tags") or [])[:8],
            "ocr_block_count": int(len(scan.get("blocks") or [])),
            "capture_backend": scan.get("capture_backend") or cap.get("backend") or "",
            "capture_error": scan.get("capture_error") or cap.get("error") or "",
            "capture_ms": int(scan.get("capture_elapsed_ms") or cap.get("elapsed_ms") or 0),
        }

    def get_operation_observation(self, use_cache=True, max_candidates=16):
        """
        生成运营动作规划所需的“受限观测快照”：
        - 不暴露可直接执行的坐标 API
        - 仅返回候选 target_id（供上层计划器引用）
        """
        page_ctx = self.get_page_context()
        scan = self.get_latest_ocr_scan(use_cache=use_cache, max_chat_messages=1)
        if not isinstance(scan, dict):
            scan = {}

        action_candidates = list(scan.get("action_candidates") or [])
        targets = []
        target_map = {}
        limit = max(1, int(max_candidates or 16))
        for idx, item in enumerate(action_candidates[:limit], start=1):
            if not isinstance(item, dict):
                continue
            target_id = f"t{idx:02d}"
            payload = {
                "target_id": target_id,
                "action": str(item.get("action") or ""),
                "text": str(item.get("text") or "")[:120],
                "role": str(item.get("role") or ""),
                "score": float(item.get("score") or 0.0),
                "rect": dict(item.get("rect") or {}),
                "source": str(item.get("source") or ""),
            }
            targets.append(payload)
            target_map[target_id] = dict(payload)

        blocked_regions = []
        for b in list(scan.get("blocks") or []):
            if not isinstance(b, dict):
                continue
            role = str(b.get("role") or "")
            if role not in {"chat_panel", "metrics_panel"}:
                continue
            rect = b.get("rect")
            if isinstance(rect, dict):
                blocked_regions.append({"role": role, "rect": dict(rect)})

        return {
            "ts": time.time(),
            "page_context": {
                "page_type": str(page_ctx.get("page_type") or "non_target"),
                "is_operable": bool(page_ctx.get("is_operable")),
                "is_monitor_only": bool(page_ctx.get("is_monitor_only")),
                "source": str(page_ctx.get("source") or ""),
            },
            "ocr": {
                "available": bool(scan.get("available", True)),
                "error": str(scan.get("error") or ""),
                "provider": str(scan.get("provider") or ""),
                "scene_tags": list(scan.get("scene_tags") or [])[:10],
                "line_count": int(scan.get("line_count") or 0),
                "text_preview": str(scan.get("text") or "")[:260],
                "line_preview": [
                    str((ln or {}).get("text") or "")[:80]
                    for ln in list(scan.get("lines") or [])[:10]
                    if isinstance(ln, dict)
                ],
            },
            "targets": targets,
            "target_map": target_map,
            "blocked_regions": blocked_regions[:12],
        }

    def get_latest_ocr_scan(self, use_cache=True, max_chat_messages=None):
        max_msgs = max(1, int(max_chat_messages or getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6)))
        if self._is_screen_ocr_mode():
            scan = self._scan_from_screen(use_cache=use_cache, max_chat_messages=max_msgs)
            self._last_ocr_scan = dict(scan or {})
            return scan
        if not self.ocr_reader.available():
            return {
                "ok": False,
                "available": False,
                "error": self.ocr_reader.ocr_engine.last_error or "ocr_provider_unavailable",
                "text": "",
                "lines": [],
                "line_count": 0,
                "chat_messages": [],
                "page_type": "non_target",
                "source": "ocr",
            }
        if not self.page:
            return {
                "ok": False,
                "available": True,
                "error": "no_page",
                "text": "",
                "lines": [],
                "line_count": 0,
                "chat_messages": [],
                "page_type": "non_target",
                "source": "ocr",
            }
        scan = self.ocr_reader.scan_page(
            self.page,
            use_cache=use_cache,
            max_chat_messages=max_msgs,
        )
        self._last_ocr_scan = dict(scan or {})
        return scan

    def _is_local_mock_shop_page(self, title, url):
        title_lower = (title or "").lower()
        url_lower = (url or "").lower()
        if "mock_tiktok_shop" in url_lower:
            return True
        if ("localhost" in url_lower or "127.0.0.1" in url_lower) and (
            "/streamer/live/product/dashboard" in url_lower
            or "/workbench/live/overview" in url_lower
        ):
            return True
        if "tiktok shop streamer mock" in title_lower:
            return True
        return False

    def _classify_page(self, title, url):
        """
        页面类型识别：
        - shop_dashboard: TikTok Shop 助播可操作页（可执行置顶/秒杀）
        - shop_overview: TikTok Shop 直播大屏（监控页，非执行页）
        - tiktok_live_room: @xxx/live 直播间页
        - tiktok_live_dashboard: 通用直播控制台页
        - non_target: 非目标页
        """
        title = (title or "")
        title_lower = title.lower()
        url = (url or "").lower()

        # 本地模拟页（用于离线联调）
        if self._is_local_mock_shop_page(title, url):
            if "/workbench/live/overview" in url or "直播大屏" in title:
                return "shop_overview"
            return "shop_dashboard"

        if "shop.tiktok.com" in url:
            if "/streamer/live/product/dashboard" in url:
                return "shop_dashboard"
            if "/workbench/live/overview" in url:
                return "shop_overview"
            if "直播控制台" in title or "直播管理平台" in title:
                return "shop_dashboard"
            if "直播大屏" in title:
                return "shop_overview"
        if "business.tiktokshop.com" in url:
            if "/creator/live" in url or "/live" in url:
                return "shop_dashboard"
            if "live shopping" in title_lower and "tiktok shop" in title_lower:
                return "shop_dashboard"

        if re.search(r"tiktok\.com/@[^/]+/live", url):
            return "tiktok_live_room"
        if "tiktok.com/live/dashboard" in url or "live dashboard" in title_lower:
            return "tiktok_live_dashboard"
        return "non_target"

    def get_page_context(self):
        """返回当前页面识别结果。"""
        if self._is_screen_ocr_mode():
            scan = self._scan_from_screen(
                use_cache=True,
                max_chat_messages=max(1, int(getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6))),
            )
            self._last_ocr_scan = dict(scan or {})
            self._update_chat_roi_from_scan(scan or {})
            ocr_page_type = str(scan.get("page_type") or "").strip() if isinstance(scan, dict) else ""
            return {
                "page_type": ocr_page_type or "non_target",
                "title": "",
                "url": "",
                "is_operable": bool(scan.get("is_operable")),
                "is_monitor_only": bool(scan.get("is_monitor_only")),
                "source": "screen_ocr",
            }

        if not self.page:
            return {
                "page_type": "non_target",
                "title": "",
                "url": "",
                "is_operable": False,
                "is_monitor_only": False,
            }
        try:
            title = self.page.title or ""
            url = self.page.url or ""
        except Exception:
            title, url = "", ""

        # OCR 信息源模式：页面上下文优先由 OCR 读取，不依赖 DOM 文本结构。
        if self._is_ocr_info_enabled() and self.ocr_reader.available():
            scan = self.get_latest_ocr_scan(
                use_cache=True,
                max_chat_messages=max(1, int(getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6))),
            )
            self._last_ocr_scan = dict(scan or {})
            ocr_page_type = str(scan.get("page_type") or "").strip() if isinstance(scan, dict) else ""
            if ocr_page_type:
                return {
                    "page_type": ocr_page_type,
                    "title": title,
                    "url": url,
                    "is_operable": bool(scan.get("is_operable")),
                    "is_monitor_only": bool(scan.get("is_monitor_only")),
                    "source": "ocr",
                }
            if self._is_ocr_only_info_mode():
                return {
                    "page_type": "non_target",
                    "title": title,
                    "url": url,
                    "is_operable": False,
                    "is_monitor_only": False,
                    "source": "ocr",
                }

        page_type = self._classify_page(title, url)
        is_operable = page_type in ("shop_dashboard", "tiktok_live_dashboard")
        is_monitor_only = page_type in ("shop_overview",)
        return {
            "page_type": page_type,
            "title": title,
            "url": url,
            "is_operable": is_operable,
            "is_monitor_only": is_monitor_only,
            "source": "dom",
        }

    def _is_operable_page_for_action(self, page_type, action):
        action = (action or "").strip().lower()
        if action in ("pin_product", "unpin_product", "repin_product", "start_flash_sale"):
            return page_type == "shop_dashboard"
        return page_type in ("shop_dashboard", "tiktok_live_dashboard")

    def _action_requires_shop_dashboard(self, action):
        action = (action or "").strip().lower()
        return action in {"pin_product", "unpin_product", "repin_product", "start_flash_sale"}

    def _try_open_shop_dashboard(self):
        page = self.page
        if not page:
            return False
        urls = list(getattr(settings, "ACTION_PAGE_SHOP_DASHBOARD_URLS", []) or [])
        if not urls:
            return False

        for raw_url in urls[:4]:
            url = str(raw_url or "").strip()
            if not url:
                continue
            try:
                page.get(url, retry=0, timeout=4.0)
                time.sleep(0.30)
                title = getattr(page, "title", "") or ""
                cur_url = getattr(page, "url", "") or ""
                page_type = self._classify_page(title, cur_url)
                if page_type == "shop_dashboard":
                    logger.info(f"已切换到 Shop 控制台执行页: {cur_url}")
                    return True
            except Exception as e:
                logger.debug(f"跳转 Shop 控制台失败: url={url}, err={e}")
                continue
        return False

    def _find_best_tab_for_action(self, action):
        co = ChromiumOptions().set_local_port(self.port)
        browser = ChromiumPage(co)
        tabs = browser.get_tabs() or []

        candidates = []
        for tab in tabs:
            title = getattr(tab, "title", "") or ""
            url = getattr(tab, "url", "") or ""
            page_type = self._classify_page(title, url)
            score = self._score_tab(tab)
            if self._is_operable_page_for_action(page_type, action):
                score += 40
            elif page_type == "shop_overview":
                score += 8
            candidates.append((score, page_type, tab))

        if not candidates:
            return None, None, None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_type, best_tab = candidates[0]
        return best_tab, best_type, best_score

    def _activate_tab_best_effort(self, tab):
        """尽力激活目标标签页，兼容不同 DrissionPage 版本 API。"""
        if tab is None:
            return False
        try:
            setter = getattr(tab, "set", None)
            activate = getattr(setter, "activate", None) if setter is not None else None
            if callable(activate):
                activate()
                return True
        except Exception:
            pass
        for method in ("activate", "set_active", "focus"):
            try:
                fn = getattr(tab, method, None)
                if callable(fn):
                    fn()
                    return True
            except Exception:
                continue
        try:
            if hasattr(tab, "run_js"):
                tab.run_js("window.focus && window.focus();")
                return True
        except Exception:
            pass
        return False

    def ensure_action_page(self, action):
        """
        保障执行页：
        1) 当前页可执行则直接通过
        2) 否则在浏览器标签页中切到最优可执行页
        """
        screen_ocr_mode = self._is_screen_ocr_mode()
        if screen_ocr_mode:
            ctx = self.get_page_context()
            if self._is_operable_page_for_action(ctx.get("page_type"), action):
                return True
            # screen_ocr 模式下，仍尝试建立浏览器上下文并切换到最佳执行标签页。
            self.ensure_browser_page_connection(force=True)
        elif not self.ensure_connection():
            return False

        cur = self.get_page_context()
        if self._is_operable_page_for_action(cur.get("page_type"), action):
            return True

        if self._action_requires_shop_dashboard(action) and str(cur.get("page_type") or "") == "tiktok_live_dashboard":
            if self._try_open_shop_dashboard():
                cur2 = self.get_page_context()
                if self._is_operable_page_for_action(cur2.get("page_type"), action):
                    return True

        try:
            best_tab, best_type, best_score = self._find_best_tab_for_action(action)
            if best_tab is None:
                logger.warning("执行页切换失败：未找到可用标签页")
                return False
            self.page = best_tab
            self.last_alive_probe_ok = self._page_alive()
            self.last_alive_probe_at = time.time()
            logger.info(
                f"执行页切换: action={action}, page_type={best_type}, "
                f"score={best_score}, title={getattr(self.page, 'title', '')}"
            )
            if screen_ocr_mode:
                activated = self._activate_tab_best_effort(self.page)
                if activated:
                    time.sleep(0.22)
            if self._action_requires_shop_dashboard(action) and best_type == "tiktok_live_dashboard":
                self._try_open_shop_dashboard()
        except Exception as e:
            logger.warning(f"执行页切换失败: {e}")
            return False

        ctx = self.get_page_context()
        if self._is_operable_page_for_action(ctx.get("page_type"), action):
            return True
        if screen_ocr_mode:
            logger.warning(
                f"screen_ocr 页面不可执行动作: action={action}, page_type={ctx.get('page_type')}"
            )
        logger.warning(
            f"当前页面不可执行动作: action={action}, page_type={ctx.get('page_type')}, url={ctx.get('url')}"
        )
        return False

    def _is_live_like(self, title, url):
        title = (title or "")
        title_lower = title.lower()
        url = (url or "").lower()

        # 项目内置 mock 页面也应被视为“可用直播页”，
        # 否则 ensure_connection 会误判非直播并触发无效重连。
        if self._is_local_mock_shop_page(title, url):
            return True

        if "tiktok.com" not in url and "tiktok" not in title_lower:
            return False

        live_markers = [
            "tiktok.com/live",
            "/live/",
            "正在直播",
            "live dashboard",
            "直播",
            "shop.tiktok.com/streamer/live/product/dashboard",
            "shop.tiktok.com/workbench/live/overview",
            "business.tiktokshop.com/us/creator/live",
            "live shopping",
            "直播控制台",
            "直播大屏",
        ]
        return any(marker in url or marker in title_lower for marker in live_markers)

    def _is_target_live_room(self, title, url):
        """
        严格目标：主播直播间页面（优先 /@xxx/live）。
        避免误选“推荐页/搜索页/新标签页/控制台页”导致慢与误触发权限。
        """
        title = (title or "")
        title_lower = title.lower()
        url = (url or "").lower()
        if "tiktok.com" not in url:
            if self._is_local_mock_shop_page(title, url):
                return True
            return False
        # TikTok Shop 直播控制台视为目标页（便于稳定复用，不反复重连）
        if "shop.tiktok.com/streamer/live/product/dashboard" in url:
            return True
        if "shop.tiktok.com/workbench/live/overview" in url:
            return True
        if "business.tiktokshop.com/us/creator/live" in url:
            return True
        if self._is_local_mock_shop_page(title, url):
            return True
        # TikTok 常见直播入口页，虽然不一定是 /@xxx/live，但可作为稳定候选，避免长时间找不到目标页。
        if re.search(r"tiktok\.com/live(?:/|$)", url):
            return True
        if re.search(r"tiktok\.com/@[^/]+/live", url):
            return True
        # 兼容中英文标题
        if (
            ("正在直播" in title and "tiktok" in title_lower)
            or ("直播控制台" in title)
            or ("live" in title_lower and "tiktok" in title_lower)
        ):
            return True
        return False

    def _score_tab(self, tab):
        title = (getattr(tab, "title", "") or "")
        title_lower = title.lower()
        url = (getattr(tab, "url", "") or "").lower()
        page_type = self._classify_page(title, url)

        score = 0
        if page_type == "shop_dashboard":
            score += 45
        elif page_type == "tiktok_live_room":
            score += 30
        elif page_type == "tiktok_live_dashboard":
            score += 20
        elif page_type == "shop_overview":
            score += 8

        if self._is_target_live_room(title, url):
            score += 30
        if "tiktok.com/live" in url:
            score += 8
        elif "tiktok.com" in url:
            score += 3

        if "/live/" in url:
            score += 3
        elif url.endswith("/live") or "/live?" in url:
            score += 3
        if "dashboard" in url or "dashboard" in title_lower:
            score += 2
        if "直播" in title or "live" in title_lower:
            score += 2
        if "tiktok" in title_lower:
            score += 1
        # 强力降权：推荐流/搜索页/新标签页/本地页面
        if any(k in title_lower for k in ["推荐", "for you", "search", "google", "new tab", "新标签页"]):
            score -= 25
        if any(k in url for k in ["google.", "chrome://", "localhost:8501", "127.0.0.1:8501"]):
            score -= 30
        if self._is_local_mock_shop_page(title, url):
            score += 25
            # Mock 多标签场景：执行动作时优先 dashboard_live，强力降权 dashboard_idle。
            if "view=dashboard_live" in url or "/workbench/live/overview" in url:
                score += 18
            if "view=dashboard_idle" in url:
                score -= 22

        for key in settings.TIKTOK_TAB_REQUIRED_KEYWORDS:
            if key and (key in title_lower or key in url):
                score += 1
        for key in settings.TIKTOK_TAB_EXCLUDE_KEYWORDS:
            if key and (key in title_lower or key in url):
                score -= 8

        if not self._is_live_like(title, url):
            score -= 2
        return score

    def _is_audio_test_tab(self, title, url):
        title_lower = (title or "").lower()
        url_lower = (url or "").lower()
        media_hosts = [
            "bilibili.com",
            "live.bilibili.com",
            "youtube.com",
            "youtu.be",
            "twitch.tv",
            "douyin.com",
        ]
        if any(host in url_lower for host in media_hosts):
            return True
        if any(token in title_lower for token in ["哔哩哔哩", "bilibili", "youtube", "twitch", "douyin"]):
            return True
        return False

    def _score_tab_for_browser_page(self, tab, prefer_media_tab=False):
        score = self._score_tab(tab)
        if not prefer_media_tab:
            return score
        title = (getattr(tab, "title", "") or "")
        url = (getattr(tab, "url", "") or "")
        url_lower = url.lower()
        if self._is_audio_test_tab(title, url):
            score += 32
        if "bilibili.com" in url_lower:
            score += 8
        if any(key in url_lower for key in ["/video/", "/bangumi/", "/live", "live.bilibili.com"]):
            score += 4
        return score

    def _is_current_live_tab(self):
        if self._is_screen_ocr_mode():
            return bool(self.screen_capture.available())
        if not self.page:
            return False
        try:
            return self._is_live_like(self.page.title, self.page.url)
        except Exception:
            return False

    def _page_alive(self):
        if self._is_screen_ocr_mode():
            return bool(self.screen_capture.available())
        if not self.page:
            return False
        try:
            # 仅 title 可访问不代表 DOM 会话可用，额外做一次 JS 探活
            _ = self.page.url
            _ = self.page.run_js("return 1")
            return True
        except Exception:
            return False

    def _browser_page_alive(self):
        if not self.page:
            return False
        try:
            _ = self.page.url
            _ = self.page.run_js("return 1")
            return True
        except Exception:
            return False

    def _is_disconnect_error(self, err):
        msg = str(err or "").lower()
        return (
            "与页面的连接已断开" in msg
            or "connection" in msg and "disconnect" in msg
            or "cannot find context" in msg
            or "target closed" in msg
        )

    def ensure_connection(self, force=False):
        """确保 page 连接可用。"""
        if self._is_screen_ocr_mode():
            if not self.screen_capture.available():
                self.last_alive_probe_ok = False
                return False
            now = time.time()
            if not force and (now - self.last_alive_probe_at < self.alive_probe_interval):
                return bool(self.last_alive_probe_ok)
            cap = self.screen_capture.capture(monitor_index=self.screen_monitor_index)
            self._last_capture = dict(cap or {})
            ok = bool(cap.get("ok"))
            self.last_alive_probe_ok = ok
            self.last_alive_probe_at = now
            return ok

        now = time.time()
        live_tab = self._is_current_live_tab()
        alive = False
        if not force and self.page:
            if now - self.last_alive_probe_at < self.alive_probe_interval:
                alive = self.last_alive_probe_ok
            else:
                alive = self._page_alive()
                self.last_alive_probe_ok = alive
                self.last_alive_probe_at = now

        if alive and not force and live_tab:
            return True
        if alive and not force and not live_tab:
            now = time.time()
            if now - self.last_wrong_tab_log_at > 8:
                logger.warning(f"当前标签页疑似非直播页: {self.page.title} | {self.page.url}")
                self.last_wrong_tab_log_at = now

        if not force and now - self.last_reconnect_at < self.reconnect_cooldown:
            return False

        self.last_reconnect_at = now
        try:
            self.connect_browser()
            logger.info("VisionAgent 已自动重连浏览器页面")
            alive = self._page_alive()
            self.last_alive_probe_ok = alive
            self.last_alive_probe_at = time.time()
            return alive
        except Exception as e:
            logger.warning(f"VisionAgent 自动重连失败: {e}")
            self.last_alive_probe_ok = False
            return False
    
    def connect_browser(self):
        """连接到浏览器"""
        if self._is_screen_ocr_mode():
            if not self.screen_capture.available():
                raise RuntimeError(self.screen_capture.last_error or "screen_capture_unavailable")
            logger.info("screen_ocr 模式：跳过浏览器连接，使用屏幕采集作为信息源")
            return
        try:
            if self.page and self._page_alive() and self._is_target_live_room(self.page.title, self.page.url):
                logger.info(f"复用当前目标标签页: {self.page.title}")
                return
            co = ChromiumOptions().set_local_port(self.port)
            self.page = ChromiumPage(co)
            logger.info(f"成功连接到浏览器端口 {self.port}")
            
            tabs = self.page.get_tabs()
            strict_tabs = [t for t in tabs if self._is_target_live_room(getattr(t, "title", ""), getattr(t, "url", ""))]
            candidates = []
            if strict_tabs:
                for tab in strict_tabs:
                    candidates.append((self._score_tab(tab), tab))
            else:
                for tab in tabs:
                    candidates.append((self._score_tab(tab), tab))

            if not candidates:
                raise RuntimeError("浏览器无可用标签页")

            candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_tab = candidates[0]
            if settings.TIKTOK_FORCE_LIVE_TAB and best_score < settings.TIKTOK_TAB_MIN_SCORE:
                # 显式兜底：若存在内置 mock 页，优先接入，避免评分阈值误拦截。
                mock_tab = None
                soft_tiktok_tab = None
                for _, tab in candidates:
                    url = (getattr(tab, "url", "") or "").lower()
                    title = (getattr(tab, "title", "") or "").lower()
                    if "mock_tiktok_shop" in url or "mock_tiktok_shop.html" in url or "tiktok shop streamer mock" in title:
                        mock_tab = tab
                        break
                    if (soft_tiktok_tab is None) and (
                        ("tiktok.com" in url)
                        or ("tiktokshop.com" in url)
                        or ("tiktok" in title)
                    ):
                        soft_tiktok_tab = tab
                if mock_tab is not None:
                    self.page = mock_tab
                    self.last_alive_probe_ok = True
                    self.last_alive_probe_at = time.time()
                    logger.info("评分阈值未命中，已回退连接到内置 Mock 标签页")
                    return
                if soft_tiktok_tab is not None:
                    self.page = soft_tiktok_tab
                    self.last_alive_probe_ok = True
                    self.last_alive_probe_at = time.time()
                    logger.info("评分阈值未命中，已回退连接到 TikTok 相关标签页")
                    return
                logger.warning(f"未找到可用直播标签页，最高匹配分={best_score}，请先打开 TikTok 直播页")
                raise RuntimeError("未找到 TikTok 直播标签页")
            self.page = best_tab
            self.last_alive_probe_ok = True
            self.last_alive_probe_at = time.time()
            logger.info(f"已切换到目标标签页: {self.page.title} (score={best_score})")
                
        except Exception as e:
            logger.error(f"连接浏览器失败: {e}")
            raise

    def ensure_browser_page_connection(self, force=False, prefer_media_tab=False):
        """
        仅确保可执行 JS 的浏览器 page 上下文可用。
        与 info_source_mode 无关：即使在 screen_ocr 模式也会尝试连接浏览器页。
        """
        now = time.time()
        if (not force) and self._browser_page_alive():
            return True
        if (not force) and (now - self.last_reconnect_at < self.reconnect_cooldown):
            return False

        self.last_reconnect_at = now
        try:
            co = ChromiumOptions().set_local_port(self.port)
            browser = ChromiumPage(co)
            tabs = browser.get_tabs() or []
            if not tabs:
                self.page = browser
                alive = self._browser_page_alive()
                self.last_alive_probe_ok = alive
                self.last_alive_probe_at = time.time()
                return alive

            media_tabs = []
            if prefer_media_tab:
                media_tabs = [
                    t for t in tabs
                    if self._is_audio_test_tab(getattr(t, "title", ""), getattr(t, "url", ""))
                ]
            strict_tabs = [t for t in tabs if self._is_target_live_room(getattr(t, "title", ""), getattr(t, "url", ""))]
            pick_pool = media_tabs or strict_tabs or tabs
            candidates = [
                (self._score_tab_for_browser_page(tab, prefer_media_tab=prefer_media_tab), tab)
                for tab in pick_pool
            ]
            candidates.sort(key=lambda x: x[0], reverse=True)
            self.page = candidates[0][1]
            alive = self._browser_page_alive()
            self.last_alive_probe_ok = alive
            self.last_alive_probe_at = time.time()
            return alive
        except Exception as e:
            logger.warning(f"ensure_browser_page_connection 失败: {e}")
            self.last_alive_probe_ok = False
            return False

    def _allow_ocr_danmu_page_type(self, page_type):
        """OCR 弹幕仅在明确直播页放行，避免把其它应用文本当弹幕。"""
        return str(page_type or "").strip().lower() in {"tiktok_live_room"}

    def get_new_danmu(self):
        """
        获取新的弹幕消息
        基于 data-e2e="chat-message" 选择器 (2025-02 验证有效)
        """
        if self._is_screen_ocr_mode():
            if not self.ensure_connection():
                return []
            try:
                scan = self._scan_from_screen(
                    use_cache=False,
                    max_chat_messages=max(1, int(getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6))),
                    prefer_chat_roi=True,
                )
                # ROI 扫描未命中时退回一次全屏扫描，保证召回
                if not list((scan or {}).get("chat_messages") or []):
                    scan = self._scan_from_screen(
                        use_cache=False,
                        max_chat_messages=max(1, int(getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6))),
                        prefer_chat_roi=False,
                    )
                self._last_ocr_scan = dict(scan or {})
                self._update_chat_roi_from_scan(scan or {})
                page_type = str((scan or {}).get("page_type") or "").strip().lower()
                if not self._allow_ocr_danmu_page_type(page_type):
                    self.no_chat_rounds += 1
                    warn_interval = max(1, settings.NO_CHAT_WARN_INTERVAL_ROUNDS)
                    if self.no_chat_rounds % warn_interval == 0:
                        logger.warning(
                            "VisionAgent(ScreenOCR): 跳过非直播页弹幕抽取 "
                            f"page_type={scan.get('page_type')} lines={scan.get('line_count')}"
                        )
                    return []
                ocr_msgs = list(scan.get("chat_messages") or [])
                new_messages = []
                if not ocr_msgs:
                    self.no_chat_rounds += 1
                    warn_interval = max(1, settings.NO_CHAT_WARN_INTERVAL_ROUNDS)
                    if self.no_chat_rounds % warn_interval == 0:
                        logger.warning(
                            f"VisionAgent(ScreenOCR): chat=0 page_type={scan.get('page_type')} "
                            f"lines={scan.get('line_count')} err={scan.get('error')}"
                        )
                    return []
                self.no_chat_rounds = 0
                for item in ocr_msgs[-15:]:
                    user = str(item.get("user") or "观众").strip() or "观众"
                    content = str(item.get("text") or "").strip()
                    if not content:
                        continue
                    msg_id = f"{user}_{content}"
                    if msg_id in self.last_message_ids:
                        continue
                    new_messages.append({"id": msg_id, "user": user, "text": content})
                    self.last_message_ids.add(msg_id)
                    if len(self.last_message_ids) > 2000:
                        self.last_message_ids.clear()
                return new_messages
            except Exception as e:
                logger.warning(f"VisionAgent ScreenOCR弹幕抽取失败: {e}")
                return []

        if not self.ensure_connection():
            return []

        # 严格 OCR 信息源模式：弹幕抽取不读取 DOM 文本，仅依赖 OCR 结果。
        if self._is_ocr_only_info_mode() and self.ocr_reader.available():
            try:
                scan = self.ocr_reader.scan_page(
                    self.page,
                    use_cache=False,
                    max_chat_messages=max(1, int(getattr(settings, "OCR_CHAT_MAX_MESSAGES", 6))),
                )
                self._last_ocr_scan = dict(scan or {})
                page_type = str((scan or {}).get("page_type") or "").strip().lower()
                if not self._allow_ocr_danmu_page_type(page_type):
                    self.no_chat_rounds += 1
                    warn_interval = max(1, settings.NO_CHAT_WARN_INTERVAL_ROUNDS)
                    if self.no_chat_rounds % warn_interval == 0:
                        logger.warning(
                            "VisionAgent(OCR): 跳过非直播页弹幕抽取 "
                            f"page_type={scan.get('page_type')} lines={scan.get('line_count')}"
                        )
                    return []
                ocr_msgs = list(scan.get("chat_messages") or [])
                new_messages = []
                if not ocr_msgs:
                    self.no_chat_rounds += 1
                    warn_interval = max(1, settings.NO_CHAT_WARN_INTERVAL_ROUNDS)
                    if self.no_chat_rounds % warn_interval == 0:
                        logger.warning(
                            f"VisionAgent(OCR): chat=0 page_type={scan.get('page_type')} "
                            f"lines={scan.get('line_count')} err={scan.get('error')}"
                        )
                    return []

                self.no_chat_rounds = 0
                for item in ocr_msgs[-15:]:
                    user = str(item.get("user") or "观众").strip() or "观众"
                    content = str(item.get("text") or "").strip()
                    if not content:
                        continue
                    msg_id = f"{user}_{content}"
                    if msg_id in self.last_message_ids:
                        continue
                    new_messages.append({"id": msg_id, "user": user, "text": content})
                    self.last_message_ids.add(msg_id)
                    if len(self.last_message_ids) > 2000:
                        self.last_message_ids.clear()
                return new_messages
            except Exception as e:
                logger.warning(f"VisionAgent OCR弹幕抽取失败，降级为空: {e}")
                return []

        new_messages = []
        try:
            chat_items = self.page.eles('css:[data-e2e="chat-message"]')
            if len(chat_items) == 0:
                self.no_chat_rounds += 1
                warn_interval = max(1, settings.NO_CHAT_WARN_INTERVAL_ROUNDS)
                force_rounds = max(warn_interval + 1, settings.NO_CHAT_FORCE_RECONNECT_ROUNDS)
                wrong_tab_probe_rounds = max(3, warn_interval // 3)

                if (not self._is_current_live_tab()) and self.no_chat_rounds >= wrong_tab_probe_rounds:
                    if self.no_chat_rounds % wrong_tab_probe_rounds == 0:
                        logger.warning(f"VisionAgent: 当前标签页疑似非直播且无弹幕，触发重连。title={self.page.title}")
                        self.ensure_connection(force=True)
                elif self.no_chat_rounds >= force_rounds:
                    if self.no_chat_rounds % warn_interval == 0:
                        logger.warning(f"VisionAgent: 连续无弹幕达到阈值，触发重连。title={self.page.title}")
                        self.ensure_connection(force=True)
                elif self.no_chat_rounds % warn_interval == 0:
                    logger.warning(f"VisionAgent: found 0 chat items. Page title: {self.page.title}")
            # else:
            #     logger.info(f"VisionAgent: found {len(chat_items)} chat items")
            else:
                self.no_chat_rounds = 0
            
            # 只处理最后 15 条，避免处理大量历史消息
            for item in chat_items[-15:]:
                try:
                    # 提取用户名
                    user_ele = item.ele('css:[data-e2e="message-owner-name"]')
                    user = user_ele.text if user_ele else "未知用户"
                    
                    # 提取内容
                    # 由于内容没有特定的 data-e2e，我们获取整体文本并移除用户名
                    full_text = item.text
                    
                    # 简单的文本清洗
                    if user in full_text:
                        content = full_text.replace(user, "", 1).strip()
                    else:
                        content = full_text
                    
                    # 移除多余换行 (徽章等可能导致换行)
                    content = content.replace("\n", " ").strip()
                    
                    # 生成唯一 ID (基于内容和用户)
                    msg_id = f"{user}_{content}"
                    
                    if msg_id not in self.last_message_ids:
                        msg = {
                            'id': msg_id,
                            'user': user,
                            'text': content
                        }
                        new_messages.append(msg)
                        self.last_message_ids.add(msg_id)
                        
                        # 简单的集合清理
                        if len(self.last_message_ids) > 2000:
                            self.last_message_ids.clear()
                            
                except Exception as e:
                    # 忽略单条解析错误
                    continue

        except Exception as e:
            if self._is_disconnect_error(e):
                now = time.time()
                if now - self.last_disconnect_log_at > 5:
                    logger.warning(f"页面连接已断开，准备重连: {e}")
                    self.last_disconnect_log_at = now
                # 关键：置空失效句柄，避免假存活导致无法重连
                self.page = None
                self.last_alive_probe_ok = False
                self.ensure_connection(force=True)
            else:
                logger.error(f"获取弹幕出错: {e}")
        
        return new_messages

    def listen(self, callback):
        """
        持续监听弹幕
        callback: 处理每条新弹幕的函数
        """
        logger.info("开始监听弹幕...")
        while True:
            messages = self.get_new_danmu()
            for msg in messages:
                callback(msg)
            
            time.sleep(1)  # 轮询间隔

if __name__ == "__main__":
    # 测试代码
    agent = VisionAgent()
    # agent.connect_browser()
    # agent.listen(lambda x: print(x))
