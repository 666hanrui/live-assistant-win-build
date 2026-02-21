import re
import time
from typing import Dict, List

import cv2
import numpy as np

from utils.ocr_engine import LocalOcrEngine


class PageOCRReader:
    """页面 OCR 读取器：文本 + 图片特征联合识别。"""

    _CHAT_MIN_LINE_SCORE = 0.16
    _CHAT_RIGHT_PANEL_X_RATIO = 0.52
    _CHAT_EN_STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "can",
        "did",
        "do",
        "does",
        "for",
        "go",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "join",
        "me",
        "my",
        "now",
        "of",
        "on",
        "or",
        "please",
        "scan",
        "she",
        "that",
        "the",
        "they",
        "this",
        "to",
        "we",
        "what",
        "when",
        "where",
        "who",
        "why",
        "you",
        "your",
    }
    _CHAT_NOISE_KEYWORDS = [
        "voice",
        "asr",
        "ocr",
        "streamlit",
        "dashboard",
        "language=",
        "web_info_mode",
        "listen_error",
        "huggingface",
        "embedding",
        "python asr",
        "python_asr",
        "网页信息源模式",
        "运营执行模式",
        "已加载上次设置",
        "语音识别异常",
        "后台监听循环",
        "runtimeprovider",
        "welcome to tiktok live",
        "community guidelines",
        "while earning commissions",
        "scan the qr code",
        "to apply now",
        "欢迎使用 tiktok 直播",
    ]

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
    def _line_sort_key(ln: Dict):
        rect = (ln or {}).get("rect") or {}
        return (float(rect.get("y1") or 0.0), float(rect.get("x1") or 0.0))

    @staticmethod
    def _rect_iou(a: Dict, b: Dict) -> float:
        try:
            x1 = max(float(a.get("x1", 0)), float(b.get("x1", 0)))
            y1 = max(float(a.get("y1", 0)), float(b.get("y1", 0)))
            x2 = min(float(a.get("x2", 0)), float(b.get("x2", 0)))
            y2 = min(float(a.get("y2", 0)), float(b.get("y2", 0)))
            iw = max(0.0, x2 - x1)
            ih = max(0.0, y2 - y1)
            inter = iw * ih
            if inter <= 0:
                return 0.0
            area_a = max(1.0, (float(a.get("x2", 0)) - float(a.get("x1", 0))) * (float(a.get("y2", 0)) - float(a.get("y1", 0))))
            area_b = max(1.0, (float(b.get("x2", 0)) - float(b.get("x1", 0))) * (float(b.get("y2", 0)) - float(b.get("y1", 0))))
            union = max(1.0, area_a + area_b - inter)
            return inter / union
        except Exception:
            return 0.0

    @staticmethod
    def _line_confidence(ln: Dict) -> float:
        try:
            score = (ln or {}).get("score")
            if score is None:
                return 0.55
            val = float(score)
            if val > 1.0:
                val = val / 100.0
            if val < 0:
                return 0.0
            return min(1.0, val)
        except Exception:
            return 0.55

    @staticmethod
    def _line_height(ln: Dict) -> float:
        rect = (ln or {}).get("rect") or {}
        try:
            return max(1.0, float(rect.get("y2", 0)) - float(rect.get("y1", 0)))
        except Exception:
            return 1.0

    @staticmethod
    def _line_center_x(ln: Dict) -> float:
        rect = (ln or {}).get("rect") or {}
        try:
            return (float(rect.get("x1", 0)) + float(rect.get("x2", 0))) / 2.0
        except Exception:
            return 0.0

    @staticmethod
    def _sanitize_text(text: str) -> str:
        s = str(text or "").replace("\u200b", " ").replace("\xa0", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _looks_like_timestamp(text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", t):
            return True
        if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", t):
            return True
        if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}", t):
            return True
        return False

    def _is_noise_chat_line(self, text: str) -> bool:
        t = self._sanitize_text(text)
        if not t:
            return True
        lower = t.lower()
        norm = self._norm(t)
        if self._looks_like_timestamp(t):
            return True
        if re.search(r"\b(info|debug|warning|error)\b", lower) and ("|" in t or ":" in t):
            return True
        if any(k in lower for k in self._CHAT_NOISE_KEYWORDS):
            return True
        # TikTok Shop 控制台占位文案/活动流水，不应作为聊天消息。
        if any(
            k in norm
            for k in [
                "直播期间观众评论会在此处显示",
                "你在直播期间所下订单将显示在此处",
                "拖动以调整大小",
                "所有活动",
                "刚刚进入直播间",
                "joinedthelive",
            ]
        ):
            return True
        if lower.startswith("http://") or lower.startswith("https://"):
            return True
        if re.search(r"[\\/].+\.(py|md|json|log|txt)\b", lower):
            return True
        if t.count("|") >= 2:
            return True
        if re.match(r"^[\[\]():;.,\-_=+*/\\\s]+$", t):
            return True
        return False

    def _is_probable_username(self, token: str) -> bool:
        user = self._sanitize_text(token).strip(":@")
        if len(user) < 2 or len(user) > 24:
            return False
        if not re.match(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$", user):
            return False
        low = user.lower()
        if low in self._CHAT_EN_STOPWORDS:
            return False
        has_digit_or_sep = bool(re.search(r"[0-9_-]", user))
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", user))
        # 纯英文单词很容易是句子首词，避免误拆 “How do I …”。
        if user.isalpha() and (not has_digit_or_sep) and (not has_cjk) and len(user) >= 4 and low in self._CHAT_EN_STOPWORDS:
            return False
        if user.isalpha() and (not has_digit_or_sep) and (not has_cjk) and len(user) >= 6 and low == user:
            return False
        return True

    def _lines_to_text(self, lines: List[Dict], fallback: str = "") -> str:
        parts = []
        for ln in sorted(list(lines or []), key=self._line_sort_key):
            txt = self._sanitize_text((ln or {}).get("text") or "")
            if txt:
                parts.append(txt)
        if not parts:
            return str(fallback or "").strip()
        return "\n".join(parts).strip()

    def _merge_ocr_lines(self, primary: List[Dict], secondary: List[Dict]) -> List[Dict]:
        merged = []
        seen_no_rect = set()

        def _push_line(ln):
            text = self._sanitize_text((ln or {}).get("text") or "")
            if not text:
                return
            rect = (ln or {}).get("rect") or {}
            norm_text = self._norm(text)
            if not isinstance(rect, dict) or not rect:
                if norm_text in seen_no_rect:
                    return
                seen_no_rect.add(norm_text)
                merged.append({"text": text, "score": (ln or {}).get("score"), "rect": {}})
                return

            for old in merged:
                old_rect = (old or {}).get("rect") or {}
                old_norm = self._norm((old or {}).get("text") or "")
                if old_norm != norm_text:
                    continue
                if self._rect_iou(rect, old_rect) >= 0.6:
                    old_score = self._line_confidence(old)
                    new_score = self._line_confidence(ln)
                    if new_score > old_score:
                        old["text"] = text
                        old["score"] = (ln or {}).get("score")
                        old["rect"] = dict(rect)
                    return

            merged.append({"text": text, "score": (ln or {}).get("score"), "rect": dict(rect)})

        for ln in list(primary or []):
            _push_line(ln)
        for ln in list(secondary or []):
            _push_line(ln)

        merged.sort(key=self._line_sort_key)
        return merged[:1200]

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

        pre_live_keys = [
            "立即开始直播",
            "开始直播",
            "未开播",
            "暂无视频推荐",
            "直播期间观众评论会在此处显示",
            "你在直播期间所下订单将显示在此处",
            "notlive",
            "golive",
        ]
        live_keys = [
            "直播已开始",
            "正在直播",
            "开始时间",
            "开播时间",
            "实时在线人数",
            "直播曝光次数",
            "gmv",
            "viewer",
        ]

        pre_hits = [k for k in pre_live_keys if self._norm(k) in norm]
        live_hits = [k for k in live_keys if self._norm(k) in norm]
        has_timer = bool(re.search(r"\b\d{2}:\d{2}:\d{2}\b", line_text))

        pre_score = 0.0
        live_score = 0.0

        if pre_hits:
            pre_score += 0.72 + min(0.34, 0.11 * len(pre_hits))
        if live_hits:
            live_score += 0.58 + min(0.34, 0.09 * len(live_hits))
        if has_timer:
            live_score += 0.38

        # 聊天输入框可见通常意味着直播控制台主交互区已就绪（但仍不足以单独判定 live）。
        if any(k in norm for k in ["输入内容", "请输入内容", "type1andiwillarrangeit"]):
            live_score += 0.10

        is_live = bool(live_score >= 0.92 and live_score >= (pre_score + 0.16))
        phase = "live_on_air" if is_live else ("pre_live" if pre_score >= 0.66 else "unknown")
        confidence = 0.5 + min(0.48, abs(live_score - pre_score) * 0.42)
        if phase == "unknown":
            confidence = min(confidence, 0.72)

        return {
            "is_live": bool(is_live),
            "phase": phase,
            "confidence": round(float(confidence), 3),
            "pre_live_hits": pre_hits[:6],
            "live_hits": live_hits[:6],
            "has_timer": bool(has_timer),
            "scores": {"pre_live": round(pre_score, 3), "live": round(live_score, 3)},
        }

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
        has_live_room = any(k in text_norm for k in ["正在直播", "liveroom", "livechat", "关注", "观众", "聊天"])

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
            norm = self._norm(str((b or {}).get("text") or ""))
            if any(k in norm for k in ["聊天", "chat", "输入内容", "请输入内容", "商品相关"]):
                chat_score += 0.20
            if any(k in norm for k in ["所有活动", "进入直播间", "订单将显示在此处", "活动", "activity"]):
                chat_score -= 0.22
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

    def _derive_scene_tags(self, text_norm: str, visual: Dict, blocks: List[Dict], live_state: Dict = None) -> List[str]:
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
        phase = str((live_state or {}).get("phase") or "").strip().lower()
        if phase == "live_on_air":
            tags.append("live_on_air")
        elif phase == "pre_live":
            tags.append("pre_live")
        if any(k in text_norm for k in ["输入内容", "请输入内容", "type1andiwillarrangeit"]):
            tags.append("chat_input_visible")
        if any(k in text_norm for k in ["立即开始直播", "开始直播"]):
            tags.append("go_live_button_visible")
        # 去重保持顺序
        seen = set()
        dedup = []
        for t in tags:
            if t in seen:
                continue
            seen.add(t)
            dedup.append(t)
        return dedup

    def _extract_chat_messages(
        self,
        lines: List[Dict],
        max_messages: int = 6,
        blocks: List[Dict] = None,
        visual: Dict = None,
        source: str = "image",
    ) -> List[Dict]:
        candidates = []

        source_lines = list(lines or [])
        max_messages = max(1, int(max_messages or 6))
        # 若存在区块识别结果，优先从“聊天概率高”的区块抽取，减少无关区域干扰。
        scoped_by_chat_block = False
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
                    scoped_by_chat_block = True

        # 屏幕 OCR 且未定位到聊天区时，优先保留右侧区域文本，减少左侧 UI/日志串扰。
        # 对 ROI 裁剪源不再强制右侧过滤，避免把用户名/消息前缀误删。
        vis_w = int((visual or {}).get("w") or 0)
        source_tag = str(source or "").strip().lower()
        if (not scoped_by_chat_block) and vis_w > 0 and source_tag.startswith("screen") and ("roi" not in source_tag):
            right_lines = [ln for ln in source_lines if self._line_center_x(ln) >= vis_w * self._CHAT_RIGHT_PANEL_X_RATIO]
            if len(right_lines) >= max(3, max_messages):
                source_lines = right_lines

        ordered = sorted(source_lines, key=self._line_sort_key)
        idx = 0
        while idx < len(ordered):
            ln = ordered[idx]
            text = self._sanitize_text((ln or {}).get("text") or "")
            rect = (ln or {}).get("rect") or {}
            line_conf = self._line_confidence(ln)
            if line_conf < self._CHAT_MIN_LINE_SCORE:
                idx += 1
                continue
            if not text or len(text) < 2 or len(text) > 170:
                idx += 1
                continue
            if self._is_noise_chat_line(text):
                idx += 1
                continue

            user = "观众"
            content = text
            score = 0.48
            consumed_next_line = False

            # 形如 "user: message" / "user：message"
            m = re.match(r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})\s*[:：]\s*(.+)$", text)
            if m and self._is_probable_username(m.group(1)):
                user = self._sanitize_text(m.group(1))
                content = self._sanitize_text(m.group(2))
                score = 0.88
            else:
                # 形如 "user message"
                m2 = re.match(r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})\s+(.+)$", text)
                if m2 and self._is_probable_username(m2.group(1)) and len(self._sanitize_text(m2.group(2))) >= 2:
                    user = self._sanitize_text(m2.group(1))
                    content = self._sanitize_text(m2.group(2))
                    score = 0.70
                elif self._is_probable_username(text) and idx + 1 < len(ordered):
                    # 兼容 “用户名一行 + 内容下一行” 的弹幕布局。
                    nxt = ordered[idx + 1]
                    nxt_text = self._sanitize_text((nxt or {}).get("text") or "")
                    if nxt_text and (not self._is_noise_chat_line(nxt_text)):
                        y_gap = self._line_sort_key(nxt)[0] - self._line_sort_key(ln)[0]
                        gap_limit = max(12.0, 2.2 * max(self._line_height(ln), self._line_height(nxt)))
                        if 0 <= y_gap <= gap_limit:
                            user = self._sanitize_text(text)
                            content = nxt_text
                            # 使用较低者作为保守置信度。
                            score = 0.74
                            line_conf = min(line_conf, self._line_confidence(nxt))
                            consumed_next_line = True

            content = self._sanitize_text(content)
            # 英文弹幕常被 OCR 切成两行（如 "How" + "do I go Live?"），在此做轻量拼接。
            if user == "观众" and idx + 1 < len(ordered):
                words = [w for w in re.split(r"\s+", content) if w]
                first = words[0].lower() if words else ""
                if len(words) <= 1 and len(content) <= 10 and (first in self._CHAT_EN_STOPWORDS or len(content) <= 3):
                    nxt = ordered[idx + 1]
                    nxt_text = self._sanitize_text((nxt or {}).get("text") or "")
                    if nxt_text and (not self._is_noise_chat_line(nxt_text)):
                        y_gap = self._line_sort_key(nxt)[0] - self._line_sort_key(ln)[0]
                        gap_limit = max(10.0, 2.0 * max(self._line_height(ln), self._line_height(nxt)))
                        if 0 <= y_gap <= gap_limit:
                            content = self._sanitize_text(f"{content} {nxt_text}")
                            line_conf = min(line_conf, self._line_confidence(nxt))
                            consumed_next_line = True

            if len(content) < 2 or self._is_noise_chat_line(content):
                idx += 1
                continue
            if re.match(r"^[\d\s:./-]+$", content):
                idx += 1
                continue
            if len(content) >= 80 and content.count(" ") <= 1:
                idx += 1
                continue

            score = score + (line_conf - 0.55) * 0.28
            if score < 0.42:
                idx += 1
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
            if consumed_next_line:
                idx += 1
            idx += 1

        # 去重：同一轮中避免重复 OCR 行污染
        dedup = []
        seen = set()
        for item in candidates:
            key = f"{self._norm(item.get('user') or '')}|{self._norm(item.get('text') or '')}"
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(item)
        candidates = dedup

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
            visual = self._analyze_visual_features(img_bgr)
            ocr = self.ocr_engine.recognize(img_bgr, preprocess=True)
            lines = list(ocr.get("lines") or [])
            text = self._lines_to_text(lines, fallback=str(ocr.get("text") or "").strip())
            text_norm = self._norm(text)
            page_type = self._classify_page(text_norm, visual)
            blocks = self._detect_blocks(img_bgr, lines)
            blocks = self._assign_block_roles(blocks, visual)
            action_candidates = self._extract_action_candidates(lines)
            action_candidates.extend(self._extract_action_candidates_from_blocks(blocks))
            chat_messages = self._extract_chat_messages(
                lines,
                max_messages=max_chat_messages,
                blocks=blocks,
                visual=visual,
                source=source,
            )
            used_secondary_pass = False
            # 屏幕 OCR 下若弹幕召回偏弱，补一轮原图识别并融合，提升英文细字体的识别率。
            if str(source or "").startswith("screen") and len(chat_messages) < max(2, int(max_chat_messages // 2)):
                ocr_raw = self.ocr_engine.recognize(img_bgr, preprocess=False)
                raw_lines = list((ocr_raw or {}).get("lines") or [])
                if raw_lines:
                    merged_lines = self._merge_ocr_lines(lines, raw_lines)
                    merged_blocks = self._detect_blocks(img_bgr, merged_lines)
                    merged_blocks = self._assign_block_roles(merged_blocks, visual)
                    merged_chat = self._extract_chat_messages(
                        merged_lines,
                        max_messages=max_chat_messages,
                        blocks=merged_blocks,
                        visual=visual,
                        source=source,
                    )
                    if len(merged_chat) >= len(chat_messages):
                        used_secondary_pass = True
                        lines = merged_lines
                        blocks = merged_blocks
                        chat_messages = merged_chat
                        text = self._lines_to_text(merged_lines, fallback=text)
                        text_norm = self._norm(text)
                        page_type = self._classify_page(text_norm, visual)
                        action_candidates = self._extract_action_candidates(lines)
                        action_candidates.extend(self._extract_action_candidates_from_blocks(blocks))
            live_state = self._extract_live_state(text_norm, lines)
            scene_tags = self._derive_scene_tags(text_norm, visual, blocks, live_state=live_state)

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
                "ocr_secondary_pass": bool(used_secondary_pass),
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
