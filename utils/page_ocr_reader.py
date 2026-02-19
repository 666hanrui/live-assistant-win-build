import re
import time
from typing import Dict, List

import cv2
import numpy as np

from utils.ocr_engine import LocalOcrEngine


class PageOCRReader:
    """页面 OCR 读取器：文本 + 图片特征联合识别。"""

    def __init__(self):
        self.ocr_engine = LocalOcrEngine()
        self._last_scan = None
        self._last_scan_at = 0.0

    def available(self) -> bool:
        return bool(self.ocr_engine.available())

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s or "").lower())

    @staticmethod
    def _infer_flash_action(norm: str) -> str:
        """
        仅识别“秒杀上架/结束秒杀”两类强语义，避免把普通“活动/promo”误判为秒杀按钮。
        """
        if not norm:
            return ""

        has_flash = any(k in norm for k in ["秒杀", "flashsale", "flashdeal", "flashpromo", "flashpromotion"])
        has_start = any(k in norm for k in ["上架", "开始", "开启", "launch", "start", "open", "enable"])
        has_stop = any(k in norm for k in ["结束", "停止", "下架", "关闭", "撤下", "end", "stop", "disable"])

        explicit_start = any(
            k in norm
            for k in [
                "秒杀活动上架",
                "秒杀上架",
                "开启秒杀",
                "开始秒杀",
                "launchflashsale",
                "startflashsale",
                "openflashsale",
            ]
        )
        explicit_stop = any(
            k in norm
            for k in [
                "结束秒杀活动",
                "结束秒杀",
                "停止秒杀",
                "下架秒杀",
                "stopflashsale",
                "endflashsale",
                "closeflashsale",
            ]
        )

        if explicit_stop or (has_flash and has_stop and (not has_start)):
            return "stop_flash_sale"
        if explicit_start or (has_flash and has_start and (not has_stop)):
            return "start_flash_sale"
        return ""

    def _extract_live_state(self, text_norm: str, lines: List[Dict]) -> Dict:
        line_text = " ".join(str((ln or {}).get("text") or "") for ln in lines)
        norm = f"{text_norm} {self._norm(line_text)}"
        not_live_keys = ["立即开始直播", "开始直播", "未开播", "notlive", "golive"]
        live_keys = ["直播已开始", "正在直播", "live", "直播控制台", "livecontrol", "时长"]
        is_live = any(self._norm(k) in norm for k in live_keys) and not any(self._norm(k) in norm for k in not_live_keys)
        return {"is_live": bool(is_live)}

    def _analyze_visual_features(self, img_bgr: np.ndarray) -> Dict:
        if img_bgr is None or not isinstance(img_bgr, np.ndarray) or img_bgr.size == 0:
            return {}
        h, w = img_bgr.shape[:2]
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2]

        dark_ratio = float(np.mean(v < 60))
        bright_ratio = float(np.mean(v > 180))

        # 近似青绿色按钮（TikTok Shop 常见操作色）
        cyan_mask = cv2.inRange(hsv, (70, 50, 70), (105, 255, 255))
        cyan_ratio = float(np.mean(cyan_mask > 0))

        # 近似红色直播状态色
        red_mask1 = cv2.inRange(hsv, (0, 60, 70), (12, 255, 255))
        red_mask2 = cv2.inRange(hsv, (165, 60, 70), (179, 255, 255))
        red_mask = cv2.bitwise_or(red_mask1, red_mask2)
        red_ratio = float(np.mean(red_mask > 0))

        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 180)
        edge_ratio = float(np.mean(edges > 0))

        return {
            "w": int(w),
            "h": int(h),
            "dark_ratio": dark_ratio,
            "bright_ratio": bright_ratio,
            "cyan_ratio": cyan_ratio,
            "red_ratio": red_ratio,
            "edge_ratio": edge_ratio,
        }

    def _classify_page(self, text_norm: str, visual: Dict) -> str:
        has_shop = any(k in text_norm for k in ["tiktokshop", "直播管理平台", "streamer", "dashboard"])
        has_dashboard = any(k in text_norm for k in ["直播控制台", "商品", "chat", "观众", "活动", "product"])
        has_overview = any(k in text_norm for k in ["直播大屏", "overview", "流量来源", "gmv", "用户画像"])
        has_live_room = any(k in text_norm for k in ["正在直播", "live", "关注", "观众", "聊天"])

        if has_shop and has_dashboard:
            return "shop_dashboard"
        if has_shop and has_overview:
            return "shop_overview"
        if has_live_room and not has_shop:
            return "tiktok_live_room"
        if has_dashboard:
            return "tiktok_live_dashboard"

        # 文本较弱时，用视觉特征兜底：深色大面积 + 青色按钮 + 中高边缘复杂度倾向控制台/大屏
        if visual:
            dark = float(visual.get("dark_ratio") or 0.0)
            cyan = float(visual.get("cyan_ratio") or 0.0)
            edge = float(visual.get("edge_ratio") or 0.0)
            if dark > 0.22 and (cyan > 0.0025 or edge > 0.045):
                return "shop_dashboard"
        return "non_target"

    def _extract_action_candidates(self, lines: List[Dict]) -> List[Dict]:
        out = []
        for ln in lines:
            text = str((ln or {}).get("text") or "").strip()
            if not text:
                continue
            norm = self._norm(text)
            action = None

            if any(k in norm for k in ["取消置顶", "unpin"]):
                action = "unpin_product"
            elif any(k in norm for k in ["置顶", "pintotop", "pin", "top"]):
                action = "pin_product"
            else:
                action = self._infer_flash_action(norm)
            if action:
                out.append(
                    {
                        "action": action,
                        "text": text,
                        "rect": (ln or {}).get("rect"),
                        "score": (ln or {}).get("score"),
                    }
                )
        return out

    @staticmethod
    def _rect_intersection_area(a: Dict, b: Dict) -> float:
        try:
            x1 = max(float(a.get("x1", 0)), float(b.get("x1", 0)))
            y1 = max(float(a.get("y1", 0)), float(b.get("y1", 0)))
            x2 = min(float(a.get("x2", 0)), float(b.get("x2", 0)))
            y2 = min(float(a.get("y2", 0)), float(b.get("y2", 0)))
            w = max(0.0, x2 - x1)
            h = max(0.0, y2 - y1)
            return w * h
        except Exception:
            return 0.0

    def _detect_blocks(self, img_bgr: np.ndarray, lines: List[Dict]) -> List[Dict]:
        """
        轻量区块识别：通过轮廓提取主要视觉容器，再挂接 OCR 文本行。
        """
        if img_bgr is None or img_bgr.size == 0:
            return []
        h, w = img_bgr.shape[:2]
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 60, 150)
        kernel = np.ones((5, 5), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
        edges = cv2.erode(edges, kernel, iterations=1)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blocks = []
        min_area = max(2400.0, float(w * h) * 0.0025)
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            area = float(bw * bh)
            if area < min_area:
                continue
            if bw < 80 or bh < 48:
                continue
            rect = {"x1": int(x), "y1": int(y), "x2": int(x + bw), "y2": int(y + bh)}
            blocks.append(
                {
                    "rect": rect,
                    "area": area,
                    "cx": float(x + bw / 2.0),
                    "cy": float(y + bh / 2.0),
                    "w": int(bw),
                    "h": int(bh),
                }
            )

        # 限制数量，优先保留大区块，降低后续计算开销
        blocks.sort(key=lambda b: b["area"], reverse=True)
        blocks = blocks[:24]

        # 将 OCR 行分配到区块
        for b in blocks:
            b_lines = []
            b_rect = b["rect"]
            for ln in lines or []:
                l_rect = (ln or {}).get("rect")
                if not isinstance(l_rect, dict):
                    continue
                inter = self._rect_intersection_area(b_rect, l_rect)
                if inter <= 0:
                    continue
                b_lines.append(ln)
            b["line_count"] = len(b_lines)
            b["text"] = " ".join(str((ln or {}).get("text") or "") for ln in b_lines).strip()

        # 标记可能聊天区（通常靠右/偏下，文本多）
        for b in blocks:
            right_ratio = b["cx"] / max(1.0, float(w))
            lower_ratio = b["cy"] / max(1.0, float(h))
            text_score = min(1.0, float(b.get("line_count", 0)) / 20.0)
            chat_score = (0.45 if right_ratio > 0.58 else 0.0) + (0.25 if lower_ratio > 0.35 else 0.0) + text_score
            b["chat_score"] = round(chat_score, 4)

        return blocks

    def _assign_block_roles(self, blocks: List[Dict], visual: Dict) -> List[Dict]:
        if not blocks:
            return []
        w = int((visual or {}).get("w") or 0)
        h = int((visual or {}).get("h") or 0)
        out = []
        for b in blocks:
            text = str((b or {}).get("text") or "").strip()
            norm = self._norm(text)
            cx = float((b or {}).get("cx") or 0.0)
            cy = float((b or {}).get("cy") or 0.0)
            bw = int((b or {}).get("w") or 0)
            bh = int((b or {}).get("h") or 0)
            role = "unknown"
            confidence = 0.35

            if float((b or {}).get("chat_score") or 0.0) >= 0.7:
                role = "chat_panel"
                confidence = 0.82
            elif any(k in norm for k in ["商品", "库存", "置顶", "取消置顶", "product", "stock", "pin", "unpin"]):
                role = "product_panel"
                confidence = 0.76
            elif any(k in norm for k in ["秒杀", "活动", "promotion", "flashsale", "flashdeal", "deal"]):
                role = "action_panel"
                confidence = 0.74
            elif any(k in norm for k in ["gmv", "点击率", "实时在线", "观众", "时长", "成交", "orders", "viewer"]):
                role = "metrics_panel"
                confidence = 0.72
            elif h > 0 and cy <= max(96.0, h * 0.12) and bw >= max(320, int(w * 0.4)):
                role = "top_bar"
                confidence = 0.66
            elif bw > 0 and bh > 0 and (bw * bh) >= max(180000, int(w * h * 0.12)) and cx <= max(360.0, w * 0.6):
                role = "main_content"
                confidence = 0.62

            item = dict(b)
            item["role"] = role
            item["role_confidence"] = round(float(confidence), 3)
            out.append(item)
        return out

    def _extract_action_candidates_from_blocks(self, blocks: List[Dict]) -> List[Dict]:
        out = []
        for b in blocks or []:
            role = str((b or {}).get("role") or "")
            if role == "chat_panel":
                continue
            text = str((b or {}).get("text") or "").strip()
            if not text:
                continue
            norm = self._norm(text)
            action = None

            if any(k in norm for k in ["取消置顶", "unpin"]):
                action = "unpin_product"
            elif any(k in norm for k in ["置顶", "pintotop", "pin", "top"]):
                action = "pin_product"
            else:
                action = self._infer_flash_action(norm)
            if not action:
                continue
            base_score = float((b or {}).get("role_confidence") or 0.5)
            if role in {"action_panel", "product_panel"}:
                base_score += 0.25
            elif role == "main_content":
                base_score += 0.12
            out.append(
                {
                    "action": action,
                    "text": text[:120],
                    "rect": (b or {}).get("rect"),
                    "score": base_score,
                    "source": "block",
                    "role": role,
                }
            )
        return out

    def _derive_scene_tags(self, text_norm: str, visual: Dict, blocks: List[Dict]) -> List[str]:
        tags = []
        if any(k in text_norm for k in ["tiktokshop", "直播管理平台", "streamer", "dashboard"]):
            tags.append("shop_console")
        if any(k in text_norm for k in ["直播控制台", "商品", "product", "库存"]):
            tags.append("product_ops")
        if any(k in text_norm for k in ["秒杀", "flashsale", "promotion", "活动"]):
            tags.append("promo_ops")
        if any(k in text_norm for k in ["聊天", "chat", "评论", "comment"]):
            tags.append("chat_visible")
        if float((visual or {}).get("dark_ratio") or 0.0) > 0.22:
            tags.append("dark_theme")
        if float((visual or {}).get("cyan_ratio") or 0.0) > 0.0025:
            tags.append("cta_accent_present")
        if any(str((b or {}).get("role") or "") == "chat_panel" for b in blocks or []):
            tags.append("chat_panel_detected")
        if any(str((b or {}).get("role") or "") == "product_panel" for b in blocks or []):
            tags.append("product_panel_detected")
        # 去重保持顺序
        seen = set()
        dedup = []
        for t in tags:
            if t in seen:
                continue
            seen.add(t)
            dedup.append(t)
        return dedup

    def _extract_chat_messages(self, lines: List[Dict], max_messages: int = 6, blocks: List[Dict] = None) -> List[Dict]:
        candidates = []

        def _rect_y(ln):
            rect = (ln or {}).get("rect") or {}
            return float(rect.get("y1") or 0)

        source_lines = list(lines or [])
        # 若存在区块识别结果，优先从“聊天概率高”的区块抽取，减少无关区域干扰。
        if blocks:
            best = sorted(blocks, key=lambda b: float(b.get("chat_score") or 0.0), reverse=True)
            if best and float(best[0].get("chat_score") or 0.0) >= 0.65:
                best_rect = best[0].get("rect") or {}
                scoped = []
                for ln in source_lines:
                    rect = (ln or {}).get("rect") or {}
                    inter = self._rect_intersection_area(best_rect, rect)
                    if inter > 0:
                        scoped.append(ln)
                if scoped:
                    source_lines = scoped

        ordered = sorted(source_lines, key=_rect_y)
        for ln in ordered:
            text = str((ln or {}).get("text") or "").strip()
            rect = (ln or {}).get("rect") or {}
            if not text or len(text) < 3:
                continue
            if len(text) > 170:
                continue
            norm = text.lower()
            if any(x in norm for x in ["观众", "gmv", "流量来源", "product", "库存", "成交件数"]):
                continue

            user = "观众"
            content = text
            score = 0.45

            # 形如 "user: message" / "user：message"
            m = re.match(r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})\s*[:：]\s*(.+)$", text)
            if m:
                user = m.group(1).strip()
                content = m.group(2).strip()
                score = 0.88
            else:
                # 形如 "user message"
                m2 = re.match(r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})\s+(.+)$", text)
                if m2 and len(m2.group(2).strip()) >= 2:
                    user = m2.group(1).strip()
                    content = m2.group(2).strip()
                    score = 0.62

            if len(content) < 2:
                continue

            candidates.append(
                {
                    "user": user,
                    "text": content,
                    "raw": text,
                    "score": round(score, 3),
                    "rect": rect,
                }
            )

        # 默认取底部最新若干条
        if len(candidates) > max_messages:
            candidates = candidates[-max_messages:]
        return candidates

    def scan_image(
        self,
        img_bgr: np.ndarray,
        use_cache=True,
        max_chat_messages: int = 6,
        source: str = "image",
        capture_meta: Dict = None,
    ) -> Dict:
        now = time.time()
        if use_cache and self._last_scan and (now - self._last_scan_at <= 0.8):
            cached = dict(self._last_scan)
            cached["cached"] = True
            return cached

        if not self.available():
            return {
                "ok": False,
                "available": False,
                "error": self.ocr_engine.last_error or "ocr_provider_unavailable",
                "text": "",
                "lines": [],
                "page_type": "non_target",
            }

        if img_bgr is None or not isinstance(img_bgr, np.ndarray) or img_bgr.size == 0:
            return {
                "ok": False,
                "available": self.available(),
                "error": "invalid_image",
                "text": "",
                "lines": [],
                "page_type": "non_target",
            }

        start = time.time()
        try:
            ocr = self.ocr_engine.recognize(img_bgr, preprocess=True)
            lines = list(ocr.get("lines") or [])
            text = str(ocr.get("text") or "").strip()
            text_norm = self._norm(text)
            visual = self._analyze_visual_features(img_bgr)
            page_type = self._classify_page(text_norm, visual)
            blocks = self._detect_blocks(img_bgr, lines)
            blocks = self._assign_block_roles(blocks, visual)
            action_candidates = self._extract_action_candidates(lines)
            action_candidates.extend(self._extract_action_candidates_from_blocks(blocks))
            chat_messages = self._extract_chat_messages(lines, max_messages=max_chat_messages, blocks=blocks)
            live_state = self._extract_live_state(text_norm, lines)
            scene_tags = self._derive_scene_tags(text_norm, visual, blocks)

            payload = {
                "ok": bool(text),
                "available": True,
                "provider": str(ocr.get("provider") or self.ocr_engine.provider or ""),
                "error": str(ocr.get("error") or ""),
                "elapsed_ms": int((time.time() - start) * 1000),
                "text": text,
                "lines": lines,
                "line_count": len(lines),
                "page_type": page_type,
                "is_operable": page_type in ("shop_dashboard", "tiktok_live_dashboard"),
                "is_monitor_only": page_type in ("shop_overview",),
                "action_candidates": action_candidates,
                "chat_messages": chat_messages,
                "blocks": blocks,
                "live_state": live_state,
                "visual": visual,
                "scene_tags": scene_tags,
                "source": source,
                "cached": False,
            }
            cap = dict(capture_meta or {})
            if cap:
                payload["screen_left"] = int(cap.get("left") or 0)
                payload["screen_top"] = int(cap.get("top") or 0)
                payload["screen_width"] = int(cap.get("width") or visual.get("w") or 0)
                payload["screen_height"] = int(cap.get("height") or visual.get("h") or 0)
                payload["capture_backend"] = str(cap.get("backend") or "")
                payload["capture_elapsed_ms"] = int(cap.get("elapsed_ms") or 0)
                payload["coord_space"] = "screen"
            else:
                payload["coord_space"] = "image"
            self._last_scan = dict(payload)
            self._last_scan_at = time.time()
            return payload
        except Exception as e:
            return {
                "ok": False,
                "available": self.available(),
                "error": str(e),
                "text": "",
                "lines": [],
                "page_type": "non_target",
                "source": source,
            }

    def scan_page(self, page, use_cache=True, max_chat_messages: int = 6) -> Dict:
        if not page:
            return {
                "ok": False,
                "available": self.available(),
                "error": "no_page",
                "text": "",
                "lines": [],
                "page_type": "non_target",
            }

        try:
            png_bytes = page.get_screenshot(as_bytes="png")
            ok, img = LocalOcrEngine.decode_png_bytes(png_bytes)
            if not ok:
                return {
                    "ok": False,
                    "available": True,
                    "error": "screenshot_decode_failed",
                    "text": "",
                    "lines": [],
                    "page_type": "non_target",
                }

            return self.scan_image(
                img,
                use_cache=use_cache,
                max_chat_messages=max_chat_messages,
                source="page_screenshot",
            )
        except Exception as e:
            return {
                "ok": False,
                "available": self.available(),
                "error": str(e),
                "text": "",
                "lines": [],
                "page_type": "non_target",
            }
