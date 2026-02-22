from utils.logger import logger
import time
import random
import re
import json
import platform
import subprocess
from collections import deque
from pathlib import Path
from app_config.settings import REPLY_INTERVAL, SEND_MESSAGE_MAX_CHARS, OPERATION_EXECUTION_MODE
import app_config.settings as settings
from utils.mouse_utils import (
    human_click,
    human_pause,
    human_paste,
    human_press,
    human_scroll,
    human_select_all_and_delete,
    human_typewrite,
)
from utils.vision_utils import find_button_on_screen
from utils.ocr_engine import LocalOcrEngine

class OperationsAgent:
    def __init__(self, vision_agent):
        self.vision_agent = vision_agent  # 需要访问浏览器页面
        self.reply_interval = REPLY_INTERVAL
        self.last_reply_at = 0.0
        self._last_input_selector = None
        self._last_send_selector = None
        self._fast_find_timeout = 0.12
        self._full_find_timeout = 0.35
        self.last_action_receipt = {}
        self.execution_mode = self._normalize_execution_mode(OPERATION_EXECUTION_MODE)
        self._ocr_vision_allow_dom_fallback = bool(getattr(settings, "OCR_VISION_ALLOW_DOM_FALLBACK", False))
        self._force_full_physical_chain = bool(getattr(settings, "FORCE_FULL_PHYSICAL_MOUSE_KEYBOARD", False))
        self.ocr_engine = LocalOcrEngine()
        self._last_ocr_text = ""
        self._last_ocr_error = ""
        self._last_ocr_ms = 0
        self._last_ocr_at = 0.0
        self._last_ocr_provider = self.ocr_engine.provider or ""
        self._last_ocr_lines = []
        self._last_ocr_blocks = []
        self._last_ocr_scene_tags = []
        self._last_ocr_action_candidates = []
        self._last_ocr_source = ""
        self._last_ocr_payload = {}
        self._human_like_enabled = bool(getattr(settings, "HUMAN_LIKE_ACTION_ENABLED", True))
        self._human_delay_min = max(0.0, float(getattr(settings, "HUMAN_LIKE_ACTION_DELAY_MIN_SECONDS", 0.04) or 0.04))
        self._human_delay_max = max(self._human_delay_min, float(getattr(settings, "HUMAN_LIKE_ACTION_DELAY_MAX_SECONDS", 0.20) or 0.20))
        self._human_post_min = max(0.0, float(getattr(settings, "HUMAN_LIKE_ACTION_POST_DELAY_MIN_SECONDS", 0.03) or 0.03))
        self._human_post_max = max(self._human_post_min, float(getattr(settings, "HUMAN_LIKE_ACTION_POST_DELAY_MAX_SECONDS", 0.16) or 0.16))
        self._human_click_jitter = max(0.0, float(getattr(settings, "HUMAN_LIKE_CLICK_JITTER_PX", 1.8) or 1.8))
        self._ocr_physical_click_enabled = bool(getattr(settings, "OCR_VISION_FORCE_PHYSICAL_CLICK", True))
        self._viewport_screen_use_border_comp = bool(
            getattr(settings, "OCR_VIEWPORT_TO_SCREEN_USE_BORDER_COMPENSATION", False)
        )
        self._viewport_screen_scale = max(0.5, min(3.0, float(getattr(settings, "OCR_VIEWPORT_TO_SCREEN_SCALE", 1.0) or 1.0)))
        self._viewport_screen_offset_x = float(getattr(settings, "OCR_VIEWPORT_TO_SCREEN_OFFSET_X", 0.0) or 0.0)
        self._viewport_screen_offset_y = float(getattr(settings, "OCR_VIEWPORT_TO_SCREEN_OFFSET_Y", 0.0) or 0.0)
        self._screen_ocr_mac_retina_half_scale_fallback = bool(
            getattr(settings, "SCREEN_OCR_MAC_RETINA_HALF_SCALE_FALLBACK", True)
        )
        self._screen_ocr_mac_retina_half_scale_ratio = max(
            0.35, min(0.85, float(getattr(settings, "SCREEN_OCR_MAC_RETINA_HALF_SCALE_RATIO", 0.5) or 0.5))
        )
        self._ocr_retry_wait_seconds = max(5.0, float(getattr(settings, "OCR_ACTION_RETRY_WAIT_SECONDS", 30.0) or 30.0))
        self._ocr_retry_poll_seconds = max(0.2, float(getattr(settings, "OCR_ACTION_RETRY_POLL_SECONDS", 0.65) or 0.65))
        self._ocr_reaction_llm_enabled = bool(getattr(settings, "OCR_ACTION_REACTION_LLM_ENABLED", True))
        self._ocr_reaction_llm_max_checks = max(1, int(getattr(settings, "OCR_ACTION_REACTION_LLM_MAX_CHECKS", 4) or 4))
        self._ocr_reaction_llm_min_interval = max(0.2, float(getattr(settings, "OCR_ACTION_REACTION_LLM_MIN_INTERVAL_SECONDS", 1.2) or 1.2))
        self._ocr_pin_fixed_row_click_enabled = bool(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_ENABLED", True))
        self._pin_unpin_force_fixed_row_click = bool(getattr(settings, "PIN_UNPIN_FORCE_FIXED_ROW_CLICK", True))
        self._pin_unpin_require_link_index = bool(getattr(settings, "PIN_UNPIN_REQUIRE_LINK_INDEX", True))
        self._pin_unpin_dom_rescue_enabled = bool(getattr(settings, "PIN_UNPIN_DOM_RESCUE_ENABLED", True))
        _dom_rescue_reasons = list(getattr(settings, "PIN_UNPIN_DOM_RESCUE_REASONS", []) or [])
        self._pin_unpin_dom_rescue_reasons = {
            str(x or "").strip().lower()
            for x in _dom_rescue_reasons
            if str(x or "").strip()
        }
        if not self._pin_unpin_dom_rescue_reasons:
            self._pin_unpin_dom_rescue_reasons = {
                "ocr_target_not_found_after_scroll",
                "ocr_target_not_found",
                "ocr_no_lines",
                "ocr_unavailable",
                "ocr_rect_invalid",
                "non_operable_page_before_click_shop_dashboard_required",
            }
        if self._pin_unpin_force_fixed_row_click:
            self._ocr_pin_fixed_row_click_enabled = True
        self._ocr_pin_fixed_row_click_x_ratio = float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_X_RATIO", 0.0) or 0.0)
        self._ocr_pin_fixed_row_click_right_padding_px = max(
            0.0, float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_PX", 56.0) or 56.0)
        )
        self._ocr_pin_fixed_row_click_right_padding_ratio = max(
            0.0, float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_RATIO", 0.06) or 0.06)
        )
        self._ocr_pin_fixed_row_click_panel_x_ratio = float(
            getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_PANEL_X_RATIO", 0.90) or 0.90
        )
        self._ocr_pin_fixed_row_click_offset_x_px = float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_PX", 0.0) or 0.0)
        self._ocr_pin_fixed_row_click_offset_x_ratio = max(
            0.0, float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_RATIO", 0.30) or 0.30)
        )
        self._ocr_pin_fixed_row_click_offset_y_px = float(getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_PX", 0.0) or 0.0)
        self._ocr_pin_fixed_row_click_offset_y_ratio = float(
            getattr(settings, "OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_RATIO", 0.0) or 0.0
        )
        self._ocr_pin_click_test_confirm_popup = bool(getattr(settings, "OCR_PIN_CLICK_TEST_CONFIRM_POPUP", False))
        self._ocr_pin_click_test_max_wait_seconds = max(
            0.5, float(getattr(settings, "OCR_PIN_CLICK_TEST_MAX_WAIT_SECONDS", 3.8) or 3.8)
        )
        self._ocr_pin_fixed_row_calibration_log_enabled = bool(
            getattr(settings, "OCR_PIN_FIXED_ROW_CALIBRATION_LOG_ENABLED", True)
        )
        self._ocr_pin_fixed_row_calibration_log_path = str(
            getattr(settings, "OCR_PIN_FIXED_ROW_CALIBRATION_LOG_PATH", "data/reports/pin_click_calibration.jsonl")
            or "data/reports/pin_click_calibration.jsonl"
        ).strip()
        self._last_fixed_row_click_meta = {}
        self._reaction_judge_agent = None
        self._os_keyboard_fallback_enabled = bool(getattr(settings, "OS_KEYBOARD_INPUT_FALLBACK_ENABLED", False))
        self._os_key_min_interval = max(0.001, float(getattr(settings, "OS_KEYBOARD_TYPING_MIN_INTERVAL_SECONDS", 0.018) or 0.018))
        self._os_key_max_interval = max(self._os_key_min_interval, float(getattr(settings, "OS_KEYBOARD_TYPING_MAX_INTERVAL_SECONDS", 0.055) or 0.055))
        self._message_keyboard_only_enabled = bool(getattr(settings, "MESSAGE_KEYBOARD_ONLY_ENABLED", True))
        self._message_keyboard_input_mode = self._normalize_keyboard_input_mode(getattr(settings, "MESSAGE_KEYBOARD_INPUT_MODE", "type"))
        self._human_delay_total_ms = 0.0
        self._human_delay_calls = 0
        self._human_last_delay_ms = 0.0
        self._human_last_delay_reason = ""
        self._action_trace_current = None
        self._action_trace_last = {}
        self._action_trace_history = deque(maxlen=80)
        self._last_click_driver = ""
        self._last_click_point = {}
        self._last_click_error = ""
        self._action_planner_agent = None
        self._llm_plan_enabled = bool(getattr(settings, "OPS_LLM_PLAN_ENABLED", False))
        self._llm_plan_shadow_mode = bool(getattr(settings, "OPS_LLM_PLAN_SHADOW_MODE", True))
        self._llm_plan_max_steps = max(1, int(getattr(settings, "OPS_LLM_PLAN_MAX_STEPS", 3) or 3))
        self._llm_plan_max_retries = max(0, int(getattr(settings, "OPS_LLM_PLAN_MAX_RETRIES", 1) or 1))
        self._llm_plan_timeout_seconds = max(3.0, float(getattr(settings, "OPS_LLM_PLAN_TIMEOUT_SECONDS", 18.0) or 18.0))
        self._llm_plan_min_confidence = max(0.0, min(1.0, float(getattr(settings, "OPS_LLM_PLAN_MIN_CONFIDENCE", 0.55) or 0.55)))
        self._llm_plan_next_step_enabled = bool(getattr(settings, "OPS_LLM_PLAN_NEXT_STEP_ENABLED", True))
        self._llm_plan_next_step_max_turns = max(
            1,
            int(getattr(settings, "OPS_LLM_PLAN_NEXT_STEP_MAX_TURNS", 6) or 6),
        )
        self._llm_plan_next_step_min_confidence = max(
            0.0,
            min(1.0, float(getattr(settings, "OPS_LLM_PLAN_NEXT_STEP_MIN_CONFIDENCE", 0.50) or 0.50)),
        )
        self._llm_plan_situation_driven = bool(getattr(settings, "OPS_LLM_PLAN_SITUATION_DRIVEN", True))
        self._llm_plan_replay_path = str(
            getattr(settings, "OPS_LLM_PLAN_REPLAY_PATH", "data/reports/operation_plan_replay.jsonl")
            or "data/reports/operation_plan_replay.jsonl"
        )
        self._last_action_plan_trace = {}
        self._operation_navigator_agent = None
        self._nav_llm_enabled = bool(getattr(settings, "OPS_LLM_NAVIGATION_ENABLED", True))
        self._nav_unknown_page_enabled = bool(getattr(settings, "OPS_LLM_NAVIGATION_UNKNOWN_PAGE_ENABLED", True))
        self._nav_min_confidence = max(0.0, min(1.0, float(getattr(settings, "OPS_LLM_NAVIGATION_MIN_CONFIDENCE", 0.58) or 0.58)))
        self._nav_max_scroll_rounds = max(0, int(getattr(settings, "OPS_LLM_NAVIGATION_MAX_SCROLL_ROUNDS", 6) or 6))
        self._nav_max_llm_calls = max(1, int(getattr(settings, "OPS_LLM_NAVIGATION_MAX_LLM_CALLS", 3) or 3))
        self._nav_min_interval = max(0.2, float(getattr(settings, "OPS_LLM_NAVIGATION_MIN_INTERVAL_SECONDS", 0.9) or 0.9))
        self._nav_scroll_cooldown = max(0.05, float(getattr(settings, "OPS_LLM_NAVIGATION_SCROLL_COOLDOWN_SECONDS", 0.32) or 0.32))
        self._nav_scroll_pixels = max(80, int(getattr(settings, "OPS_LLM_NAVIGATION_SCROLL_PIXELS", 180) or 180))
        self._nav_last_llm_at = 0.0
        self._last_nav_trace = {}
        self._run_js_timeout_seconds = max(
            0.2,
            float(getattr(settings, "OPS_RUN_JS_TIMEOUT_SECONDS", 1.2) or 1.2),
        )
        self._run_js_fallback_timeout_seconds = max(
            0.1,
            float(getattr(settings, "OPS_RUN_JS_FALLBACK_TIMEOUT_SECONDS", 0.45) or 0.45),
        )
        self._run_js_max_contexts = max(
            1,
            int(getattr(settings, "OPS_RUN_JS_MAX_CONTEXTS", 3) or 3),
        )
        self._run_js_include_frames = bool(getattr(settings, "OPS_RUN_JS_INCLUDE_FRAMES", False))
        self._last_js_context_name = "page"

        if self._force_full_physical_chain:
            self.execution_mode = "ocr_vision"
            self._ocr_physical_click_enabled = True
            self._ocr_vision_allow_dom_fallback = False
            self._message_keyboard_only_enabled = True
            self._os_keyboard_fallback_enabled = True

    def _normalize_execution_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode in {"dom", "ocr_vision"}:
            return mode
        return "ocr_vision"

    def _normalize_keyboard_input_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode in {"type", "paste", "auto"}:
            return mode
        return "type"

    def set_execution_mode(self, mode):
        target = self._normalize_execution_mode(mode)
        if self._force_full_physical_chain and target == "dom":
            logger.warning("强制全链路物理键鼠已开启，忽略 dom 执行模式请求，保持 ocr_vision。")
            target = "ocr_vision"
        self.execution_mode = target
        return self.execution_mode

    def get_execution_mode(self):
        return self.execution_mode

    def _dom_execution_enabled(self):
        return (not self._force_full_physical_chain) and self.get_execution_mode() == "dom"

    def _dom_fallback_enabled(self):
        if self._force_full_physical_chain:
            return False
        if self._dom_execution_enabled():
            return True
        if self._is_ocr_info_only_mode():
            return False
        return self._is_ocr_vision_mode() and bool(self._ocr_vision_allow_dom_fallback)

    def set_reaction_judge(self, judge_agent=None):
        self._reaction_judge_agent = judge_agent
        return bool(judge_agent)

    def set_action_planner(self, planner_agent=None):
        self._action_planner_agent = planner_agent
        return bool(planner_agent)

    def set_operation_navigator(self, navigator_agent=None):
        self._operation_navigator_agent = navigator_agent
        return bool(navigator_agent)

    def _is_ocr_vision_mode(self):
        return self.get_execution_mode() == "ocr_vision"

    def _is_ocr_info_only_mode(self):
        try:
            mode = str(getattr(self.vision_agent, "get_info_source_mode", lambda: "")() or "").strip().lower()
            return mode in {"ocr_only", "screen_ocr"}
        except Exception:
            return False

    def _is_screen_ocr_info_mode(self):
        try:
            mode = str(getattr(self.vision_agent, "get_info_source_mode", lambda: "")() or "").strip().lower()
            return mode == "screen_ocr"
        except Exception:
            return False

    def _log_execution_mode(self, action):
        if self._is_ocr_vision_mode():
            if self._dom_fallback_enabled():
                logger.info(f"{action}: OCR视觉模式已启用（优先OCR锚点，DOM兼容兜底）")
            else:
                logger.info(f"{action}: OCR视觉模式已启用（优先OCR锚点，DOM兜底已禁用）")

    def _human_action_delay(self, reason=""):
        if not self._human_like_enabled:
            return
        slept = float(human_pause(self._human_delay_min, self._human_delay_max, reason=reason) or 0.0)
        self._record_human_delay(reason=reason, slept_seconds=slept)

    def _human_action_post_delay(self, reason=""):
        if not self._human_like_enabled:
            return
        slept = float(human_pause(self._human_post_min, self._human_post_max, reason=reason) or 0.0)
        self._record_human_delay(reason=reason, slept_seconds=slept)

    def _begin_action_trace(self, action):
        self._action_trace_current = {
            "action": str(action or ""),
            "start_at": time.time(),
            "delay_ms": 0.0,
            "delay_count": 0,
            "last_delay_reason": "",
            "last_delay_ms": 0.0,
        }

    def _record_human_delay(self, reason="", slept_seconds=0.0):
        ms = max(0.0, float(slept_seconds or 0.0) * 1000.0)
        self._human_delay_total_ms += ms
        self._human_delay_calls += 1
        self._human_last_delay_ms = ms
        self._human_last_delay_reason = str(reason or "")
        trace = self._action_trace_current if isinstance(self._action_trace_current, dict) else None
        if trace:
            trace["delay_ms"] = float(trace.get("delay_ms") or 0.0) + ms
            trace["delay_count"] = int(trace.get("delay_count") or 0) + 1
            trace["last_delay_reason"] = str(reason or "")
            trace["last_delay_ms"] = ms

    def _finalize_action_trace(self, action, ok=None, note=""):
        trace = self._action_trace_current if isinstance(self._action_trace_current, dict) else None
        if not trace:
            return dict(self._action_trace_last or {})
        if str(trace.get("action") or "") != str(action or ""):
            return dict(self._action_trace_last or {})
        finished = dict(trace)
        finished["ok"] = bool(ok)
        finished["note"] = str(note or "")
        finished["end_at"] = time.time()
        finished["elapsed_ms"] = int(max(0.0, (finished["end_at"] - float(trace.get("start_at") or finished["end_at"])) * 1000.0))
        finished["delay_ms"] = int(max(0.0, float(finished.get("delay_ms") or 0.0)))
        finished["last_delay_ms"] = int(max(0.0, float(finished.get("last_delay_ms") or 0.0)))
        self._action_trace_last = dict(finished)
        self._action_trace_history.append(dict(finished))
        self._action_trace_current = None
        return dict(finished)

    def get_human_like_settings(self):
        return {
            "enabled": bool(self._human_like_enabled),
            "delay_min_seconds": float(self._human_delay_min),
            "delay_max_seconds": float(self._human_delay_max),
            "post_delay_min_seconds": float(self._human_post_min),
            "post_delay_max_seconds": float(self._human_post_max),
            "click_jitter_px": float(self._human_click_jitter),
            "ocr_physical_click_enabled": bool(self._ocr_physical_click_enabled),
            "keyboard_fallback_enabled": bool(self._os_keyboard_fallback_enabled),
            "typing_min_interval_seconds": float(self._os_key_min_interval),
            "typing_max_interval_seconds": float(self._os_key_max_interval),
            "message_keyboard_only_enabled": bool(self._message_keyboard_only_enabled),
            "message_keyboard_input_mode": str(self._message_keyboard_input_mode),
            "ocr_vision_allow_dom_fallback": bool(self._ocr_vision_allow_dom_fallback),
            "force_full_physical_chain": bool(self._force_full_physical_chain),
            "pin_click_test_confirm_popup": bool(self._ocr_pin_click_test_confirm_popup),
        }

    def set_human_like_settings(self, **kwargs):
        if "enabled" in kwargs:
            self._human_like_enabled = bool(kwargs.get("enabled"))
        if "delay_min_seconds" in kwargs:
            self._human_delay_min = max(0.0, float(kwargs.get("delay_min_seconds") or self._human_delay_min))
        if "delay_max_seconds" in kwargs:
            self._human_delay_max = max(self._human_delay_min, float(kwargs.get("delay_max_seconds") or self._human_delay_max))
        if "post_delay_min_seconds" in kwargs:
            self._human_post_min = max(0.0, float(kwargs.get("post_delay_min_seconds") or self._human_post_min))
        if "post_delay_max_seconds" in kwargs:
            self._human_post_max = max(self._human_post_min, float(kwargs.get("post_delay_max_seconds") or self._human_post_max))
        if "click_jitter_px" in kwargs:
            self._human_click_jitter = max(0.0, float(kwargs.get("click_jitter_px") or self._human_click_jitter))
        if "ocr_physical_click_enabled" in kwargs:
            self._ocr_physical_click_enabled = bool(kwargs.get("ocr_physical_click_enabled"))
        if "keyboard_fallback_enabled" in kwargs:
            self._os_keyboard_fallback_enabled = bool(kwargs.get("keyboard_fallback_enabled"))
        if "typing_min_interval_seconds" in kwargs:
            self._os_key_min_interval = max(0.001, float(kwargs.get("typing_min_interval_seconds") or self._os_key_min_interval))
        if "typing_max_interval_seconds" in kwargs:
            self._os_key_max_interval = max(self._os_key_min_interval, float(kwargs.get("typing_max_interval_seconds") or self._os_key_max_interval))
        if "message_keyboard_only_enabled" in kwargs:
            self._message_keyboard_only_enabled = bool(kwargs.get("message_keyboard_only_enabled"))
        if "message_keyboard_input_mode" in kwargs:
            self._message_keyboard_input_mode = self._normalize_keyboard_input_mode(kwargs.get("message_keyboard_input_mode"))
        if "ocr_vision_allow_dom_fallback" in kwargs:
            self._ocr_vision_allow_dom_fallback = bool(kwargs.get("ocr_vision_allow_dom_fallback"))
        if "force_full_physical_chain" in kwargs:
            self._force_full_physical_chain = bool(kwargs.get("force_full_physical_chain"))
        if "pin_click_test_confirm_popup" in kwargs:
            self._ocr_pin_click_test_confirm_popup = bool(kwargs.get("pin_click_test_confirm_popup"))
        if self._force_full_physical_chain:
            self.execution_mode = "ocr_vision"
            self._ocr_physical_click_enabled = True
            self._ocr_vision_allow_dom_fallback = False
            self._message_keyboard_only_enabled = True
            self._os_keyboard_fallback_enabled = True
        return self.get_human_like_settings()

    def get_human_like_stats(self, recent_limit=8):
        items = list(self._action_trace_history)[-max(1, int(recent_limit or 8)) :]
        return {
            "delay_total_ms": int(max(0.0, self._human_delay_total_ms)),
            "delay_calls": int(self._human_delay_calls),
            "last_delay_ms": int(max(0.0, self._human_last_delay_ms)),
            "last_delay_reason": str(self._human_last_delay_reason or ""),
            "last_action_trace": dict(self._action_trace_last or {}),
            "recent_action_traces": items,
        }

    def _build_ocr_payload_from_scan(self, scan, source_hint=""):
        if not isinstance(scan, dict):
            scan = {}
        lines = list(scan.get("lines") or [])
        text = str(scan.get("text") or "").strip()
        blocks = list(scan.get("blocks") or [])
        scene_tags = list(scan.get("scene_tags") or [])
        action_candidates = list(scan.get("action_candidates") or [])
        visual = scan.get("visual") or {}
        payload = {
            "ok": bool(text),
            "text": text,
            "lines": lines,
            "blocks": blocks,
            "scene_tags": scene_tags,
            "action_candidates": action_candidates,
            "provider": str(scan.get("provider") or self.ocr_engine.provider or ""),
            "error": str(scan.get("error") or ""),
            "elapsed_ms": int(scan.get("elapsed_ms") or 0),
            "cached": bool(scan.get("cached")),
            "available": bool(scan.get("available", True)),
            "page_type": str(scan.get("page_type") or ""),
            "is_operable": bool(scan.get("is_operable")),
            "is_monitor_only": bool(scan.get("is_monitor_only")),
            "live_state": dict(scan.get("live_state") or {}),
            "image_w": int(visual.get("w") or scan.get("screen_width") or 0),
            "image_h": int(visual.get("h") or scan.get("screen_height") or 0),
            "screen_width": int(scan.get("screen_width") or visual.get("w") or 0),
            "screen_height": int(scan.get("screen_height") or visual.get("h") or 0),
            "screen_left": int(scan.get("screen_left") or 0),
            "screen_top": int(scan.get("screen_top") or 0),
            "coord_space": str(scan.get("coord_space") or "image"),
            "capture_backend": str(scan.get("capture_backend") or ""),
            "source": str(scan.get("source") or source_hint or ""),
        }
        self._last_ocr_text = text
        self._last_ocr_lines = lines
        self._last_ocr_blocks = blocks
        self._last_ocr_scene_tags = scene_tags
        self._last_ocr_action_candidates = action_candidates
        self._last_ocr_error = payload["error"]
        self._last_ocr_ms = payload["elapsed_ms"]
        self._last_ocr_at = time.time()
        self._last_ocr_provider = payload["provider"]
        self._last_ocr_payload = dict(payload)
        return payload

    def _ocr_extract_page_text(self, use_cache=True):
        now = time.time()
        expect_screen = self._is_screen_ocr_info_mode()
        cache_valid_source = (self._last_ocr_source == "screen") if expect_screen else (self._last_ocr_source != "screen")
        if use_cache and cache_valid_source and (now - self._last_ocr_at <= 0.8):
            if isinstance(self._last_ocr_payload, dict) and self._last_ocr_payload:
                cached = dict(self._last_ocr_payload)
                cached["cached"] = True
                cached["available"] = bool(cached.get("available", self.ocr_engine.available()))
                if "text" not in cached:
                    cached["text"] = self._last_ocr_text
                if "lines" not in cached:
                    cached["lines"] = list(self._last_ocr_lines or [])
                if "blocks" not in cached:
                    cached["blocks"] = list(self._last_ocr_blocks or [])
                if "scene_tags" not in cached:
                    cached["scene_tags"] = list(self._last_ocr_scene_tags or [])
                if "action_candidates" not in cached:
                    cached["action_candidates"] = list(self._last_ocr_action_candidates or [])
                if "provider" not in cached:
                    cached["provider"] = self._last_ocr_provider
                if "error" not in cached:
                    cached["error"] = self._last_ocr_error
                if "elapsed_ms" not in cached:
                    cached["elapsed_ms"] = self._last_ocr_ms
                return cached
            if self._last_ocr_text:
                return {
                    "ok": bool(self._last_ocr_text),
                    "text": self._last_ocr_text,
                    "lines": list(self._last_ocr_lines or []),
                    "blocks": list(self._last_ocr_blocks or []),
                    "scene_tags": list(self._last_ocr_scene_tags or []),
                    "action_candidates": list(self._last_ocr_action_candidates or []),
                    "provider": self._last_ocr_provider,
                    "error": self._last_ocr_error,
                    "elapsed_ms": self._last_ocr_ms,
                    "cached": True,
                    "available": self.ocr_engine.available(),
                }

        # 纯屏幕 OCR 信息源：不读取浏览器 DOM/截图，仅使用屏幕采集结果。
        if self._is_screen_ocr_info_mode():
            try:
                getter = getattr(self.vision_agent, "get_latest_ocr_scan", None)
                if callable(getter):
                    scan = getter(use_cache=use_cache, max_chat_messages=1) or {}
                else:
                    scan = {}
                payload = self._build_ocr_payload_from_scan(scan, source_hint="screen_capture")
                payload["coord_space"] = str(payload.get("coord_space") or "screen")
                self._last_ocr_source = "screen"
                self._last_ocr_payload = dict(payload)
                return payload
            except Exception as e:
                self._last_ocr_error = str(e)
                return {
                    "ok": False,
                    "text": "",
                    "lines": [],
                    "blocks": [],
                    "scene_tags": [],
                    "action_candidates": [],
                    "provider": self._last_ocr_provider,
                    "error": str(e),
                    "elapsed_ms": 0,
                    "cached": False,
                    "available": self.ocr_engine.available(),
                }

        # OCR 视觉执行链路：优先走 VisionAgent 的 scan 结果，包含 blocks/scene_tags/action_candidates。
        if self._is_ocr_vision_mode() or self._is_ocr_info_only_mode():
            try:
                getter = getattr(self.vision_agent, "get_latest_ocr_scan", None)
                if callable(getter):
                    scan = getter(use_cache=use_cache, max_chat_messages=1) or {}
                    payload = self._build_ocr_payload_from_scan(scan, source_hint="page_screenshot")
                    if payload.get("ok") or payload.get("lines") or payload.get("blocks") or payload.get("action_candidates"):
                        self._last_ocr_source = "page_scan"
                        self._last_ocr_payload = dict(payload)
                        return payload
            except Exception:
                pass

        page = getattr(self.vision_agent, "page", None)
        if not page:
            self._last_ocr_error = "no_page"
            return {
                "ok": False,
                "text": "",
                "lines": [],
                "blocks": [],
                "scene_tags": [],
                "action_candidates": [],
                "provider": self._last_ocr_provider,
                "error": "no_page",
                "elapsed_ms": 0,
                "cached": False,
                "available": self.ocr_engine.available(),
            }
        if not self.ocr_engine.available():
            self._last_ocr_error = self.ocr_engine.last_error or "ocr_provider_unavailable"
            return {
                "ok": False,
                "text": "",
                "lines": [],
                "blocks": [],
                "scene_tags": [],
                "action_candidates": [],
                "provider": self._last_ocr_provider,
                "error": self._last_ocr_error,
                "elapsed_ms": 0,
                "cached": False,
                "available": False,
            }

        try:
            png_bytes = page.get_screenshot(as_bytes="png")
            ok, img = LocalOcrEngine.decode_png_bytes(png_bytes)
            if not ok:
                self._last_ocr_error = "screenshot_decode_failed"
                return {
                    "ok": False,
                    "text": "",
                    "lines": [],
                    "blocks": [],
                    "scene_tags": [],
                    "action_candidates": [],
                    "provider": self._last_ocr_provider,
                    "error": self._last_ocr_error,
                    "elapsed_ms": 0,
                    "cached": False,
                    "available": True,
                }
            result = self.ocr_engine.recognize(img, preprocess=True)
            self._last_ocr_text = str(result.get("text") or "").strip()
            self._last_ocr_lines = list(result.get("lines") or [])
            self._last_ocr_blocks = []
            self._last_ocr_scene_tags = []
            self._last_ocr_action_candidates = []
            self._last_ocr_error = str(result.get("error") or "")
            self._last_ocr_ms = int(result.get("elapsed_ms") or 0)
            self._last_ocr_at = time.time()
            self._last_ocr_provider = str(result.get("provider") or self.ocr_engine.provider or "")
            result["image_w"] = int(img.shape[1]) if getattr(img, "shape", None) is not None else 0
            result["image_h"] = int(img.shape[0]) if getattr(img, "shape", None) is not None else 0
            result["blocks"] = []
            result["scene_tags"] = []
            result["action_candidates"] = []
            try:
                _, vp = self._run_js_in_contexts(
                    "return {w:window.innerWidth||0,h:window.innerHeight||0,scrollY:window.scrollY||0};"
                )
                if isinstance(vp, dict):
                    result["view_w"] = int(vp.get("w") or 0)
                    result["view_h"] = int(vp.get("h") or 0)
                    result["scroll_y"] = int(vp.get("scrollY") or 0)
            except Exception:
                pass
            result["cached"] = False
            result["available"] = True
            if "page_type" not in result:
                result["page_type"] = ""
            if "is_operable" not in result:
                result["is_operable"] = False
            if "is_monitor_only" not in result:
                result["is_monitor_only"] = False
            self._last_ocr_source = "page"
            self._last_ocr_payload = dict(result)
            return result
        except Exception as e:
            self._last_ocr_error = str(e)
            return {
                "ok": False,
                "text": "",
                "lines": [],
                "blocks": [],
                "scene_tags": [],
                "action_candidates": [],
                "provider": self._last_ocr_provider,
                "error": str(e),
                "elapsed_ms": 0,
                "cached": False,
                "available": self.ocr_engine.available(),
            }

    def _norm_ocr_text(self, text):
        s = str(text or "").lower()
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)

    def _match_flash_action_text(self, norm_text, action):
        s = self._norm_ocr_text(norm_text)
        if not s:
            return {"ok": False, "score": 0.0, "explicit": False}

        flash_terms = ["秒杀", "秒殺", "flashsale", "flashdeal", "flashpromo", "flashpromotion"]
        start_terms = ["上架", "開始", "开始", "开启", "launch", "start", "open", "enable"]
        stop_terms = ["结束", "結束", "停止", "下架", "关闭", "關閉", "撤下", "end", "stop", "disable", "close"]
        has_flash = any(t in s for t in flash_terms)
        has_start = any(t in s for t in start_terms)
        has_stop = any(t in s for t in stop_terms)

        explicit_start = any(
            t in s
            for t in [
                "秒杀活动上架",
                "秒殺活動上架",
                "秒杀上架",
                "秒殺上架",
                "开启秒杀",
                "開始秒殺",
                "开始秒杀",
                "launchflashsale",
                "startflashsale",
                "openflashsale",
            ]
        )
        explicit_stop = any(
            t in s
            for t in [
                "结束秒杀活动",
                "結束秒殺活動",
                "结束秒杀",
                "結束秒殺",
                "停止秒杀",
                "停止秒殺",
                "下架秒杀",
                "下架秒殺",
                "stopflashsale",
                "endflashsale",
                "closeflashsale",
            ]
        )

        if action == "start_flash_sale":
            ok = explicit_start or (has_flash and has_start and (not has_stop))
            score = (8.0 if explicit_start else 0.0) + (3.0 if has_flash else 0.0) + (2.0 if has_start else 0.0) - (4.0 if has_stop else 0.0)
            return {"ok": bool(ok), "score": score, "explicit": bool(explicit_start)}
        if action == "stop_flash_sale":
            ok = explicit_stop or (has_flash and has_stop and (not has_start))
            score = (8.0 if explicit_stop else 0.0) + (3.0 if has_flash else 0.0) + (2.0 if has_stop else 0.0) - (4.0 if has_start else 0.0)
            return {"ok": bool(ok), "score": score, "explicit": bool(explicit_stop)}
        return {"ok": False, "score": 0.0, "explicit": False}

    def _collect_ocr_signal_lines(self, ocr):
        lines = []
        for raw_line in list((ocr or {}).get("lines") or []):
            if not isinstance(raw_line, dict):
                continue
            role = str(raw_line.get("role") or "").strip().lower()
            if role in {"chat_panel", "metrics_panel"}:
                continue
            txt = str(raw_line.get("text") or "").strip()
            if not txt:
                continue
            if self._is_streamlit_panel_noise_text(txt):
                continue
            if self._is_noisy_non_button_text(txt):
                continue
            norm = self._norm_ocr_text(txt)
            if len(norm) < 2:
                continue
            lines.append({"norm": norm, "text": txt[:120]})
        return lines

    def _collect_ocr_signal_blocks(self, ocr):
        blocks = []
        for raw_block in list((ocr or {}).get("blocks") or []):
            if not isinstance(raw_block, dict):
                continue
            role = str(raw_block.get("role") or "").strip().lower()
            if role in {"chat_panel", "metrics_panel"}:
                continue
            txt = str(raw_block.get("text") or "").strip()
            if not txt:
                continue
            if self._is_streamlit_panel_noise_text(txt):
                continue
            norm = self._norm_ocr_text(txt)
            if len(norm) < 4:
                continue
            blocks.append({"norm": norm, "text": txt[:160], "role": role})
        return blocks

    def _build_ocr_reaction_snapshot(self, ocr):
        lines = self._collect_ocr_signal_lines(ocr)
        blocks = self._collect_ocr_signal_blocks(ocr)
        line_keys = set()
        line_preview = {}
        block_keys = set()
        block_preview = {}
        for item in lines:
            n = str(item.get("norm") or "")
            if not n:
                continue
            line_keys.add(n)
            line_preview.setdefault(n, str(item.get("text") or ""))
        for item in blocks:
            n = str(item.get("norm") or "")
            if not n:
                continue
            block_keys.add(n)
            block_preview.setdefault(n, {"text": str(item.get("text") or ""), "role": str(item.get("role") or "")})
        return {
            "text_norm": self._norm_ocr_text((ocr or {}).get("text") or ""),
            "line_keys": line_keys,
            "line_preview": line_preview,
            "block_keys": block_keys,
            "block_preview": block_preview,
        }

    def _build_reaction_terms(self, action):
        common = [
            "成功", "失败", "失敗", "错误", "錯誤", "提示", "已", "处理中", "加载",
            "請稍後", "请稍后", "toast", "success", "failed", "error", "warning",
            "popup", "dialog", "modal", "confirm",
        ]
        if action == "start_flash_sale":
            common.extend(["秒杀", "秒殺", "上架", "开启", "开始", "进行中", "结束秒杀", "stopflash", "endflash"])
        elif action == "stop_flash_sale":
            common.extend(["秒杀", "秒殺", "结束", "停止", "下架", "已结束", "可上架", "startflash", "launchflash"])
        return [self._norm_ocr_text(x) for x in common if self._norm_ocr_text(x)]

    def _detect_ocr_reaction_change(self, action, baseline_snapshot, ocr):
        if not isinstance(baseline_snapshot, dict):
            return None
        current = self._build_ocr_reaction_snapshot(ocr)
        base_line_keys = set(baseline_snapshot.get("line_keys") or set())
        curr_line_keys = set(current.get("line_keys") or set())
        base_block_keys = set(baseline_snapshot.get("block_keys") or set())
        curr_block_keys = set(current.get("block_keys") or set())

        line_preview = dict(current.get("line_preview") or {})
        block_preview = dict(current.get("block_preview") or {})
        terms = self._build_reaction_terms(action)

        new_line_items = []
        for key in curr_line_keys:
            if key in base_line_keys:
                continue
            txt = str(line_preview.get(key) or "")
            if txt:
                new_line_items.append({"norm": key, "text": txt})

        new_block_items = []
        for key in curr_block_keys:
            if key in base_block_keys:
                continue
            item = block_preview.get(key) or {}
            txt = str(item.get("text") or "")
            if txt:
                new_block_items.append({"norm": key, "text": txt, "role": str(item.get("role") or "")})

        base_text = str(baseline_snapshot.get("text_norm") or "")
        curr_text = str(current.get("text_norm") or "")
        text_changed = bool(curr_text and curr_text != base_text and abs(len(curr_text) - len(base_text)) >= 8)

        if not new_line_items and not new_block_items and not text_changed:
            return None

        signal_hit = False
        for item in new_line_items + new_block_items:
            n = str(item.get("norm") or "")
            if terms and any(t in n for t in terms):
                signal_hit = True
                break
        if (not signal_hit) and text_changed and terms and any(t in curr_text for t in terms):
            signal_hit = True

        signature_parts = []
        signature_parts.extend(sorted(str(item.get("norm") or "") for item in new_line_items))
        signature_parts.extend(sorted(str(item.get("norm") or "") for item in new_block_items))
        if text_changed:
            signature_parts.append(f"text:{curr_text[:48]}")
        signature = "|".join(signature_parts)[:600]

        return {
            "action": action,
            "signal_hit": bool(signal_hit),
            "new_lines": [i.get("text") for i in new_line_items[:6]],
            "new_blocks": [{"text": i.get("text"), "role": i.get("role")} for i in new_block_items[:4]],
            "text_changed": bool(text_changed),
            "text_sample": curr_text[:180] if text_changed else "",
            "signature": signature,
        }

    def _extract_json_payload(self, text):
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def _judge_reaction_by_rules(self, action, reaction_candidate):
        candidate = dict(reaction_candidate or {})
        if not candidate:
            return {"has_reaction": False, "judge_source": "rules", "reason": "empty_candidate", "confidence": 0.0}

        terms = set(self._build_reaction_terms(action))
        score = 0.0
        evidence = []

        for txt in list(candidate.get("new_lines") or []):
            n = self._norm_ocr_text(txt)
            if any(t in n for t in terms):
                score += 0.36
                evidence.append(txt[:60])

        for item in list(candidate.get("new_blocks") or []):
            txt = str((item or {}).get("text") or "")
            role = str((item or {}).get("role") or "")
            n = self._norm_ocr_text(txt)
            if role in {"action_panel", "main_content", "product_panel"}:
                score += 0.20
            if any(t in n for t in terms):
                score += 0.32
                evidence.append(txt[:60])

        if bool(candidate.get("text_changed")) and any(t in str(candidate.get("text_sample") or "") for t in terms):
            score += 0.22

        has_reaction = score >= 0.66
        return {
            "has_reaction": bool(has_reaction),
            "judge_source": "rules",
            "reason": "rule_signal_hit" if has_reaction else "rule_signal_weak",
            "confidence": round(min(0.99, max(0.01, score)), 3),
            "evidence": evidence[:3],
        }

    def _judge_reaction_with_llm(self, action, baseline_snapshot, reaction_candidate, ocr_now, elapsed_ms):
        if not self._ocr_reaction_llm_enabled:
            return None
        agent = self._reaction_judge_agent
        llm = getattr(agent, "llm", None) if agent else None
        has_llm = bool(getattr(agent, "has_llm", False))
        if (not has_llm) or (llm is None):
            return None

        baseline_lines = list((baseline_snapshot or {}).get("line_preview", {}).values())[:8]
        baseline_blocks = []
        for item in list((baseline_snapshot or {}).get("block_preview", {}).values())[:4]:
            if not isinstance(item, dict):
                continue
            baseline_blocks.append({"text": str(item.get("text") or "")[:100], "role": str(item.get("role") or "")})

        current_lines = [x.get("text") for x in self._collect_ocr_signal_lines(ocr_now)[:10]]
        current_blocks = [{"text": x.get("text"), "role": x.get("role")} for x in self._collect_ocr_signal_blocks(ocr_now)[:6]]

        payload = {
            "action": action,
            "elapsed_ms_after_click": int(elapsed_ms or 0),
            "candidate_change": reaction_candidate or {},
            "baseline_lines": baseline_lines,
            "baseline_blocks": baseline_blocks,
            "current_lines": current_lines,
            "current_blocks": current_blocks,
            "scene_tags": list((ocr_now or {}).get("scene_tags") or [])[:8],
            "page_type": str((ocr_now or {}).get("page_type") or ""),
        }
        prompt = (
            "你是直播运营动作的页面反馈判定器。"
            "请基于下面的OCR增量变化，判断“点击后页面是否已经出现新反馈（例如弹窗、提示文本、状态切换）”。"
            "只输出JSON，不要输出任何额外文本。"
            "JSON格式: {\"has_reaction\":true|false,\"reaction_type\":\"popup|toast|status_text|none|unknown\","
            "\"confidence\":0~1,\"reason\":\"...\",\"evidence\":[\"...\"]}\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = llm.invoke(prompt)
            raw = str(getattr(response, "content", response) or "").strip()
            data = self._extract_json_payload(raw)
            if not isinstance(data, dict):
                return {
                    "has_reaction": False,
                    "judge_source": "llm_parse_failed",
                    "confidence": 0.0,
                    "reason": "llm_json_parse_failed",
                    "raw": raw[:220],
                }
            return {
                "has_reaction": bool(data.get("has_reaction")),
                "reaction_type": str(data.get("reaction_type") or "unknown"),
                "confidence": float(data.get("confidence") or 0.0),
                "reason": str(data.get("reason") or ""),
                "evidence": list(data.get("evidence") or [])[:3],
                "judge_source": "llm",
                "raw": raw[:220],
            }
        except Exception as e:
            return {
                "has_reaction": False,
                "judge_source": "llm_error",
                "confidence": 0.0,
                "reason": f"llm_error:{e}",
            }

    def _wait_for_ocr_feedback_after_click(self, action, baseline_snapshot, link_index=None, timeout_seconds=30.0):
        timeout = max(0.0, float(timeout_seconds or 0.0))
        if timeout <= 0:
            return {"verified": None, "reaction": None, "elapsed_ms": 0, "judges": []}

        started = time.time()
        deadline = started + timeout
        poll = max(0.2, float(self._ocr_retry_poll_seconds or 0.65))
        checked_signatures = set()
        judge_history = []
        llm_checks = 0
        last_llm_at = 0.0

        while True:
            ocr_now = self._ocr_extract_page_text(use_cache=False)
            text_norm = self._norm_ocr_text(ocr_now.get("text") or "")
            verified = self._verify_receipt_from_ocr_text(action, text_norm, link_index=link_index)
            if verified:
                return {
                    "verified": verified,
                    "reaction": {"ok": True, "source": "ocr_receipt"},
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "judges": judge_history[-4:],
                }

            candidate = self._detect_ocr_reaction_change(action, baseline_snapshot, ocr_now)
            signature = str((candidate or {}).get("signature") or "")
            if candidate and signature and signature not in checked_signatures:
                checked_signatures.add(signature)
                elapsed_ms = int((time.time() - started) * 1000)

                llm_judge = None
                now = time.time()
                if llm_checks < self._ocr_reaction_llm_max_checks and (now - last_llm_at) >= self._ocr_reaction_llm_min_interval:
                    llm_judge = self._judge_reaction_with_llm(action, baseline_snapshot, candidate, ocr_now, elapsed_ms)
                    if llm_judge is not None:
                        llm_checks += 1
                        last_llm_at = now

                rule_judge = self._judge_reaction_by_rules(action, candidate)
                final_judge = llm_judge if isinstance(llm_judge, dict) else rule_judge
                if isinstance(llm_judge, dict) and (not bool(llm_judge.get("has_reaction"))):
                    # LLM判定“无反应”时，若规则信号很强，仍视为有反应，避免漏判明显弹窗。
                    if bool(rule_judge.get("has_reaction")) and float(rule_judge.get("confidence") or 0.0) >= 0.86:
                        final_judge = {
                            "has_reaction": True,
                            "judge_source": "llm_plus_rules",
                            "confidence": float(rule_judge.get("confidence") or 0.0),
                            "reason": "rule_strong_override",
                            "evidence": list(rule_judge.get("evidence") or [])[:3],
                        }

                judge_item = {
                    "elapsed_ms": elapsed_ms,
                    "candidate": candidate,
                    "llm": llm_judge or {},
                    "rules": rule_judge or {},
                    "final": final_judge or {},
                }
                judge_history.append(judge_item)
                logger.info(
                    "OCR反应判定: action=%s elapsed=%sms source=%s has_reaction=%s confidence=%.3f reason=%s",
                    action,
                    elapsed_ms,
                    str((final_judge or {}).get("judge_source") or ""),
                    bool((final_judge or {}).get("has_reaction")),
                    float((final_judge or {}).get("confidence") or 0.0),
                    str((final_judge or {}).get("reason") or ""),
                )

                if bool((final_judge or {}).get("has_reaction")):
                    return {
                        "verified": None,
                        "reaction": {
                            "ok": True,
                            "source": "reaction_judge",
                            "judge": final_judge,
                            "candidate": candidate,
                        },
                        "elapsed_ms": elapsed_ms,
                        "judges": judge_history[-4:],
                    }

            now = time.time()
            if now >= deadline:
                break
            time.sleep(min(poll, max(0.05, deadline - now)))

        return {
            "verified": None,
            "reaction": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "judges": judge_history[-4:],
        }

    def _is_noisy_non_button_text(self, text):
        t = str(text or "")
        norm = self._norm_ocr_text(t)
        noise_terms = [
            "库存",
            "存量",
            "点击",
            "加购",
            "成交",
            "序号",
            "号链接",
            "msgem",
            "hair",
            "price",
            "stock",
            "orders",
            "viewer",
            "聊天",
            "评论",
            "输入内容",
        ]
        has_noise = any(k in t.lower() for k in noise_terms) or any(k in norm for k in ["库存", "点击", "加购", "成交", "序号"])
        has_price = ("$" in t) or ("￥" in t) or bool(re.search(r"\d{2,}\.\d{1,2}", t))
        too_long = len(t) >= 34
        return bool((has_noise and (too_long or has_price)) or len(norm) > 60)

    def _is_streamlit_panel_noise_text(self, text):
        """
        过滤本项目控制台自身文案，避免 screen_ocr 误把左侧控制台文本当作可点击运营按钮。
        """
        t = str(text or "").strip()
        if not t:
            return False
        n = self._norm_ocr_text(t)
        noise_terms = [
            "最近识别",
            "最新输入",
            "语音输入",
            "运行状态",
            "核心可用率",
            "语音模式",
            "asrprovider",
            "starterror",
            "voiceerror",
            "应用回复设置",
            "连接浏览器",
            "启动监听",
            "停止监听",
            "运行系统自检",
            "打开模拟网页测试",
            "使用说明书",
            "统一语言",
            "语气模板",
            "command=",
            "status=",
            "note=",
            "action_done",
            "ocr源",
            "ocr耗时",
            "ocr行数",
        ]
        return any(k in n for k in noise_terms)

    def _is_browser_chrome_noise_text(self, text):
        """
        过滤浏览器地址栏/系统路径等噪声文本，避免把 URL 当作“置顶/取消置顶”按钮。
        """
        t = str(text or "").strip()
        if not t:
            return False
        low = t.lower()
        markers = [
            "http://",
            "https://",
            "file://",
            "localhost",
            "127.0.0.1",
            "/users/",
            "\\users\\",
            "desktop/",
            "mock_tiktok_shop",
            ".html?",
            ".html",
        ]
        if any(m in low for m in markers):
            return True
        # 地址/路径文本通常分隔符密集，且不应被当作按钮。
        slash_count = low.count("/") + low.count("\\")
        return slash_count >= 3

    def _is_command_like_text(self, text):
        """
        过滤口令/聊天中的“置顶xx号链接”文本，避免误当作商品行锚点。
        """
        raw = str(text or "").strip()
        if not raw:
            return False
        n = self._norm_ocr_text(raw)
        markers = [
            "助播",
            "助手",
            "assistant",
            "cohost",
            "置顶",
            "取消置顶",
            "重新置顶",
            "口令",
            "命令",
            "pinlink",
            "unpinlink",
            "pin",
            "unpin",
            "top",
            "链接",
        ]
        hit = any(m in n for m in markers)
        if not hit:
            return False
        # 只有同时带“动作词 + 数字索引”才判定为口令类噪声。
        has_idx = bool(re.search(r"([0-9]{1,3}|一|二|三|四|五|六|七|八|九|十)", raw))
        return bool(has_idx)

    def _match_pin_unpin_target_text(self, text, action):
        """
        pin/unpin 目标词匹配：英文使用词边界，避免 desktop/top 误命中。
        """
        raw = str(text or "").strip()
        if not raw:
            return False
        if self._is_streamlit_panel_noise_text(raw) or self._is_browser_chrome_noise_text(raw):
            return False
        low = raw.lower()
        norm = self._norm_ocr_text(raw)
        if action == "unpin_product":
            if any(k in norm for k in ["取消置顶", "取消顶置", "撤销置顶", "去掉置顶", "下掉置顶"]):
                return True
            return bool(re.search(r"(?<![a-z])(unpin|pinned)(?![a-z])", low))
        if any(k in norm for k in ["置顶", "顶置"]):
            return True
        return bool(re.search(r"(?<![a-z])(pin|top)(?![a-z])", low))

    def _build_ocr_click_candidates(self, rect, ocr, fallback_center=None):
        """
        基于 OCR 目标框生成多个候选点击点，降低一次点击偏移导致失败的概率。
        返回: [{"vx": int, "vy": int, "label": str}, ...]
        """
        pts = []
        x1 = y1 = x2 = y2 = None
        if isinstance(rect, dict):
            try:
                x1 = float(rect.get("x1"))
                y1 = float(rect.get("y1"))
                x2 = float(rect.get("x2"))
                y2 = float(rect.get("y2"))
            except Exception:
                x1 = y1 = x2 = y2 = None

        if all(v is not None for v in [x1, y1, x2, y2]) and x2 > x1 and y2 > y1:
            w = x2 - x1
            h = y2 - y1
            seeds = [
                (0.50, 0.50, "center"),
                (0.35, 0.50, "left-center"),
                (0.65, 0.50, "right-center"),
                (0.50, 0.35, "upper-center"),
                (0.50, 0.65, "lower-center"),
            ]
            for rx, ry, label in seeds:
                ox = x1 + w * rx
                oy = y1 + h * ry
                vp = self._map_ocr_point_to_viewport(ox, oy, ocr)
                if not vp:
                    continue
                pts.append({"vx": int(vp[0]), "vy": int(vp[1]), "label": label})

        if fallback_center:
            vp = self._map_ocr_point_to_viewport(float(fallback_center[0]), float(fallback_center[1]), ocr)
            if vp:
                pts.insert(0, {"vx": int(vp[0]), "vy": int(vp[1]), "label": "fallback-center"})

        dedup = []
        seen = set()
        for p in pts:
            key = (int(round((p["vx"] or 0) / 3.0)), int(round((p["vy"] or 0) / 3.0)))
            if key in seen:
                continue
            seen.add(key)
            dedup.append(p)
        return dedup[:6]

    def _extract_rect_center(self, rect):
        if not isinstance(rect, dict):
            return None
        try:
            x1 = float(rect.get("x1"))
            y1 = float(rect.get("y1"))
            x2 = float(rect.get("x2"))
            y2 = float(rect.get("y2"))
            return (x1 + x2) / 2.0, (y1 + y2) / 2.0
        except Exception:
            return None

    def _normalize_link_index(self, link_index):
        try:
            idx = int(link_index)
        except Exception:
            return None
        return idx if idx > 0 else None

    def _pick_primary_block_rect(self, ocr, target_role):
        role = str(target_role or "").strip().lower()
        if not role:
            return None
        best = None
        best_score = -1.0
        for b in list((ocr or {}).get("blocks") or []):
            if not isinstance(b, dict):
                continue
            if str((b or {}).get("role") or "").strip().lower() != role:
                continue
            rect = (b or {}).get("rect") or {}
            if not isinstance(rect, dict):
                continue
            try:
                w = max(0.0, float(rect.get("x2") or 0.0) - float(rect.get("x1") or 0.0))
                h = max(0.0, float(rect.get("y2") or 0.0) - float(rect.get("y1") or 0.0))
            except Exception:
                continue
            if w <= 2 or h <= 2:
                continue
            score = float((b or {}).get("role_confidence") or 0.0) + (w * h) / 1000000.0
            if score > best_score:
                best_score = score
                best = rect
        return dict(best or {}) if isinstance(best, dict) else None

    def _extract_link_index_from_line(self, text, rect=None, product_panel_rect=None):
        raw = str(text or "").strip()
        if not raw:
            return 0
        if self._is_command_like_text(raw):
            return 0

        strict_patterns = [
            r"(?:序号|第)\s*([0-9]{1,3})(?![0-9])",
            r"([0-9]{1,3})\s*号\s*(?:链接|连接|商品|橱窗)",
            r"(?:link|item|product)\s*(?:no\.?|number)?\s*#?\s*([0-9]{1,3})(?![0-9])",
            r"#\s*([0-9]{1,3})(?![0-9])",
        ]
        for pat in strict_patterns:
            m = re.search(pat, raw, re.IGNORECASE)
            if not m:
                continue
            try:
                idx = int(m.group(1))
            except Exception:
                idx = 0
            if 1 <= idx <= 300:
                return idx

        # 仅在商品面板左侧小数字 badge 场景，放行纯数字文本。
        m_num = re.fullmatch(r"\s*([0-9]{1,3})\s*", raw)
        if not m_num:
            return 0
        try:
            idx = int(m_num.group(1))
        except Exception:
            idx = 0
        if idx < 1 or idx > 300:
            return 0
        if not (isinstance(rect, dict) and isinstance(product_panel_rect, dict)):
            return 0
        try:
            x1 = float(rect.get("x1") or 0.0)
            x2 = float(rect.get("x2") or 0.0)
            y1 = float(rect.get("y1") or 0.0)
            y2 = float(rect.get("y2") or 0.0)
            w = max(0.0, x2 - x1)
            h = max(0.0, y2 - y1)
            px1 = float(product_panel_rect.get("x1") or 0.0)
            px2 = float(product_panel_rect.get("x2") or 0.0)
            pw = max(0.0, px2 - px1)
            if pw <= 2.0:
                return 0
            center_x = (x1 + x2) / 2.0
            in_left_band = center_x <= (px1 + pw * 0.33)
            looks_badge = w <= max(84.0, pw * 0.18) and h <= 68.0
            if in_left_band and looks_badge:
                return idx
        except Exception:
            return 0
        return 0

    def _collect_visible_link_index_hits(self, ocr):
        lines = list((ocr or {}).get("lines") or [])
        if not lines:
            return []
        product_panel = self._pick_primary_block_rect(ocr or {}, "product_panel") or {}
        main_panel = self._pick_primary_block_rect(ocr or {}, "main_content") or {}
        image_w = float((ocr or {}).get("image_w") or 0.0)
        image_h = float((ocr or {}).get("image_h") or 0.0)

        def _in_rect(line_rect, panel_rect):
            if not (isinstance(line_rect, dict) and isinstance(panel_rect, dict) and panel_rect):
                return False
            try:
                lx1 = float(line_rect.get("x1") or 0.0)
                ly1 = float(line_rect.get("y1") or 0.0)
                lx2 = float(line_rect.get("x2") or 0.0)
                ly2 = float(line_rect.get("y2") or 0.0)
                px1 = float(panel_rect.get("x1") or 0.0)
                py1 = float(panel_rect.get("y1") or 0.0)
                px2 = float(panel_rect.get("x2") or 0.0)
                py2 = float(panel_rect.get("y2") or 0.0)
                ix1 = max(lx1, px1)
                iy1 = max(ly1, py1)
                ix2 = min(lx2, px2)
                iy2 = min(ly2, py2)
                return (ix2 - ix1) > 2.0 and (iy2 - iy1) > 2.0
            except Exception:
                return False

        hits = []
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            raw = str((ln or {}).get("text") or "").strip()
            rect = (ln or {}).get("rect") or {}
            if not raw or (not isinstance(rect, dict)):
                continue
            if self._is_streamlit_panel_noise_text(raw) or self._is_browser_chrome_noise_text(raw):
                continue
            if self._is_command_like_text(raw):
                continue

            idx = self._extract_link_index_from_line(raw, rect=rect, product_panel_rect=product_panel)
            if not idx:
                m_num = re.fullmatch(r"\s*([0-9]{1,3})\s*", raw)
                if m_num:
                    try:
                        idx_try = int(m_num.group(1))
                    except Exception:
                        idx_try = 0
                    if 1 <= idx_try <= 300:
                        center_try = self._extract_rect_center(rect)
                        if center_try:
                            try:
                                x1 = float(rect.get("x1") or 0.0)
                                x2 = float(rect.get("x2") or 0.0)
                                y1 = float(rect.get("y1") or 0.0)
                                y2 = float(rect.get("y2") or 0.0)
                                w = max(0.0, x2 - x1)
                                h = max(0.0, y2 - y1)
                            except Exception:
                                w = h = 0.0
                            in_product = _in_rect(rect, product_panel) if product_panel else False
                            in_main = _in_rect(rect, main_panel) if main_panel else True
                            left_cap = image_w * 0.46 if image_w > 2 else 1200.0
                            top_cap = image_h * 0.15 if image_h > 2 else 0.0
                            bottom_cap = image_h * 0.93 if image_h > 2 else 99999.0
                            likely_badge = (
                                center_try[0] <= left_cap
                                and top_cap <= center_try[1] <= bottom_cap
                                and w <= max(110.0, image_w * 0.08 if image_w > 2 else 110.0)
                                and h <= max(84.0, image_h * 0.08 if image_h > 2 else 84.0)
                            )
                            if likely_badge and (in_product or in_main):
                                idx = idx_try
            if not idx:
                continue
            center = self._extract_rect_center(rect)
            if not center:
                continue
            try:
                cx = float(center[0])
                cy = float(center[1])
            except Exception:
                continue
            # 仅保留左侧商品列表区域，避免右侧“活动日志 2号链接”污染可见序号带。
            if image_w > 2 and (cx < image_w * 0.04 or cx > image_w * 0.68):
                continue
            if image_h > 2 and (cy < image_h * 0.10 or cy > image_h * 0.94):
                continue
            if product_panel:
                try:
                    px1 = float(product_panel.get("x1") or 0.0)
                    px2 = float(product_panel.get("x2") or 0.0)
                    pw = max(0.0, px2 - px1)
                    if pw > 6:
                        # 序号带与商品行文本都在左侧，右半区多为操作按钮和其他面板文本。
                        right_gate = px1 + pw * 0.82
                        if cx > right_gate:
                            continue
                except Exception:
                    pass
            hits.append(
                {
                    "idx": int(idx),
                    "cx": float(cx),
                    "cy": float(cy),
                    "text": raw[:64],
                    "rect": dict(rect or {}),
                    "in_product": bool(_in_rect(rect, product_panel)) if product_panel else False,
                    "in_main": bool(_in_rect(rect, main_panel)) if main_panel else False,
                }
            )
        if not hits:
            return []
        hits.sort(key=lambda x: (float(x.get("cy") or 0.0), int(x.get("idx") or 0)))
        return hits

    def _infer_visible_link_index_band(self, ocr):
        hits = self._collect_visible_link_index_hits(ocr or {})
        if not hits:
            return {}
        # 进一步按“最左序号列”聚类，去除偶发落入中右区域的误识别文本。
        image_w = float((ocr or {}).get("image_w") or 0.0)
        try:
            min_x = min(float(x.get("cx") or 0.0) for x in hits)
        except Exception:
            min_x = 0.0
        left_span = max(180.0, image_w * 0.13 if image_w > 2 else 220.0)
        left_cluster = [x for x in hits if float(x.get("cx") or 0.0) <= (min_x + left_span)]
        if len(left_cluster) >= 2:
            hits = left_cluster
        values = [int(x["idx"]) for x in hits]
        return {
            "min": int(min(values)),
            "max": int(max(values)),
            "top": int(hits[0]["idx"]),
            "bottom": int(hits[-1]["idx"]),
            "count": int(len(hits)),
            "sample": [f"{int(x['idx'])}:{x['text']}" for x in hits[:4]],
        }

    def _extract_pinned_link_index_hint(self, ocr):
        """
        从 OCR 文本中提取“当前已置顶序号”提示，用于可见序号带缺失时的滚动方向兜底。
        """
        text_blob = str((ocr or {}).get("text") or "")
        if text_blob:
            m = re.search(r"(?:已置顶|当前置顶|置顶中)\s*(?:第)?\s*([0-9]{1,3})\s*(?:号|#)?", text_blob, re.IGNORECASE)
            if m:
                try:
                    idx = int(m.group(1))
                except Exception:
                    idx = 0
                if 1 <= idx <= 300:
                    return idx
            m2 = re.search(
                r"(?:currently|now)?\s*(?:pinned|pinning)\s*(?:link|item|product)?\s*#?\s*([0-9]{1,3})",
                text_blob,
                re.IGNORECASE,
            )
            if m2:
                try:
                    idx = int(m2.group(1))
                except Exception:
                    idx = 0
                if 1 <= idx <= 300:
                    return idx
        for ln in list((ocr or {}).get("lines") or [])[:40]:
            if not isinstance(ln, dict):
                continue
            raw = str((ln or {}).get("text") or "").strip()
            if (not raw) or self._is_streamlit_panel_noise_text(raw):
                continue
            m = re.search(r"(?:已置顶|当前置顶|置顶中)\s*(?:第)?\s*([0-9]{1,3})\s*(?:号|#)?", raw, re.IGNORECASE)
            if m:
                try:
                    idx = int(m.group(1))
                except Exception:
                    idx = 0
                if 1 <= idx <= 300:
                    return idx
        return 0

    def _build_anchor_from_visible_index_hits(self, ocr, target_idx):
        """
        目标序号已经进入可见区间但 OCR 行锚点缺失时，
        基于左右邻近序号插值出一条“行锚点”供固定行点击链路使用。
        """
        idx = self._normalize_link_index(target_idx)
        if not idx:
            return None
        hits = self._collect_visible_link_index_hits(ocr or {})
        if not hits:
            return None

        exact_hits = [h for h in hits if int(h.get("idx") or 0) == int(idx)]
        if exact_hits:
            hit = exact_hits[0]
            rect = dict(hit.get("rect") or {})
            if isinstance(rect, dict) and rect:
                return {
                    "text": str(idx),
                    "rect": rect,
                    "source": "visible_index_exact",
                }

        lower_hits = sorted(
            [h for h in hits if int(h.get("idx") or 0) < int(idx)],
            key=lambda h: int(h.get("idx") or 0),
            reverse=True,
        )
        upper_hits = sorted(
            [h for h in hits if int(h.get("idx") or 0) > int(idx)],
            key=lambda h: int(h.get("idx") or 0),
        )
        if (not lower_hits) or (not upper_hits):
            return None
        lower = lower_hits[0]
        upper = upper_hits[0]
        lo = int(lower.get("idx") or 0)
        hi = int(upper.get("idx") or 0)
        if lo <= 0 or hi <= 0 or hi <= lo:
            return None
        if (hi - lo) > 4:
            return None
        try:
            ratio = (float(idx) - float(lo)) / max(1.0, float(hi - lo))
            lx = float(lower.get("cx") or 0.0)
            ly = float(lower.get("cy") or 0.0)
            ux = float(upper.get("cx") or 0.0)
            uy = float(upper.get("cy") or 0.0)
            cx = lx + (ux - lx) * ratio
            cy = ly + (uy - ly) * ratio

            lr = dict(lower.get("rect") or {})
            ur = dict(upper.get("rect") or {})
            lw = max(12.0, float(lr.get("x2") or 0.0) - float(lr.get("x1") or 0.0))
            lh = max(18.0, float(lr.get("y2") or 0.0) - float(lr.get("y1") or 0.0))
            uw = max(12.0, float(ur.get("x2") or 0.0) - float(ur.get("x1") or 0.0))
            uh = max(18.0, float(ur.get("y2") or 0.0) - float(ur.get("y1") or 0.0))
            rw = max(12.0, (lw + uw) / 2.0)
            rh = max(18.0, (lh + uh) / 2.0)
            rect = {
                "x1": float(cx - rw / 2.0),
                "y1": float(cy - rh / 2.0),
                "x2": float(cx + rw / 2.0),
                "y2": float(cy + rh / 2.0),
            }
            return {
                "text": str(idx),
                "rect": rect,
                "source": "visible_index_interpolate",
                "neighbors": {"lower": lo, "upper": hi},
            }
        except Exception:
            return None

    def _infer_pin_button_column_x(self, action, ocr, row_anchor=None):
        if action not in {"pin_product", "unpin_product"}:
            return None, ""
        lines = list((ocr or {}).get("lines") or [])
        if not lines:
            return None, ""
        product_panel = self._pick_primary_block_rect(ocr or {}, "product_panel") or {}
        main_panel = self._pick_primary_block_rect(ocr or {}, "main_content") or {}

        def _in_rect(line_rect, panel_rect):
            if not (isinstance(line_rect, dict) and isinstance(panel_rect, dict) and panel_rect):
                return False
            try:
                lx1 = float(line_rect.get("x1") or 0.0)
                ly1 = float(line_rect.get("y1") or 0.0)
                lx2 = float(line_rect.get("x2") or 0.0)
                ly2 = float(line_rect.get("y2") or 0.0)
                px1 = float(panel_rect.get("x1") or 0.0)
                py1 = float(panel_rect.get("y1") or 0.0)
                px2 = float(panel_rect.get("x2") or 0.0)
                py2 = float(panel_rect.get("y2") or 0.0)
                ix1 = max(lx1, px1)
                iy1 = max(ly1, py1)
                ix2 = min(lx2, px2)
                iy2 = min(ly2, py2)
                return (ix2 - ix1) > 2.0 and (iy2 - iy1) > 2.0
            except Exception:
                return False

        row_center = self._extract_rect_center((row_anchor or {}).get("rect") or {}) if isinstance(row_anchor, dict) else None
        row_h = 0.0
        if isinstance(row_anchor, dict):
            try:
                r = (row_anchor or {}).get("rect") or {}
                row_h = max(0.0, float((r or {}).get("y2") or 0.0) - float((r or {}).get("y1") or 0.0))
            except Exception:
                row_h = 0.0
        near_row = []
        all_x = []
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            raw = str((ln or {}).get("text") or "").strip()
            rect = (ln or {}).get("rect") or {}
            if (not raw) or (not isinstance(rect, dict)):
                continue
            if self._is_streamlit_panel_noise_text(raw) or self._is_browser_chrome_noise_text(raw) or self._is_command_like_text(raw):
                continue
            if not self._match_pin_unpin_target_text(raw, action):
                continue
            if self._is_noisy_non_button_text(raw):
                continue
            if product_panel and (not _in_rect(rect, product_panel)):
                if main_panel and (not _in_rect(rect, main_panel)):
                    continue
            center = self._extract_rect_center(rect)
            if not center:
                continue
            all_x.append(float(center[0]))
            if row_center:
                y_gate = max(36.0, row_h * 2.2)
                if abs(float(center[1]) - float(row_center[1])) <= y_gate:
                    near_row.append(float(center[0]))

        sample = near_row if near_row else all_x
        if not sample:
            return None, ""
        sample.sort()
        mid = sample[len(sample) // 2]
        return float(mid), ("button_column_near_row" if near_row else "button_column_global")

    def _build_fixed_row_click_candidate(self, action, row_anchor, ocr, link_index=None):
        if (not self._ocr_pin_fixed_row_click_enabled) or action not in {"pin_product", "unpin_product"}:
            return None
        idx = self._normalize_link_index(link_index)
        if (not idx) or (not isinstance(row_anchor, dict)):
            return None
        row_rect = dict((row_anchor or {}).get("rect") or {})
        row_center = self._extract_rect_center(row_rect)
        if not row_center:
            return None
        row_h = 0.0
        try:
            row_h = max(0.0, float(row_rect.get("y2") or 0.0) - float(row_rect.get("y1") or 0.0))
        except Exception:
            row_h = 0.0

        img_w = float((ocr or {}).get("image_w") or 0.0)
        img_h = float((ocr or {}).get("image_h") or 0.0)
        x = None
        x_source = "unknown"
        panel_rect = self._pick_primary_block_rect(ocr, "product_panel")
        btn_x, btn_source = self._infer_pin_button_column_x(action, ocr, row_anchor=row_anchor)
        if btn_x is not None:
            x = float(btn_x)
            x_source = str(btn_source or "button_column")

        ratio = float(self._ocr_pin_fixed_row_click_x_ratio or 0.0)
        if (x is None) and img_w > 2 and 0.05 <= ratio <= 0.95:
            x = img_w * ratio
            x_source = "x_ratio"

        if (x is None) and panel_rect:
            try:
                panel_right = float(panel_rect.get("x2") or 0.0)
                panel_left = float(panel_rect.get("x1") or 0.0)
                panel_w = max(0.0, panel_right - panel_left)
                panel_x_ratio = float(self._ocr_pin_fixed_row_click_panel_x_ratio or 0.0)
                if panel_w > 2 and 0.55 <= panel_x_ratio <= 0.98:
                    x = panel_left + panel_w * panel_x_ratio
                    x_source = "product_panel_x_ratio"
                elif panel_right > 2:
                    pad_px = float(self._ocr_pin_fixed_row_click_right_padding_px)
                    pad_ratio = float(self._ocr_pin_fixed_row_click_right_padding_ratio or 0.0)
                    if panel_w > 2 and 0.0 < pad_ratio <= 0.45:
                        pad_px = max(pad_px, panel_w * pad_ratio)
                    x = panel_right - pad_px
                    x_source = "product_panel_right_padding"
            except Exception:
                x = None

        if x is None:
            base_x = float(row_center[0])
            rel_delta = img_w * float(self._ocr_pin_fixed_row_click_offset_x_ratio or 0.0) if img_w > 2 else 0.0
            abs_delta = float(self._ocr_pin_fixed_row_click_offset_x_px or 0.0)
            delta = max(abs_delta, rel_delta)
            if delta <= 1.0:
                delta = 320.0
            x = base_x + delta
            x_source = "row_anchor_offset"

        y_ratio = float(self._ocr_pin_fixed_row_click_offset_y_ratio or 0.0)
        y_delta_ratio = 0.0
        if row_h > 2.0 and -0.6 <= y_ratio <= 0.6:
            y_delta_ratio = row_h * y_ratio
        y = float(row_center[1]) + float(self._ocr_pin_fixed_row_click_offset_y_px or 0.0) + float(y_delta_ratio)
        if img_w > 2:
            x = max(1.0, min(img_w - 2.0, float(x)))
        if img_h > 2:
            y = max(1.0, min(img_h - 2.0, float(y)))

        vp = self._map_ocr_point_to_viewport(float(x), float(y), ocr or {})
        if not vp:
            return None

        return {
            "vx": int(vp[0]),
            "vy": int(vp[1]),
            "label": "fixed-row-relative",
            "fixed_mode": True,
            "fixed_meta": {
                "action": str(action or ""),
                "link_index": int(idx),
                "row_center": {"x": int(round(row_center[0])), "y": int(round(row_center[1]))},
                "target_ocr_point": {"x": int(round(float(x))), "y": int(round(float(y)))},
                "x_source": x_source,
                "row_rect": self._compact_rect(row_rect),
                "row_h": int(round(row_h)),
                "panel_rect": self._compact_rect(panel_rect or {}),
            },
        }

    def _show_click_test_popup(self, message, level="info", duration_ms=1800):
        """点击测试提示：使用小尺寸页面 toast，避免阻塞执行链路。"""
        msg = str(message or "").strip()
        if not msg:
            return True, {"skipped": True}
        tone = str(level or "info").strip().lower()
        if tone not in {"info", "success", "error"}:
            tone = "info"
        ttl = max(800, min(12000, int(duration_ms or 1800)))
        contexts = self._ordered_contexts()
        if not contexts:
            return False, {"reason": "no_page_context"}
        script = """
        const msg = String(arguments[0] || '');
        const level = String(arguments[1] || 'info');
        const ttl = Math.max(800, Math.min(12000, Number(arguments[2] || 1800)));
        try {
          const old = document.getElementById('__ocr_click_test_toast__');
          if (old && old.remove) old.remove();
          const palette = {
            info: { bg: 'rgba(17,24,39,0.92)', fg: '#f8fafc' },
            success: { bg: 'rgba(21,128,61,0.95)', fg: '#f0fdf4' },
            error: { bg: 'rgba(185,28,28,0.95)', fg: '#fef2f2' },
          };
          const c = palette[level] || palette.info;
          const box = document.createElement('div');
          box.id = '__ocr_click_test_toast__';
          box.textContent = msg;
          box.style.position = 'fixed';
          box.style.right = '14px';
          box.style.bottom = '14px';
          box.style.maxWidth = '260px';
          box.style.padding = '8px 10px';
          box.style.borderRadius = '8px';
          box.style.background = c.bg;
          box.style.color = c.fg;
          box.style.font = '12px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif';
          box.style.boxShadow = '0 6px 16px rgba(0,0,0,.22)';
          box.style.zIndex = '2147483647';
          box.style.opacity = '1';
          box.style.transform = 'translateY(0)';
          box.style.transition = 'opacity .18s ease, transform .18s ease';
          (document.body || document.documentElement).appendChild(box);
          setTimeout(() => {
            box.style.opacity = '0';
            box.style.transform = 'translateY(6px)';
          }, Math.max(300, ttl - 220));
          setTimeout(() => {
            if (box && box.remove) box.remove();
          }, ttl);
          return {ok:true};
        } catch (e) {
          return {ok:false, reason:String(e)};
        }
        """
        last_err = ""
        for ctx_name, ctx in contexts[:3]:
            try:
                result = ctx.run_js(script, msg, tone, int(ttl), timeout=1.2)
                if isinstance(result, dict) and not bool(result.get("ok")):
                    last_err = str(result.get("reason") or "toast_inject_failed")
                    continue
                return True, {"ctx": str(ctx_name or "page"), "level": tone}
            except Exception as e:
                last_err = str(e)
                continue
        return False, {"reason": last_err or "popup_failed", "level": tone}

    def _build_click_test_notice(self, clicked_ok, elapsed_ms=0, max_wait_ms=0, reason=""):
        if clicked_ok:
            return "已点击该位置"
        base_reason = str(reason or "unknown")
        if int(max_wait_ms or 0) > 0 and int(elapsed_ms or 0) > 0:
            return f"点击失败：耗时{int(elapsed_ms)}ms超过{int(max_wait_ms)}ms，原因：{base_reason}"
        return f"点击失败：{base_reason}"

    def _compact_rect(self, rect):
        if not isinstance(rect, dict):
            return {}
        out = {}
        for k in ("x1", "y1", "x2", "y2"):
            try:
                out[k] = int(round(float(rect.get(k) or 0.0)))
            except Exception:
                out[k] = 0
        return out

    def _append_fixed_row_click_log(self, payload):
        if not self._ocr_pin_fixed_row_calibration_log_enabled:
            return
        path = Path(self._ocr_pin_fixed_row_calibration_log_path or "data/reports/pin_click_calibration.jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入固定点位校准日志失败: {e}")

    def _record_fixed_row_click_meta(self, action, link_index, ocr, anchor, line, candidate, click_results, verified, reason):
        candidate = dict(candidate or {})
        fixed_meta = dict(candidate.get("fixed_meta") or {})
        compact_attempts = []
        for item in list(click_results or [])[:4]:
            click_obj = dict((item or {}).get("click") or {})
            compact_attempts.append(
                {
                    "attempt": int((item or {}).get("attempt") or 0),
                    "label": str((item or {}).get("label") or ""),
                    "point": dict((item or {}).get("point") or {}),
                    "driver": str(click_obj.get("driver") or ""),
                    "ok": bool(click_obj.get("ok")),
                    "reason": str(click_obj.get("reason") or ""),
                }
            )

        payload = {
            "ts": int(time.time() * 1000),
            "action": str(action or ""),
            "link_index": int(link_index or 0),
            "ok": bool(verified),
            "result_reason": str(reason or ""),
            "verified": dict(verified or {}),
            "ocr": {
                "provider": str((ocr or {}).get("provider") or ""),
                "page_type": str((ocr or {}).get("page_type") or ""),
                "coord_space": str((ocr or {}).get("coord_space") or ""),
                "image_w": int(float((ocr or {}).get("image_w") or 0.0)),
                "image_h": int(float((ocr or {}).get("image_h") or 0.0)),
                "view_w": int(float((ocr or {}).get("view_w") or 0.0)),
                "view_h": int(float((ocr or {}).get("view_h") or 0.0)),
                "scene_tags": list((ocr or {}).get("scene_tags") or [])[:12],
            },
            "line": {
                "text": str((line or {}).get("text") or "")[:80],
                "rect": self._compact_rect((line or {}).get("rect") or {}),
            },
            "anchor": {
                "text": str((anchor or {}).get("text") or "")[:80] if isinstance(anchor, dict) else "",
                "rect": self._compact_rect((anchor or {}).get("rect") or {}) if isinstance(anchor, dict) else {},
            },
            "target": {
                "viewport": {"x": int(candidate.get("vx") or 0), "y": int(candidate.get("vy") or 0)},
                "x_source": str(fixed_meta.get("x_source") or ""),
                "row_center": dict(fixed_meta.get("row_center") or {}),
                "target_ocr_point": dict(fixed_meta.get("target_ocr_point") or {}),
                "row_rect": self._compact_rect(fixed_meta.get("row_rect") or {}),
                "panel_rect": self._compact_rect(fixed_meta.get("panel_rect") or {}),
            },
            "attempts": compact_attempts,
        }
        self._last_fixed_row_click_meta = dict(payload)
        self._append_fixed_row_click_log(payload)

    def _map_ocr_point_to_viewport(self, x, y, ocr):
        coord_space = str((ocr or {}).get("coord_space") or "").strip().lower()
        if coord_space == "screen":
            left = float((ocr or {}).get("screen_left") or 0)
            top = float((ocr or {}).get("screen_top") or 0)
            return int(left + x), int(top + y)

        img_w = float(ocr.get("image_w") or 0)
        img_h = float(ocr.get("image_h") or 0)
        view_w = float(ocr.get("view_w") or img_w)
        view_h = float(ocr.get("view_h") or img_h)
        if img_w <= 0 or img_h <= 0 or view_w <= 0 or view_h <= 0:
            return None
        sx = view_w / img_w
        sy = view_h / img_h
        vx = int(max(1, min(view_w - 2, x * sx)))
        vy = int(max(1, min(view_h - 2, y * sy)))
        return vx, vy

    def _remember_click_result(self, driver="", point=None, error=""):
        self._last_click_driver = str(driver or "")
        self._last_click_error = str(error or "")
        if isinstance(point, dict):
            self._last_click_point = {
                "x": int(point.get("x") or 0),
                "y": int(point.get("y") or 0),
            }
        else:
            self._last_click_point = {}

    def _viewport_to_screen_point(self, vx, vy):
        script = """
        const borderX = Math.max(0, ((window.outerWidth || 0) - (window.innerWidth || 0)) / 2);
        const borderY = Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0) - borderX);
        const vv = window.visualViewport || {};
        return {
          screenX: Number(window.screenX ?? window.screenLeft ?? 0),
          screenY: Number(window.screenY ?? window.screenTop ?? 0),
          borderX: Number(borderX || 0),
          borderY: Number(borderY || 0),
          vvLeft: Number(vv.offsetLeft || 0),
          vvTop: Number(vv.offsetTop || 0),
          dpr: Number(window.devicePixelRatio || 1)
        };
        """
        try:
            _, info = self._run_js_in_contexts(script)
        except Exception:
            info = None
        if not isinstance(info, dict):
            return None
        try:
            sx = float(info.get("screenX") or 0.0)
            sy = float(info.get("screenY") or 0.0)
            vv_left = float(info.get("vvLeft") or 0.0)
            vv_top = float(info.get("vvTop") or 0.0)
            if self._viewport_screen_use_border_comp:
                sx += float(info.get("borderX") or 0.0)
                sy += float(info.get("borderY") or 0.0)
            base_x = sx + vv_left + float(vx)
            base_y = sy + vv_top + float(vy)
            abs_x = base_x * float(self._viewport_screen_scale) + float(self._viewport_screen_offset_x)
            abs_y = base_y * float(self._viewport_screen_scale) + float(self._viewport_screen_offset_y)
            return int(round(abs_x)), int(round(abs_y))
        except Exception:
            return None

    def _normalize_screen_click_point(self, x, y, ocr=None):
        """
        screen_ocr 模式下，修正高分屏/缩放导致的坐标系偏移。
        返回: (mx, my, meta)
        """
        mx = float(x)
        my = float(y)
        meta = {
            "input": {"x": int(round(mx)), "y": int(round(my))},
            "scaled": False,
            "clamped": False,
        }
        try:
            import pyautogui  # type: ignore

            size = pyautogui.size()
            sw = float(getattr(size, "width", 0) or 0)
            sh = float(getattr(size, "height", 0) or 0)
            meta["mouse_screen"] = {"w": int(sw), "h": int(sh)}

            cap_w = float((ocr or {}).get("screen_width") or (ocr or {}).get("image_w") or 0)
            cap_h = float((ocr or {}).get("screen_height") or (ocr or {}).get("image_h") or 0)
            left = float((ocr or {}).get("screen_left") or 0)
            top = float((ocr or {}).get("screen_top") or 0)
            meta["capture_rect"] = {"left": int(left), "top": int(top), "w": int(cap_w), "h": int(cap_h)}

            browser_dpr = 1.0
            inner_w = 0.0
            inner_h = 0.0
            try:
                js = """
                return {
                  dpr: Number(window.devicePixelRatio || 1),
                  innerW: Number(window.innerWidth || 0),
                  innerH: Number(window.innerHeight || 0)
                };
                """
                _, browser_info = self._run_js_in_contexts(js)
                if isinstance(browser_info, dict):
                    browser_dpr = max(1.0, float(browser_info.get("dpr") or 1.0))
                    inner_w = max(0.0, float(browser_info.get("innerW") or 0.0))
                    inner_h = max(0.0, float(browser_info.get("innerH") or 0.0))
            except Exception:
                pass
            meta["browser"] = {"dpr": round(float(browser_dpr), 3), "inner_w": int(inner_w), "inner_h": int(inner_h)}

            scaled_applied = False

            need_scale = cap_w > sw * 1.15 or cap_h > sh * 1.15
            if need_scale and cap_w > 1 and cap_h > 1:
                sx = sw / cap_w
                sy = sh / cap_h
                local_x = mx - left
                local_y = my - top
                # 偏移也按相同比例缩放，兼容 Retina 与多显示器缩放。
                mx = (left * sx) + (local_x * sx)
                my = (top * sy) + (local_y * sy)
                meta["scaled"] = True
                meta["scale"] = {"sx": round(sx, 4), "sy": round(sy, 4)}
                scaled_applied = True
            if (
                (not scaled_applied)
                and self._screen_ocr_mac_retina_half_scale_fallback
                and platform.system().lower() == "darwin"
                and browser_dpr >= 1.4
                and cap_w > 1
                and cap_h > 1
            ):
                # 优先使用浏览器 DPR 做映射，避免 pyautogui.size 在受限环境返回异常导致坐标不缩放。
                ratio = 1.0 / float(browser_dpr)
                local_x = mx - left
                local_y = my - top
                mx = left + (local_x * ratio)
                my = top + (local_y * ratio)
                meta["scaled"] = True
                meta["scale"] = {"sx": round(ratio, 4), "sy": round(ratio, 4), "fallback": "browser_dpr"}
                scaled_applied = True
            if (
                (not scaled_applied)
                and inner_w > 200
                and inner_h > 120
                and cap_w > (inner_w * 1.15)
                and cap_h > (inner_h * 1.15)
            ):
                # 兜底：直接按浏览器可视区与 OCR 画布比例缩放，规避 DPR 获取异常。
                sx = inner_w / cap_w
                sy = inner_h / cap_h
                local_x = mx - left
                local_y = my - top
                mx = left + (local_x * sx)
                my = top + (local_y * sy)
                meta["scaled"] = True
                meta["scale"] = {"sx": round(sx, 4), "sy": round(sy, 4), "fallback": "inner_ratio"}
                scaled_applied = True
            elif (
                (not scaled_applied)
                and self._screen_ocr_mac_retina_half_scale_fallback
                and platform.system().lower() == "darwin"
                and cap_w >= 2200
                and cap_h >= 1200
            ):
                # 某些 macOS Retina 环境中，采集分辨率与鼠标系同时返回“物理像素”，
                # 但 pyautogui 点击实际按“点坐标”解释，导致点击整体偏右下。
                ratio = float(self._screen_ocr_mac_retina_half_scale_ratio or 0.5)
                local_x = mx - left
                local_y = my - top
                mx = left + (local_x * ratio)
                my = top + (local_y * ratio)
                meta["scaled"] = True
                meta["scale"] = {"sx": round(ratio, 4), "sy": round(ratio, 4), "fallback": "mac_retina_half"}

            # 最终边界保护：避免落到系统自动夹紧后的右下角。
            cx = max(1.0, min(sw - 2.0, mx))
            cy = max(1.0, min(sh - 2.0, my))
            if abs(cx - mx) > 0.01 or abs(cy - my) > 0.01:
                meta["clamped"] = True
            mx, my = cx, cy
        except Exception as e:
            meta["error"] = str(e)
        return int(round(mx)), int(round(my)), meta

    def _click_viewport_point(self, vx, vy, ocr=None):
        if self._is_screen_ocr_info_mode():
            try:
                tx, ty, map_meta = self._normalize_screen_click_point(vx, vy, ocr=ocr)
                screen_sz = map_meta.get("mouse_screen") if isinstance(map_meta, dict) else {}
                sw = int((screen_sz or {}).get("w") or 0)
                sh = int((screen_sz or {}).get("h") or 0)
                dx = abs(float(tx) - float(vx))
                dy = abs(float(ty) - float(vy))
                if bool(map_meta.get("clamped")) and (dx > 220 or dy > 220):
                    logger.warning(
                        f"screen_ocr点击拦截: 映射偏移过大，in=({int(vx)},{int(vy)}) out=({int(tx)},{int(ty)}) "
                        f"delta=({int(dx)},{int(dy)})"
                    )
                    return {
                        "ok": False,
                        "ctx": "screen",
                        "driver": "physical_screen_blocked",
                        "reason": "mapped_point_large_delta",
                        "point": {"x": int(tx), "y": int(ty)},
                        "input_point": {"x": int(vx), "y": int(vy)},
                        "map_meta": map_meta,
                    }
                if sw > 10 and sh > 10 and bool(map_meta.get("clamped")):
                    near_edge = tx <= 4 or ty <= 4 or tx >= (sw - 4) or ty >= (sh - 4)
                    if near_edge:
                        logger.warning(
                            f"screen_ocr点击拦截: 映射后落在屏幕边缘，in=({int(vx)},{int(vy)}) out=({int(tx)},{int(ty)}) "
                            f"screen=({sw},{sh})"
                        )
                        return {
                            "ok": False,
                            "ctx": "screen",
                            "driver": "physical_screen_blocked",
                            "reason": "mapped_point_near_screen_edge",
                            "point": {"x": int(tx), "y": int(ty)},
                            "input_point": {"x": int(vx), "y": int(vy)},
                            "map_meta": map_meta,
                        }
                logger.info(
                    f"screen_ocr点击映射: in=({int(vx)},{int(vy)}) -> out=({int(tx)},{int(ty)}), "
                    f"scaled={bool(map_meta.get('scaled'))}, clamped={bool(map_meta.get('clamped'))}, "
                    f"scale={map_meta.get('scale')}, capture={map_meta.get('capture_rect')}, "
                    f"screen={map_meta.get('mouse_screen')}, browser={map_meta.get('browser')}"
                )
                self._human_action_delay("screen_click_pre")
                human_click(int(tx), int(ty), jitter_px=self._human_click_jitter)
                self._human_action_post_delay("screen_click_post")
                self._remember_click_result("physical_screen", {"x": int(tx), "y": int(ty)})
                return {
                    "ok": True,
                    "ctx": "screen",
                    "driver": "physical_screen",
                    "point": {"x": int(tx), "y": int(ty)},
                    "input_point": {"x": int(vx), "y": int(vy)},
                    "map_meta": map_meta,
                }
            except Exception as e:
                self._remember_click_result("physical_screen_failed", {"x": int(vx), "y": int(vy)}, str(e))
                return {
                    "ok": False,
                    "ctx": "screen",
                    "driver": "physical_screen_failed",
                    "reason": str(e),
                    "point": {"x": int(vx), "y": int(vy)},
                }

        if self._is_ocr_vision_mode() and (self._ocr_physical_click_enabled or self._force_full_physical_chain):
            screen_point = self._viewport_to_screen_point(vx, vy)
            if screen_point:
                sx, sy = screen_point
                try:
                    self._human_action_delay("ocr_physical_click_pre")
                    human_click(int(sx), int(sy), jitter_px=self._human_click_jitter)
                    self._human_action_post_delay("ocr_physical_click_post")
                    self._remember_click_result("physical_viewport", {"x": int(sx), "y": int(sy)})
                    return {
                        "ok": True,
                        "ctx": "screen_from_viewport",
                        "driver": "physical_viewport",
                        "point": {"x": int(sx), "y": int(sy)},
                        "viewport_point": {"x": int(vx), "y": int(vy)},
                    }
                except Exception as e:
                    logger.warning(f"OCR 物理鼠标点击失败，回退JS点击: {e}")
                    self._remember_click_result("physical_viewport_failed", {"x": int(sx), "y": int(sy)}, str(e))
            if self._force_full_physical_chain:
                fail_payload = {
                    "ok": False,
                    "reason": "physical_click_failed",
                    "point": {"x": int(vx), "y": int(vy)},
                    "driver": "physical_click_failed",
                }
                self._remember_click_result("physical_click_failed", fail_payload.get("point"), fail_payload.get("reason"))
                return fail_payload

        if not self._dom_fallback_enabled():
            fail_payload = {
                "ok": False,
                "reason": "dom_click_disabled",
                "point": {"x": int(vx), "y": int(vy)},
                "driver": "dom_click_disabled",
            }
            self._remember_click_result("dom_click_disabled", fail_payload.get("point"), fail_payload.get("reason"))
            return fail_payload

        script = """
        const x = Number(arguments[0] || 0);
        const y = Number(arguments[1] || 0);
        const el = document.elementFromPoint(x, y);
        if (!el) return {ok:false, reason:'no_element'};
        try { el.scrollIntoView && el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
        let clicked = false;
        try {
          if (typeof el.click === 'function') {
            el.click();
            clicked = true;
          }
        } catch(e) {}
        if (!clicked) {
          try {
            clicked = !!el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, clientX:x, clientY:y}));
          } catch(e) {}
        }
        if (!clicked) return {ok:false, reason:'click_failed'};
        return {ok:true, tag:(el.tagName||''), cls:(el.className||''), txt:String(el.innerText||el.textContent||'').slice(0,120)};
        """
        # 多上下文（page + iframe）下，优先返回首个成功点击，避免在首个上下文失败时提前结束。
        first_fail = None
        attempts = [
            (int(vx), int(vy)),
            (int(vx) + 14, int(vy)),
            (int(vx) - 14, int(vy)),
            (int(vx), int(vy) + 8),
            (int(vx), int(vy) - 8),
        ]

        for ax, ay in attempts:
            for ctx_name, ctx in self._iter_contexts():
                try:
                    result = ctx.run_js(script, ax, ay)
                except Exception:
                    continue
                if isinstance(result, dict):
                    result.setdefault("ctx", ctx_name)
                    result.setdefault("point", {"x": ax, "y": ay})
                    result.setdefault("driver", "js_click")
                    if result.get("ok"):
                        self._remember_click_result("js_click", result.get("point"))
                        return result
                    if first_fail is None:
                        first_fail = result
                elif result:
                    payload = {"ok": True, "ctx": ctx_name, "point": {"x": ax, "y": ay}, "raw": str(result)[:120], "driver": "js_click"}
                    self._remember_click_result("js_click", payload.get("point"))
                    return payload

        fail_payload = first_fail or {"ok": False, "reason": "js_no_result", "point": {"x": int(vx), "y": int(vy)}, "driver": "js_click_failed"}
        self._remember_click_result(
            fail_payload.get("driver") or "js_click_failed",
            fail_payload.get("point") if isinstance(fail_payload, dict) else None,
            fail_payload.get("reason") if isinstance(fail_payload, dict) else "js_no_result",
        )
        return fail_payload

    def _pick_ocr_target_line(self, action, lines, link_index=None, ocr=None, preferred_text="", preferred_role=""):
        if not lines:
            return None, None

        blocks = list((ocr or {}).get("blocks") or [])
        image_w = float((ocr or {}).get("image_w") or 0.0)
        image_h = float((ocr or {}).get("image_h") or 0.0)

        def _rect_intersection_area(a, b):
            try:
                x1 = max(float(a.get("x1", 0)), float(b.get("x1", 0)))
                y1 = max(float(a.get("y1", 0)), float(b.get("y1", 0)))
                x2 = min(float(a.get("x2", 0)), float(b.get("x2", 0)))
                y2 = min(float(a.get("y2", 0)), float(b.get("y2", 0)))
                return max(0.0, x2 - x1) * max(0.0, y2 - y1)
            except Exception:
                return 0.0

        chat_blocks = []
        metrics_blocks = []
        strict_action_blocks = []
        actionish_blocks = []
        product_blocks = []
        action_blocks = []
        main_blocks = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            rect = b.get("rect")
            if not isinstance(rect, dict):
                continue
            role = str((b or {}).get("role") or "")
            chat_score = float((b or {}).get("chat_score") or 0.0)
            if role == "chat_panel" or chat_score >= 0.7:
                chat_blocks.append(rect)
            if role == "metrics_panel":
                metrics_blocks.append(rect)
            if role in {"action_panel", "product_panel"}:
                strict_action_blocks.append(rect)
            if role in {"action_panel", "product_panel", "main_content"}:
                actionish_blocks.append(rect)
            if role == "product_panel":
                product_blocks.append(rect)
            if role == "action_panel":
                action_blocks.append(rect)
            if role == "main_content":
                main_blocks.append(rect)

        norm_lines = []
        for ln in lines:
            txt = str((ln or {}).get("text") or "").strip()
            rect = (ln or {}).get("rect")
            if not txt or not isinstance(rect, dict):
                continue
            if self._is_streamlit_panel_noise_text(txt):
                continue
            center = self._extract_rect_center(rect)
            if not center:
                continue
            in_chat = any(_rect_intersection_area(rect, cb) > 0 for cb in chat_blocks)
            in_metrics = any(_rect_intersection_area(rect, mb) > 0 for mb in metrics_blocks)
            in_strict_action = any(_rect_intersection_area(rect, ab) > 0 for ab in strict_action_blocks)
            in_actionish = any(_rect_intersection_area(rect, ab) > 0 for ab in actionish_blocks)
            in_product_panel = any(_rect_intersection_area(rect, ab) > 0 for ab in product_blocks)
            in_action_panel = any(_rect_intersection_area(rect, ab) > 0 for ab in action_blocks)
            in_main_content = any(_rect_intersection_area(rect, ab) > 0 for ab in main_blocks)
            norm_lines.append({
                "text": txt,
                "norm": self._norm_ocr_text(txt),
                "rect": rect,
                "cx": center[0],
                "cy": center[1],
                "in_chat": bool(in_chat),
                "in_metrics": bool(in_metrics),
                "in_strict_action": bool(in_strict_action),
                "in_actionish": bool(in_actionish),
                "in_product_panel": bool(in_product_panel),
                "in_action_panel": bool(in_action_panel),
                "in_main_content": bool(in_main_content),
            })
        if not norm_lines:
            return None, None
        product_panel_rect = self._pick_primary_block_rect(ocr or {}, "product_panel") or {}

        def _has_any(s, keys):
            return any(k in s for k in keys)
        def _is_row_anchor_text(raw_text, idx_value, rect=None):
            try:
                idx_num = int(idx_value or 0)
            except Exception:
                idx_num = 0
            if idx_num <= 0:
                return False
            raw = str(raw_text or "").strip()
            if not raw:
                return False
            if self._is_command_like_text(raw):
                return False
            hit_idx = self._extract_link_index_from_line(
                raw,
                rect=rect,
                product_panel_rect=product_panel_rect,
            )
            if int(hit_idx or 0) == idx_num:
                return True
            n = self._norm_ocr_text(raw)
            idx_s = str(idx_num)
            exact_forms = {
                idx_s,
                f"#{idx_s}",
                f"no{idx_s}",
                f"number{idx_s}",
                f"第{idx_s}",
                f"序号{idx_s}",
                f"{idx_s}号",
                f"{idx_s}号链接",
                f"{idx_s}号商品",
                f"link{idx_s}",
                f"item{idx_s}",
                f"product{idx_s}",
            }
            if n in exact_forms:
                return True
            if re.search(rf"(?:序号|第|link|item|product|#)\s*{idx_s}(?!\d)", raw, re.IGNORECASE):
                return True
            if re.search(rf"(?<!\d){idx_s}(?!\d)\s*(?:号|#)(?!\s*(?:链接|连接|商品|橱窗))", raw, re.IGNORECASE):
                return True
            return False

        preferred_norm = self._norm_ocr_text(preferred_text)
        preferred_role = str(preferred_role or "").strip().lower()
        role_flag_map = {
            "product_panel": "in_product_panel",
            "action_panel": "in_action_panel",
            "main_content": "in_main_content",
        }
        role_flag = role_flag_map.get(preferred_role, "")
        role_gate = bool(role_flag and any(bool(x.get(role_flag)) for x in norm_lines))

        if action in {"start_flash_sale", "stop_flash_sale"}:
            pri = []
            right_limit = image_w * 0.72 if image_w > 0 else 999999.0
            panel_x_candidates = []
            for rb in chat_blocks + metrics_blocks:
                try:
                    panel_x_candidates.append(float(rb.get("x1") or 0))
                except Exception:
                    continue
            if panel_x_candidates:
                right_limit = min(right_limit, min(panel_x_candidates) - 6.0)
            for ln in norm_lines:
                if ln.get("in_chat"):
                    continue
                if ln.get("in_metrics"):
                    continue
                if role_gate and (not ln.get(role_flag)):
                    continue
                s = ln["norm"]
                raw_text = ln["text"]
                match = self._match_flash_action_text(s, action)
                if not match.get("ok"):
                    continue

                if image_w > 0 and ln["cx"] >= right_limit:
                    continue
                if image_h > 0 and ln["cy"] >= image_h * 0.88:
                    continue
                if image_h > 0 and ln["cy"] <= image_h * 0.12:
                    continue

                rect = ln.get("rect") or {}
                rw = max(0.0, float(rect.get("x2") or 0) - float(rect.get("x1") or 0))
                rh = max(0.0, float(rect.get("y2") or 0) - float(rect.get("y1") or 0))
                if rh <= 0 or (image_h > 0 and rh > image_h * 0.12):
                    continue
                if image_w > 0 and rw >= image_w * 0.46 and not match.get("explicit"):
                    continue
                if strict_action_blocks and not ln.get("in_strict_action") and not match.get("explicit"):
                    continue
                if self._is_noisy_non_button_text(raw_text) and not match.get("explicit"):
                    continue

                score = float(match.get("score") or 0.0)
                if ln.get("in_actionish"):
                    score += 2.0
                if 48.0 <= rw <= 340.0:
                    score += 1.0
                if 20.0 <= rh <= 90.0:
                    score += 0.6
                if len(raw_text) <= 16:
                    score += 0.8
                if preferred_norm and preferred_norm in s:
                    score += 3.0
                if score > 0:
                    pri.append((score, ln))
            if pri:
                pri.sort(key=lambda x: x[0], reverse=True)
                top_ln = pri[0][1]
                logger.info(
                    f"OCR目标候选: action={action}, text={str(top_ln.get('text') or '')[:40]}, "
                    f"center=({int(top_ln.get('cx') or 0)},{int(top_ln.get('cy') or 0)}), "
                    f"strict={bool(top_ln.get('in_strict_action'))}, right_limit={int(right_limit)}, score={pri[0][0]:.2f}"
                )
                return pri[0][1], None
            return None, None

        if action in {"pin_product", "unpin_product"}:
            row_anchor = None
            anchor_pool = norm_lines
            if role_gate:
                gated = [ln for ln in norm_lines if ln.get(role_flag)]
                if gated:
                    anchor_pool = gated
            actionish_anchor_pool = [
                ln for ln in anchor_pool
                if (ln.get("in_actionish") or ln.get("in_strict_action"))
                and (not self._is_browser_chrome_noise_text(ln.get("text") or ""))
            ]
            if actionish_anchor_pool:
                anchor_pool = actionish_anchor_pool
            idx = self._normalize_link_index(link_index)
            if idx:
                anchor_keys = [
                    f"序号{idx}", f"{idx}号链接", f"第{idx}", f"link{idx}", f"item{idx}", f"product{idx}",
                ]
                for ln in anchor_pool:
                    if self._is_command_like_text(ln.get("text") or ""):
                        continue
                    ext_idx = self._extract_link_index_from_line(
                        ln.get("text") or "",
                        rect=ln.get("rect") or {},
                        product_panel_rect=product_panel_rect,
                    )
                    if int(ext_idx or 0) == int(idx):
                        row_anchor = ln
                        break
                for ln in anchor_pool:
                    if row_anchor is not None:
                        break
                    if self._is_command_like_text(ln.get("text") or ""):
                        continue
                    if _has_any(ln["norm"], [self._norm_ocr_text(k) for k in anchor_keys]):
                        row_anchor = ln
                        break
                if not row_anchor:
                    for ln in anchor_pool:
                        if self._is_command_like_text(ln.get("text") or ""):
                            continue
                        if _is_row_anchor_text(ln.get("text") or "", idx, rect=ln.get("rect") or {}):
                            row_anchor = ln
                            break
                if not row_anchor:
                    for ln in norm_lines:
                        if self._is_browser_chrome_noise_text(ln.get("text") or ""):
                            continue
                        if self._is_command_like_text(ln.get("text") or ""):
                            continue
                        if _is_row_anchor_text(ln.get("text") or "", idx, rect=ln.get("rect") or {}):
                            row_anchor = ln
                            break
                if row_anchor is None:
                    return None, None

            candidates = []
            for ln in norm_lines:
                if ln.get("in_chat"):
                    continue
                if ln.get("in_metrics"):
                    continue
                if role_gate and (not ln.get(role_flag)):
                    continue
                raw_text = ln.get("text") or ""
                if not self._match_pin_unpin_target_text(raw_text, action):
                    continue
                if self._is_noisy_non_button_text(raw_text):
                    continue
                if strict_action_blocks and (not ln.get("in_strict_action")):
                    continue
                if (not strict_action_blocks) and (not ln.get("in_actionish")):
                    continue
                if row_anchor is not None:
                    dy = abs(ln["cy"] - row_anchor["cy"])
                    dx = ln["cx"] - row_anchor["cx"]
                    score = 100 - dy - (0 if dx > 0 else 60)
                else:
                    score = 10 + ln["cx"] * 0.01
                if ln.get("in_actionish"):
                    score += 1.0
                if preferred_norm and preferred_norm in ln["norm"]:
                    score += 6.0
                candidates.append((score, ln))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1], row_anchor
            return None, row_anchor

        return None, None

    def _pick_ocr_target_from_action_candidates(self, action, ocr, anchor=None, preferred_text="", preferred_role=""):
        cands = []
        preferred_norm = self._norm_ocr_text(preferred_text)
        preferred_role = str(preferred_role or "").strip().lower()
        img_w = float((ocr or {}).get("image_w") or 0.0)
        img_h = float((ocr or {}).get("image_h") or 0.0)
        role_gate = False
        if preferred_role:
            role_gate = any(
                str((c or {}).get("role") or "").strip().lower() == preferred_role
                for c in list((ocr or {}).get("action_candidates") or [])
            )

        for c in list((ocr or {}).get("action_candidates") or []):
            if str((c or {}).get("action") or "").strip() != str(action):
                continue
            if self._is_streamlit_panel_noise_text((c or {}).get("text") or ""):
                continue
            c_role = str((c or {}).get("role") or "").strip().lower()
            if role_gate and c_role != preferred_role:
                continue
            c_text_norm = self._norm_ocr_text((c or {}).get("text") or "")
            if action in {"start_flash_sale", "stop_flash_sale"}:
                # flash 操作禁止用粗粒度 block 候选，避免命中整行/整块导致点击偏右下。
                if str((c or {}).get("source") or "") == "block":
                    continue
                match = self._match_flash_action_text(c_text_norm, action)
                if not match.get("ok"):
                    continue
            rect = (c or {}).get("rect")
            center = self._extract_rect_center(rect)
            if not center:
                continue
            if action in {"start_flash_sale", "stop_flash_sale"}:
                if img_w > 0 and center[0] >= img_w * 0.72:
                    continue
                if img_h > 0 and center[1] >= img_h * 0.88:
                    continue
                if c_role in {"chat_panel", "metrics_panel"}:
                    continue
                if self._is_noisy_non_button_text((c or {}).get("text") or ""):
                    continue
            score = float((c or {}).get("score") or 0.4)
            if preferred_norm and preferred_norm in c_text_norm:
                score += 3.0
            if isinstance(anchor, dict):
                dy = abs(center[1] - float(anchor.get("cy") or center[1]))
                dx = center[0] - float(anchor.get("cx") or center[0])
                score += max(0.0, 120.0 - dy) / 80.0
                if dx > 0:
                    score += 0.9
            cands.append((score, c))
        if cands:
            cands.sort(key=lambda x: x[0], reverse=True)
            return cands[0][1]
        return None

    def _pick_ocr_target_with_fallback(self, action, ocr, link_index=None, preferred_text="", preferred_role=""):
        lines = list((ocr or {}).get("lines") or [])
        line, anchor = self._pick_ocr_target_line(
            action,
            lines,
            link_index=link_index,
            ocr=ocr,
            preferred_text=preferred_text,
            preferred_role=preferred_role,
        )
        if line:
            return line, anchor
        if action in {"pin_product", "unpin_product"} and self._normalize_link_index(link_index):
            # 固定行点击模式下，保留 row_anchor 供后续直接点行内固定坐标。
            return None, anchor
        line = self._pick_ocr_target_from_action_candidates(
            action,
            ocr=ocr,
            anchor=anchor,
            preferred_text=preferred_text,
            preferred_role=preferred_role,
        )
        return line, anchor

    def _build_ocr_scan_signature(self, ocr):
        text_norm = self._norm_ocr_text((ocr or {}).get("text") or "")[:220]
        line_norms = []
        for ln in list((ocr or {}).get("lines") or [])[:14]:
            if not isinstance(ln, dict):
                continue
            n = self._norm_ocr_text((ln or {}).get("text") or "")
            if n:
                line_norms.append(n[:42])
        blob = "|".join([text_norm] + line_norms)
        return blob[:1200]

    def _build_nav_ocr_digest(self, ocr):
        blocks = []
        for b in list((ocr or {}).get("blocks") or [])[:8]:
            if not isinstance(b, dict):
                continue
            blocks.append(
                {
                    "role": str((b or {}).get("role") or ""),
                    "text": str((b or {}).get("text") or "")[:100],
                }
            )
        return {
            "page_type": str((ocr or {}).get("page_type") or ""),
            "scene_tags": list((ocr or {}).get("scene_tags") or [])[:10],
            "line_preview": [
                str((ln or {}).get("text") or "")[:80]
                for ln in list((ocr or {}).get("lines") or [])[:14]
                if isinstance(ln, dict)
            ],
            "blocks": blocks,
            "text_preview": str((ocr or {}).get("text") or "")[:420],
        }

    def _llm_build_navigation_hint(self, action, link_index=None, preferred_text="", ocr=None, history=None):
        llm = self._navigator_llm()
        if llm is None:
            return {}
        if not self._allow_nav_llm_now():
            return {}
        payload = {
            "action": str(action or ""),
            "link_index": int(link_index or 0),
            "preferred_text": str(preferred_text or "")[:120],
            "ocr": self._build_nav_ocr_digest(ocr or {}),
            "history": list(history or [])[-5:],
            "allowed_region_roles": ["product_panel", "action_panel", "main_content", "unknown"],
            "allowed_scroll_direction": ["down", "up", "none"],
        }
        prompt = (
            "你是直播运营页面导航器。"
            "目标：在 OCR 文本里定位动作目标区域，并给出下一步滚动策略。"
            "请只输出 JSON，不要输出额外文本。"
            "JSON格式:"
            "{\"region_role\":\"product_panel|action_panel|main_content|unknown\","
            "\"keyword_hints\":[\"...\"],"
            "\"should_scroll\":true|false,"
            "\"scroll_direction\":\"down|up|none\","
            "\"confidence\":0~1,"
            "\"reason\":\"...\"}\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = llm.invoke(prompt)
            raw = str(getattr(response, "content", response) or "").strip()
            data = self._extract_json_payload(raw)
            if not isinstance(data, dict):
                return {}
            role = str(data.get("region_role") or "unknown").strip().lower()
            if role not in {"product_panel", "action_panel", "main_content", "unknown"}:
                role = "unknown"
            direction = str(data.get("scroll_direction") or "down").strip().lower()
            if direction not in {"down", "up", "none"}:
                direction = "down"
            hints = []
            for item in list(data.get("keyword_hints") or [])[:6]:
                s = str(item or "").strip()
                if s:
                    hints.append(s[:24])
            return {
                "region_role": role,
                "keyword_hints": hints,
                "should_scroll": bool(data.get("should_scroll", True)),
                "scroll_direction": direction,
                "confidence": float(data.get("confidence") or 0.0),
                "reason": str(data.get("reason") or "")[:160],
            }
        except Exception as e:
            logger.warning(f"LLM导航提示失败: {e}")
            return {}

    def _resolve_nav_region_rect(self, ocr, nav_hint):
        hint = dict(nav_hint or {})
        role = str(hint.get("region_role") or "").strip().lower()
        if role not in {"product_panel", "action_panel", "main_content"}:
            return None
        blocks = []
        for b in list((ocr or {}).get("blocks") or []):
            if not isinstance(b, dict):
                continue
            if str((b or {}).get("role") or "").strip().lower() != role:
                continue
            rect = (b or {}).get("rect")
            if not isinstance(rect, dict):
                continue
            try:
                area = max(0.0, float(rect.get("x2") or 0) - float(rect.get("x1") or 0)) * max(
                    0.0,
                    float(rect.get("y2") or 0) - float(rect.get("y1") or 0),
                )
            except Exception:
                area = 0.0
            blocks.append((area, rect))
        if not blocks:
            return None
        blocks.sort(key=lambda x: x[0], reverse=True)
        return dict(blocks[0][1] or {})

    def _scroll_operation_surface(self, direction="down", ocr=None, region_rect=None, scroll_pixels=None):
        direction = str(direction or "down").strip().lower()
        if direction not in {"down", "up"}:
            return {"ok": False, "reason": "invalid_direction", "direction": direction}

        delta_base = int(abs(scroll_pixels if scroll_pixels is not None else self._nav_scroll_pixels))
        delta = max(40, delta_base)
        if direction == "up":
            delta = -delta

        def _to_screen_anchor_from_ocr_point(ox, oy):
            vp = self._map_ocr_point_to_viewport(float(ox), float(oy), ocr or {})
            if not vp:
                return None
            if self._is_screen_ocr_info_mode():
                sx, sy, _ = self._normalize_screen_click_point(vp[0], vp[1], ocr=ocr or {})
                return int(sx), int(sy)
            return self._viewport_to_screen_point(vp[0], vp[1])

        screen_anchor = None
        anchor_source = ""
        if isinstance(region_rect, dict):
            try:
                rx1 = float(region_rect.get("x1") or 0.0)
                ry1 = float(region_rect.get("y1") or 0.0)
                rx2 = float(region_rect.get("x2") or 0.0)
                ry2 = float(region_rect.get("y2") or 0.0)
                rw = max(0.0, rx2 - rx1)
                rh = max(0.0, ry2 - ry1)
            except Exception:
                rw = rh = 0.0
                rx1 = ry1 = rx2 = ry2 = 0.0
            if rw > 8 and rh > 8:
                if rw >= 420 and rh >= 160:
                    ox = rx1 + rw * 0.32
                    oy = ry1 + rh * 0.72
                    anchor_source = "nav_region_left_lower"
                else:
                    ox = rx1 + rw * 0.50
                    oy = ry1 + rh * 0.55
                    anchor_source = "nav_region_center"
                sp = _to_screen_anchor_from_ocr_point(ox, oy)
                if isinstance(sp, tuple) and len(sp) >= 2:
                    screen_anchor = (int(sp[0]), int(sp[1]))
        if screen_anchor is None and self._is_screen_ocr_info_mode():
            panel_rect = self._pick_primary_block_rect(ocr or {}, "product_panel")
            if isinstance(panel_rect, dict) and panel_rect:
                try:
                    px1 = float(panel_rect.get("x1") or 0.0)
                    py1 = float(panel_rect.get("y1") or 0.0)
                    px2 = float(panel_rect.get("x2") or 0.0)
                    py2 = float(panel_rect.get("y2") or 0.0)
                    pw = max(0.0, px2 - px1)
                    ph = max(0.0, py2 - py1)
                except Exception:
                    pw = ph = 0.0
                    px1 = py1 = 0.0
                if pw > 12 and ph > 12:
                    ox = px1 + pw * 0.32
                    oy = py1 + ph * 0.72
                    sp = _to_screen_anchor_from_ocr_point(ox, oy)
                    if isinstance(sp, tuple) and len(sp) >= 2:
                        screen_anchor = (int(sp[0]), int(sp[1]))
                        anchor_source = "product_panel_left_lower"
        if screen_anchor is None and self._is_screen_ocr_info_mode():
            hits = self._collect_visible_link_index_hits(ocr or {})
            if hits:
                img_w = float((ocr or {}).get("image_w") or 0.0)
                img_h = float((ocr or {}).get("image_h") or 0.0)
                target_y = img_h * 0.70 if img_h > 2 else float(hits[len(hits) // 2].get("cy") or 0.0)
                chosen = min(hits, key=lambda h: abs(float(h.get("cy") or 0.0) - target_y))
                cx = float(chosen.get("cx") or 0.0)
                cy = float(chosen.get("cy") or 0.0)
                drift_x = max(160.0, min(420.0, img_w * 0.16 if img_w > 2 else 220.0))
                ox = cx + drift_x
                if img_w > 2:
                    ox = max(img_w * 0.18, min(img_w * 0.58, ox))
                oy = cy + 12.0
                if img_h > 2:
                    oy = max(img_h * 0.24, min(img_h * 0.90, oy))
                sp = _to_screen_anchor_from_ocr_point(ox, oy)
                if isinstance(sp, tuple) and len(sp) >= 2:
                    screen_anchor = (int(sp[0]), int(sp[1]))
                    anchor_source = "index_band_infer"
        if screen_anchor is None and self._is_screen_ocr_info_mode():
            try:
                left = int((ocr or {}).get("screen_left") or 0)
                top = int((ocr or {}).get("screen_top") or 0)
                sw = int((ocr or {}).get("screen_width") or (ocr or {}).get("image_w") or 0)
                sh = int((ocr or {}).get("screen_height") or (ocr or {}).get("image_h") or 0)
                if sw > 20 and sh > 20:
                    # 兜底优先落在左侧商品列表区域，避免滚轮落到视频/聊天面板导致不滚动。
                    sp = _to_screen_anchor_from_ocr_point(
                        float(left + sw * 0.32),
                        float(top + sh * 0.72),
                    )
                    if isinstance(sp, tuple) and len(sp) >= 2:
                        screen_anchor = (int(sp[0]), int(sp[1]))
                        anchor_source = "screen_left_panel_fallback"
            except Exception:
                screen_anchor = None
                anchor_source = ""

        wheel_units = -abs(int(delta))
        if direction == "up":
            wheel_units = abs(int(delta))
        try:
            self._human_action_delay("nav_scroll_pre")
            if isinstance(screen_anchor, tuple) and len(screen_anchor) >= 2:
                human_scroll(wheel_units, x=int(screen_anchor[0]), y=int(screen_anchor[1]))
            else:
                human_scroll(wheel_units)
            self._human_action_post_delay("nav_scroll_post")
            return {
                "ok": True,
                "driver": "physical_scroll",
                "direction": direction,
                "wheel_units": int(wheel_units),
                "delta": int(abs(delta)),
                "anchor": {"x": int(screen_anchor[0]), "y": int(screen_anchor[1])} if isinstance(screen_anchor, tuple) else {},
                "anchor_source": str(anchor_source or ""),
            }
        except Exception as e:
            return {
                "ok": False,
                "driver": "physical_scroll_failed",
                "direction": direction,
                "delta": int(abs(delta)),
                "reason": str(e),
            }

    def _resolve_ocr_target_with_navigation(self, action, link_index=None, preferred_text=""):
        ocr = self._ocr_extract_page_text(use_cache=False)
        nav_trace = []
        if (not list(ocr.get("lines") or [])) and (not list(ocr.get("action_candidates") or [])):
            return {"ok": False, "reason": "ocr_no_lines", "ocr": ocr, "line": None, "anchor": None, "nav_trace": nav_trace}

        nav_hint = {}
        llm_calls = 0
        stagnant_rounds = 0
        last_sig = self._build_ocr_scan_signature(ocr)
        is_pin_unpin = str(action or "").strip().lower() in {"pin_product", "unpin_product"}
        link_idx = self._normalize_link_index(link_index)
        force_scroll_for_index = bool(is_pin_unpin and link_idx)
        max_rounds = int(self._nav_max_scroll_rounds) if (self._nav_llm_enabled or force_scroll_for_index) else 0
        anchor_for_fixed_fallback = None
        in_range_rescan_budget = 1
        empty_band_rounds = 0
        last_non_empty_visible_band = {}
        effective_scroll_cooldown = float(self._nav_scroll_cooldown)
        if force_scroll_for_index:
            effective_scroll_cooldown = max(0.05, effective_scroll_cooldown * 0.55)

        for round_idx in range(max(0, max_rounds) + 1):
            role_hint = str((nav_hint or {}).get("region_role") or "").strip().lower()
            keyword_hints = [str(x or "").strip() for x in list((nav_hint or {}).get("keyword_hints") or []) if str(x or "").strip()]
            merged_preferred = " ".join([str(preferred_text or "")] + keyword_hints).strip()
            line, anchor = self._pick_ocr_target_with_fallback(
                action,
                ocr=ocr,
                link_index=link_index,
                preferred_text=merged_preferred,
                preferred_role=role_hint,
            )
            if isinstance(anchor, dict):
                anchor_for_fixed_fallback = anchor
            if line:
                self._last_nav_trace = {
                    "ok": True,
                    "round": int(round_idx),
                    "hint": dict(nav_hint or {}),
                    "trace": list(nav_trace or [])[-8:],
                }
                return {
                    "ok": True,
                    "reason": "target_found",
                    "ocr": ocr,
                    "line": line,
                    "anchor": anchor,
                    "nav_trace": nav_trace,
                    "nav_hint": nav_hint,
                }
            if (
                is_pin_unpin
                and link_idx
                and self._ocr_pin_fixed_row_click_enabled
                and isinstance(anchor, dict)
            ):
                self._last_nav_trace = {
                    "ok": True,
                    "round": int(round_idx),
                    "hint": dict(nav_hint or {}),
                    "trace": list(nav_trace or [])[-8:],
                    "fallback": "row_anchor_only",
                }
                return {
                    "ok": True,
                    "reason": "row_anchor_found",
                    "ocr": ocr,
                    "line": None,
                    "anchor": anchor,
                    "nav_trace": nav_trace,
                    "nav_hint": nav_hint,
                }

            if round_idx >= max_rounds:
                break

            if self._nav_llm_enabled and (not force_scroll_for_index) and llm_calls < self._nav_max_llm_calls:
                hint = self._llm_build_navigation_hint(
                    action=action,
                    link_index=link_index,
                    preferred_text=preferred_text,
                    ocr=ocr,
                    history=nav_trace,
                )
                if isinstance(hint, dict) and hint:
                    nav_hint = hint
                    llm_calls += 1
                    nav_trace.append(
                        {
                            "round": int(round_idx + 1),
                            "phase": "llm_hint",
                            "hint": dict(hint),
                        }
                    )

            visible_band = self._infer_visible_link_index_band(ocr) if force_scroll_for_index else {}
            direction = str((nav_hint or {}).get("scroll_direction") or "down").strip().lower()
            if direction not in {"down", "up"}:
                direction = "down"
            band_min = 0
            band_max = 0
            band_count = 0
            if force_scroll_for_index and isinstance(visible_band, dict) and visible_band:
                band_min = int(visible_band.get("min") or 0)
                band_max = int(visible_band.get("max") or 0)
                band_count = int(visible_band.get("count") or 0)
                empty_band_rounds = 0
                last_non_empty_visible_band = dict(visible_band)
                if band_max > 0 and int(link_idx or 0) > band_max:
                    direction = "down"
                elif band_min > 0 and int(link_idx or 0) < band_min:
                    direction = "up"
            elif force_scroll_for_index:
                empty_band_rounds += 1
            pinned_hint_idx = 0
            if force_scroll_for_index:
                if (not isinstance(visible_band, dict)) or (not visible_band) or band_count <= 0:
                    pinned_hint_idx = int(self._extract_pinned_link_index_hint(ocr) or 0)
                    if 1 <= pinned_hint_idx <= 300:
                        if int(link_idx or 0) < pinned_hint_idx:
                            direction = "up"
                        elif int(link_idx or 0) > pinned_hint_idx:
                            direction = "down"
                    elif isinstance(last_non_empty_visible_band, dict) and last_non_empty_visible_band:
                        try:
                            last_min = int(last_non_empty_visible_band.get("min") or 0)
                            last_max = int(last_non_empty_visible_band.get("max") or 0)
                            if last_max > 0 and int(link_idx or 0) > last_max:
                                direction = "down"
                            elif last_min > 0 and int(link_idx or 0) < last_min:
                                direction = "up"
                        except Exception:
                            pass
                    elif round_idx > 0 and stagnant_rounds >= 1:
                        # 可见带长期缺失时，避免始终单向滚动；翻转一次尝试恢复。
                        direction = "up" if direction == "down" else "down"
            target_in_visible_band = bool(
                force_scroll_for_index
                and band_count >= 1
                and band_min > 0
                and band_max >= band_min
                and int(link_idx or 0) >= band_min
                and int(link_idx or 0) <= band_max
            )
            if target_in_visible_band and (not isinstance(anchor, dict)):
                recovered_anchor = self._build_anchor_from_visible_index_hits(ocr, link_idx)
                if isinstance(recovered_anchor, dict):
                    self._last_nav_trace = {
                        "ok": True,
                        "round": int(round_idx),
                        "hint": dict(nav_hint or {}),
                        "trace": list(nav_trace or [])[-8:],
                        "fallback": str(recovered_anchor.get("source") or "visible_index_recover"),
                    }
                    return {
                        "ok": True,
                        "reason": str(recovered_anchor.get("source") or "row_anchor_found"),
                        "ocr": ocr,
                        "line": None,
                        "anchor": recovered_anchor,
                        "nav_trace": nav_trace,
                        "nav_hint": nav_hint,
                    }
                if in_range_rescan_budget > 0:
                    in_range_rescan_budget -= 1
                    nav_trace.append(
                        {
                            "round": int(round_idx + 1),
                            "phase": "in_range_rescan",
                            "visible_band": dict(visible_band or {}),
                            "target": int(link_idx or 0),
                        }
                    )
                    time.sleep(max(0.05, float(effective_scroll_cooldown) * 0.55))
                    ocr = self._ocr_extract_page_text(use_cache=False)
                    last_sig = self._build_ocr_scan_signature(ocr)
                    continue
            should_scroll = bool((nav_hint or {}).get("should_scroll", True))
            if force_scroll_for_index and round_idx < max_rounds:
                should_scroll = True
            if target_in_visible_band:
                should_scroll = False
            if (
                force_scroll_for_index
                and (not target_in_visible_band)
                and empty_band_rounds >= 2
                and int(pinned_hint_idx or 0) <= 0
                and (not isinstance(anchor, dict))
            ):
                should_scroll = False
                nav_trace.append(
                    {
                        "round": int(round_idx + 1),
                        "phase": "empty_band_fast_fail",
                        "target": int(link_idx or 0),
                        "empty_band_rounds": int(empty_band_rounds),
                    }
                )
            if force_scroll_for_index:
                logger.info(
                    f"OCR导航滚动决策: action={action}, target={int(link_idx or 0)}, "
                    f"round={int(round_idx + 1)}/{int(max_rounds + 1)}, direction={direction}, "
                    f"visible_band={dict(visible_band or {})}, pinned_hint={int(pinned_hint_idx or 0)}, "
                    f"stagnant={int(stagnant_rounds)}, should_scroll={bool(should_scroll)}"
                )
            if not should_scroll:
                break

            region_rect = self._resolve_nav_region_rect(ocr, nav_hint)
            if force_scroll_for_index and (not isinstance(region_rect, dict)):
                region_rect = self._pick_primary_block_rect(ocr or {}, "product_panel")
            if force_scroll_for_index and (not isinstance(region_rect, dict)):
                region_rect = self._pick_primary_block_rect(ocr or {}, "main_content")
            scroll_pixels = int(self._nav_scroll_pixels)
            if force_scroll_for_index:
                # 置顶序号导航采用“静态微步优先”，避免近距离越过目标。
                scroll_pixels = int(max(70, min(scroll_pixels, 180)))
                distance = 0
                if band_min > 0 and band_max >= band_min:
                    if int(link_idx or 0) > band_max:
                        distance = int(link_idx or 0) - band_max
                    elif int(link_idx or 0) < band_min:
                        distance = band_min - int(link_idx or 0)
                elif int(pinned_hint_idx or 0) > 0:
                    distance = abs(int(link_idx or 0) - int(pinned_hint_idx or 0))
                if distance > 0:
                    if distance <= 1:
                        scroll_pixels = min(scroll_pixels, 55)
                    elif distance <= 2:
                        scroll_pixels = min(scroll_pixels, 70)
                    elif distance <= 4:
                        scroll_pixels = min(scroll_pixels, 85)
                    elif distance <= 8:
                        scroll_pixels = min(scroll_pixels, 105)
                    else:
                        scroll_pixels = min(scroll_pixels, 125)
                if stagnant_rounds > 0:
                    # 卡住时只做小幅增量，不允许大步跳跃。
                    scroll_pixels = int(min(140, max(scroll_pixels, scroll_pixels + 10 * int(stagnant_rounds))))
                near_target = bool(distance > 0 and distance <= 2)
            scroll_res = self._scroll_operation_surface(
                direction=direction,
                ocr=ocr,
                region_rect=region_rect,
                scroll_pixels=scroll_pixels,
            )
            nav_trace.append(
                {
                    "round": int(round_idx + 1),
                    "phase": "scroll",
                    "direction": direction,
                    "region_role": str((nav_hint or {}).get("region_role") or ("product_panel" if force_scroll_for_index else "")),
                    "visible_band": dict(visible_band or {}),
                    "scroll": dict(scroll_res or {}),
                }
            )
            if not bool((scroll_res or {}).get("ok")):
                break

            time.sleep(float(effective_scroll_cooldown))
            ocr_next = self._ocr_extract_page_text(use_cache=False)
            next_sig = self._build_ocr_scan_signature(ocr_next)
            next_visible_band = self._infer_visible_link_index_band(ocr_next) if force_scroll_for_index else {}
            reversed_scroll = False
            if force_scroll_for_index and isinstance(visible_band, dict) and isinstance(next_visible_band, dict):
                try:
                    prev_min = int(visible_band.get("min") or 0)
                    prev_max = int(visible_band.get("max") or 0)
                    next_min = int(next_visible_band.get("min") or 0)
                    next_max = int(next_visible_band.get("max") or 0)
                    if prev_min > 0 and prev_max >= prev_min and next_min > 0 and next_max >= next_min:
                        prev_mid = (float(prev_min) + float(prev_max)) / 2.0
                        next_mid = (float(next_min) + float(next_max)) / 2.0
                        moved = next_mid - prev_mid
                        reversed_scroll = (
                            (direction == "up" and moved > 0.8)
                            or (direction == "down" and moved < -0.8)
                        )
                        if reversed_scroll:
                            nav_trace.append(
                                {
                                    "round": int(round_idx + 1),
                                    "phase": "scroll_direction_mismatch",
                                    "direction": direction,
                                    "visible_band_before": dict(visible_band or {}),
                                    "visible_band_after": dict(next_visible_band or {}),
                                    "moved": round(float(moved), 3),
                                }
                            )
                except Exception:
                    reversed_scroll = False

            if reversed_scroll:
                fix_direction = "down" if direction == "up" else "up"
                if force_scroll_for_index:
                    if near_target:
                        fix_pixels = int(max(40, min(80, scroll_pixels + 8)))
                    else:
                        fix_pixels = int(max(45, min(110, scroll_pixels + 18)))
                else:
                    fix_pixels = int(max(self._nav_scroll_pixels * 1.28, scroll_pixels + 120))
                fix_res = self._scroll_operation_surface(
                    direction=fix_direction,
                    ocr=ocr_next,
                    region_rect=region_rect,
                    scroll_pixels=fix_pixels,
                )
                nav_trace.append(
                    {
                        "round": int(round_idx + 1),
                        "phase": "scroll_direction_fix",
                        "direction": fix_direction,
                        "scroll": dict(fix_res or {}),
                    }
                )
                if bool((fix_res or {}).get("ok")):
                    time.sleep(max(0.05, float(effective_scroll_cooldown) * 0.85))
                    ocr_fix = self._ocr_extract_page_text(use_cache=False)
                    fix_sig = self._build_ocr_scan_signature(ocr_fix)
                    if fix_sig:
                        ocr_next = ocr_fix
                        next_sig = fix_sig
                    nav_hint = dict(nav_hint or {})
                    nav_hint["scroll_direction"] = fix_direction
            if next_sig and next_sig == last_sig:
                stagnant_rounds += 1
                if force_scroll_for_index and stagnant_rounds <= 3:
                    if near_target:
                        boost_pixels = int(max(45, min(90, scroll_pixels + 10)))
                    else:
                        boost_pixels = int(max(55, min(130, scroll_pixels + 22)))
                    boost_res = self._scroll_operation_surface(
                        direction=direction,
                        ocr=ocr,
                        region_rect=region_rect,
                        scroll_pixels=boost_pixels,
                    )
                    nav_trace.append(
                        {
                            "round": int(round_idx + 1),
                            "phase": "scroll_boost",
                            "direction": direction,
                            "visible_band": dict(visible_band or {}),
                            "scroll": dict(boost_res or {}),
                        }
                    )
                    if bool((boost_res or {}).get("ok")):
                        time.sleep(max(0.05, float(effective_scroll_cooldown) * 0.75))
                        ocr_boost = self._ocr_extract_page_text(use_cache=False)
                        boost_sig = self._build_ocr_scan_signature(ocr_boost)
                        if boost_sig and boost_sig != last_sig:
                            ocr_next = ocr_boost
                            next_sig = boost_sig
                            stagnant_rounds = 0
            else:
                stagnant_rounds = 0
            if stagnant_rounds >= 2:
                nav_hint = dict(nav_hint or {})
                nav_hint["scroll_direction"] = "up" if direction == "down" else "down"
            ocr = ocr_next
            last_sig = next_sig

        reason = "ocr_target_not_found_after_scroll" if nav_trace else "ocr_target_not_found"
        self._last_nav_trace = {
            "ok": False,
            "reason": reason,
            "hint": dict(nav_hint or {}),
            "trace": list(nav_trace or [])[-8:],
        }
        return {
            "ok": False,
            "reason": reason,
            "ocr": ocr,
            "line": None,
            "anchor": anchor_for_fixed_fallback if isinstance(anchor_for_fixed_fallback, dict) else None,
            "nav_trace": nav_trace,
            "nav_hint": nav_hint,
        }

    def _perform_action_by_ocr_anchor(self, action, link_index=None, preferred_text=""):
        is_pin_unpin = action in {"pin_product", "unpin_product"}
        link_idx = self._normalize_link_index(link_index)
        if is_pin_unpin and self._pin_unpin_require_link_index and (not link_idx):
            fail_payload = {
                "ts": int(time.time() * 1000),
                "action": str(action or ""),
                "link_index": 0,
                "ok": False,
                "result_reason": "link_index_required_for_fixed_row_click",
                "attempts": [],
            }
            self._last_fixed_row_click_meta = dict(fail_payload)
            self._append_fixed_row_click_log(fail_payload)
            return {
                "ok": False,
                "reason": "link_index_required_for_fixed_row_click",
                "ocr": {},
                "anchor": None,
                "nav_trace": [],
            }
        target = self._resolve_ocr_target_with_navigation(
            action,
            link_index=link_idx if link_idx else link_index,
            preferred_text=preferred_text,
        )
        ocr = dict(target.get("ocr") or {})
        line = target.get("line")
        anchor = target.get("anchor")
        nav_trace = list(target.get("nav_trace") or [])
        if is_pin_unpin:
            for retry_idx in range(2):
                page_type = self._resolve_ocr_page_type_with_ctx_fallback(ocr)
                operable = self._is_shop_dashboard_page_type(page_type)
                if operable:
                    break
                if retry_idx == 0 and self._is_screen_ocr_info_mode():
                    focused = self._focus_action_page_for_screen_ocr(action)
                    if focused:
                        target = self._resolve_ocr_target_with_navigation(
                            action,
                            link_index=link_idx if link_idx else link_index,
                            preferred_text=preferred_text,
                        )
                        ocr = dict(target.get("ocr") or {})
                        line = target.get("line")
                        anchor = target.get("anchor")
                        nav_trace = list(target.get("nav_trace") or [])
                        continue
                return {
                    "ok": False,
                    "reason": "non_operable_page_before_click_shop_dashboard_required",
                    "ocr": ocr,
                    "line": line,
                    "anchor": anchor,
                    "nav_trace": nav_trace,
                }
        fixed_candidate = self._build_fixed_row_click_candidate(action, anchor, ocr, link_index=link_idx)
        if not line and action in {"start_flash_sale", "stop_flash_sale"}:
            logger.warning(
                f"OCR秒杀按钮未定位: action={action}, page_type={ocr.get('page_type')}, "
                f"scene_tags={list(ocr.get('scene_tags') or [])[:6]}, "
                f"cand_count={len(list(ocr.get('action_candidates') or []))}"
            )
        if not line:
            if fixed_candidate:
                line = {
                    "text": f"row_anchor_{int(link_idx or 0)}",
                    "rect": dict((anchor or {}).get("rect") or {}),
                    "source": "row_anchor_fallback",
                }
                logger.info(
                    f"OCR行锚点兜底点击: action={action}, link_index={int(link_idx or 0)}, "
                    f"point=({int(fixed_candidate['vx'])},{int(fixed_candidate['vy'])})"
                )
            else:
                return {"ok": False, "reason": str(target.get("reason") or "ocr_target_not_found"), "ocr": ocr, "anchor": anchor, "nav_trace": nav_trace}
        candidates = []
        center = self._extract_rect_center(line.get("rect")) if isinstance(line, dict) else None
        if center:
            candidates = self._build_ocr_click_candidates(line.get("rect"), ocr, fallback_center=center)
        elif not fixed_candidate:
            return {"ok": False, "reason": "ocr_rect_invalid", "line": line}
        if is_pin_unpin and self._ocr_pin_fixed_row_click_enabled:
            if not fixed_candidate:
                fixed_candidate = self._build_fixed_row_click_candidate(action, anchor, ocr, link_index=link_idx)
            if fixed_candidate:
                candidates = [fixed_candidate]
                logger.info(
                    f"OCR固定行相对点击启用: action={action}, link_index={int(link_idx or 0)}, "
                    f"point=({int(fixed_candidate['vx'])},{int(fixed_candidate['vy'])}), "
                    f"source={str((fixed_candidate.get('fixed_meta') or {}).get('x_source') or '')}"
                )
            elif self._pin_unpin_force_fixed_row_click:
                miss_reason = "fixed_row_anchor_missing" if (not isinstance(anchor, dict)) else "fixed_row_candidate_unavailable"
                fail_payload = {
                    "ts": int(time.time() * 1000),
                    "action": str(action or ""),
                    "link_index": int(link_idx or 0),
                    "ok": False,
                    "result_reason": miss_reason,
                    "ocr": {
                        "page_type": str((ocr or {}).get("page_type") or ""),
                        "image_w": int(float((ocr or {}).get("image_w") or 0.0)),
                        "image_h": int(float((ocr or {}).get("image_h") or 0.0)),
                    },
                    "line": {
                        "text": str((line or {}).get("text") or "")[:80],
                        "rect": self._compact_rect((line or {}).get("rect") or {}),
                    },
                    "anchor": {
                        "text": str((anchor or {}).get("text") or "")[:80] if isinstance(anchor, dict) else "",
                        "rect": self._compact_rect((anchor or {}).get("rect") or {}) if isinstance(anchor, dict) else {},
                    },
                    "attempts": [],
                }
                self._last_fixed_row_click_meta = dict(fail_payload)
                self._append_fixed_row_click_log(fail_payload)
                return {
                    "ok": False,
                    "reason": miss_reason,
                    "ocr": ocr,
                    "line": line,
                    "anchor": anchor,
                    "nav_trace": nav_trace,
                }
            else:
                logger.warning(
                    f"OCR固定行相对点击未生效，将回退常规OCR候选: action={action}, link_index={int(link_idx or 0)}"
                )
        if not candidates:
            return {"ok": False, "reason": "ocr_viewport_map_failed", "line": line}

        logger.info(
            f"OCR锚点命中: action={action}, target={str(line.get('text') or '')[:36]}, "
            f"candidate_count={len(candidates)}, first=({int(candidates[0]['vx'])},{int(candidates[0]['vy'])})"
        )

        click_results = []
        verified = None
        ok = False
        ui_reaction = None
        baseline_snapshot = self._build_ocr_reaction_snapshot(ocr)
        long_wait_before_retry = self._is_screen_ocr_info_mode() and action in {"start_flash_sale", "stop_flash_sale"}
        max_attempts = 3 if self._is_screen_ocr_info_mode() else 2
        for attempt_idx, p in enumerate(candidates[:max_attempts]):
            logger.info(
                f"OCR点击尝试: action={action}, attempt={attempt_idx + 1}/{max_attempts}, "
                f"label={p.get('label')}, point=({int(p['vx'])},{int(p['vy'])})"
            )
            click_started_at = time.time()
            click_res = self._click_viewport_point(p["vx"], p["vy"], ocr=ocr)
            click_elapsed_ms = int(max(0.0, (time.time() - click_started_at) * 1000.0))
            if isinstance(click_res, dict):
                click_res.setdefault("elapsed_ms", int(click_elapsed_ms))

            if bool(p.get("fixed_mode")) and self._ocr_pin_click_test_confirm_popup:
                max_wait_ms = int(float(self._ocr_pin_click_test_max_wait_seconds or 0.0) * 1000.0)
                if bool((click_res or {}).get("ok")) and click_elapsed_ms <= max_wait_ms:
                    msg = self._build_click_test_notice(True)
                    toast_ok, toast_meta = self._show_click_test_popup(msg, level="success", duration_ms=1200)
                    if not toast_ok:
                        logger.warning(f"点击测试提示展示失败: {toast_meta}")
                elif click_elapsed_ms > max_wait_ms:
                    if bool((click_res or {}).get("ok")):
                        fail_reason = "click_ok_but_timeout"
                    else:
                        fail_reason = str((click_res or {}).get("reason") or (click_res or {}).get("driver") or "click_timeout")
                    msg = self._build_click_test_notice(
                        False,
                        elapsed_ms=click_elapsed_ms,
                        max_wait_ms=max_wait_ms,
                        reason=fail_reason,
                    )
                    toast_ok, toast_meta = self._show_click_test_popup(msg, level="error", duration_ms=3200)
                    if not toast_ok:
                        logger.warning(f"点击测试提示展示失败: {toast_meta}")

            click_results.append({
                "attempt": attempt_idx + 1,
                "label": p.get("label"),
                "point": {"x": int(p["vx"]), "y": int(p["vy"])},
                "click": click_res,
                "fixed_meta": dict(p.get("fixed_meta") or {}),
            })
            if not bool(click_res.get("ok")):
                continue
            ok = True

            if long_wait_before_retry and attempt_idx == 0:
                wait_feedback = self._wait_for_ocr_feedback_after_click(
                    action,
                    baseline_snapshot,
                    link_index=link_idx if link_idx else link_index,
                    timeout_seconds=self._ocr_retry_wait_seconds,
                )
                click_results[-1]["feedback_wait"] = wait_feedback
                verified = wait_feedback.get("verified")
                ui_reaction = wait_feedback.get("reaction")
                if verified:
                    break
                if ui_reaction:
                    logger.info(
                        f"OCR点击后检测到页面反应，暂不二次处理: action={action}, reaction={ui_reaction.get('source')}"
                    )
                    break
                if attempt_idx + 1 < max_attempts:
                    logger.warning(
                        f"OCR点击后{int(self._ocr_retry_wait_seconds)}秒未检测到反应，进入二次处理: action={action}"
                    )
            else:
                verified = self._verify_receipt_by_ocr(action, link_index=link_idx if link_idx else link_index)
                if verified:
                    break
            if attempt_idx + 1 < max_attempts:
                time.sleep(0.14)

        click_res = click_results[-1]["click"] if click_results else {"ok": False, "reason": "click_not_attempted"}
        detail = {
            "target_text": line.get("text"),
            "target_rect": line.get("rect"),
            "anchor_text": anchor.get("text") if isinstance(anchor, dict) else "",
            "viewport_point": {"x": candidates[0]["vx"], "y": candidates[0]["vy"]},
            "fixed_row_click_mode": bool(fixed_candidate),
            "click": click_res,
            "click_attempts": click_results,
            "ocr_verify": verified or {},
            "ui_reaction": ui_reaction or {},
            "ocr_provider": ocr.get("provider"),
            "ocr_ms": ocr.get("elapsed_ms"),
            "ocr_page_type": ocr.get("page_type"),
            "ocr_scene_tags": list(ocr.get("scene_tags") or []),
            "ocr_action_candidate_count": len(list(ocr.get("action_candidates") or [])),
            "navigation_trace": nav_trace,
        }
        result_reason = "clicked_verified" if verified else ("clicked" if ok else "click_failed")
        if fixed_candidate:
            self._record_fixed_row_click_meta(
                action=action,
                link_index=link_idx if link_idx else link_index,
                ocr=ocr,
                anchor=anchor,
                line=line,
                candidate=fixed_candidate,
                click_results=click_results,
                verified=verified or {},
                reason=result_reason,
            )
        return {"ok": ok, "reason": result_reason, "detail": detail}

    def _llm_judge_operable_page(self, action, ocr):
        if (not self._nav_llm_enabled) or (not self._nav_unknown_page_enabled):
            return {}
        llm = self._navigator_llm()
        if llm is None:
            return {}
        if not self._allow_nav_llm_now():
            return {}
        payload = {
            "action": str(action or ""),
            "ocr": self._build_nav_ocr_digest(ocr or {}),
            "operable_page_hints": {
                "pin_product": ["商品列表", "置顶", "取消置顶", "product", "pin", "unpin", "link"],
                "unpin_product": ["商品列表", "取消置顶", "unpin", "pinned", "link"],
                "start_flash_sale": ["秒杀", "上架", "活动", "flash", "promotion", "launch", "start"],
                "stop_flash_sale": ["结束秒杀", "停止秒杀", "下架", "flash", "promotion", "stop", "end"],
            }.get(str(action or "").strip().lower(), []),
        }
        prompt = (
            "你是直播运营页面判定器。"
            "请根据 OCR 文本判断当前页面是否可以执行给定动作。"
            "即使页面样式没见过，也要根据语义判断。"
            "仅输出 JSON，不要输出额外文本。"
            "JSON格式:"
            "{\"operable\":true|false,"
            "\"confidence\":0~1,"
            "\"reason\":\"...\","
            "\"target_region_role\":\"product_panel|action_panel|main_content|unknown\","
            "\"evidence\":[\"...\"]}\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = llm.invoke(prompt)
            raw = str(getattr(response, "content", response) or "").strip()
            data = self._extract_json_payload(raw)
            if not isinstance(data, dict):
                return {}
            role = str(data.get("target_region_role") or "unknown").strip().lower()
            if role not in {"product_panel", "action_panel", "main_content", "unknown"}:
                role = "unknown"
            return {
                "operable": bool(data.get("operable")),
                "confidence": float(data.get("confidence") or 0.0),
                "reason": str(data.get("reason") or "")[:180],
                "target_region_role": role,
                "evidence": [str(x or "")[:80] for x in list(data.get("evidence") or [])[:4] if str(x or "").strip()],
            }
        except Exception as e:
            logger.warning(f"LLM页面判定失败: {e}")
            return {}

    def _is_pin_unpin_action(self, action):
        return str(action or "").strip().lower() in {"pin_product", "unpin_product", "repin_product"}

    def _is_shop_dashboard_page_type(self, page_type):
        return str(page_type or "").strip().lower() == "shop_dashboard"

    def _is_ctx_operable_for_action(self, action, ctx):
        page_type = str((ctx or {}).get("page_type") or "").strip().lower()
        if self._is_pin_unpin_action(action):
            return self._is_shop_dashboard_page_type(page_type)
        if bool((ctx or {}).get("is_operable")):
            return True
        return page_type in {"shop_dashboard", "tiktok_live_dashboard"}

    def _resolve_ocr_page_type_with_ctx_fallback(self, ocr):
        page_type = str((ocr or {}).get("page_type") or "").strip().lower()
        if page_type:
            return page_type
        try:
            ctx = self.vision_agent.get_page_context() or {}
        except Exception:
            ctx = {}
        return str((ctx or {}).get("page_type") or "").strip().lower()

    def _focus_action_page_for_screen_ocr(self, action):
        """
        screen_ocr 场景下尝试把浏览器执行页切到前台，避免 OCR 误扫到控制面板窗口。
        """
        focused = False
        ensure_browser = getattr(self.vision_agent, "ensure_browser_page_connection", None)
        if callable(ensure_browser):
            try:
                focused = bool(ensure_browser(force=True)) or focused
            except Exception:
                pass
        ensure_action = getattr(self.vision_agent, "ensure_action_page", None)
        if callable(ensure_action):
            try:
                focused = bool(ensure_action(action)) or focused
            except Exception:
                pass
        page = getattr(self.vision_agent, "page", None)
        activate = getattr(self.vision_agent, "_activate_tab_best_effort", None)
        if callable(activate) and page is not None:
            try:
                focused = bool(activate(page)) or focused
            except Exception:
                pass
        if focused:
            time.sleep(0.22)
        return focused

    def _is_ocr_operable_page(self, action):
        if not bool(getattr(settings, "OCR_VISION_PRECHECK_ENABLED", True)):
            return True, {"skipped": True}
        ocr = self._ocr_extract_page_text(use_cache=False)
        if not ocr.get("available"):
            return False, {"ocr": ocr, "reason": "ocr_unavailable"}
        action_norm = str(action or "").strip().lower()
        is_pin_unpin = self._is_pin_unpin_action(action_norm)
        page_type = str(ocr.get("page_type") or "").strip()
        if is_pin_unpin:
            if self._is_shop_dashboard_page_type(page_type):
                return True, {"ocr": ocr, "reason": "page_type_shop_dashboard", "page_type": page_type}
            scene_tags = set(str(t or "").strip() for t in list(ocr.get("scene_tags") or []))
            visible_hits = self._collect_visible_link_index_hits(ocr)
            return False, {
                "ocr": ocr,
                "reason": "pin_unpin_requires_shop_dashboard",
                "page_type": page_type,
                "scene_tags": list(scene_tags)[:10],
                "visible_band": self._infer_visible_link_index_band(ocr) if visible_hits else {},
                "visible_hits": int(len(visible_hits)),
            }
        if page_type in {"shop_dashboard", "tiktok_live_dashboard"}:
            return True, {"ocr": ocr, "reason": "page_type_operable", "page_type": page_type}
        scene_tags = set(str(t or "").strip() for t in list(ocr.get("scene_tags") or []))
        if scene_tags.intersection({"shop_console", "product_ops", "promo_ops", "product_panel_detected"}):
            return True, {"ocr": ocr, "reason": "scene_operable", "scene_tags": list(scene_tags)}
        text = str(ocr.get("text") or "").lower().replace(" ", "")
        if not text:
            return False, {"ocr": ocr, "reason": "ocr_empty"}
        required = [str(k or "").lower().replace(" ", "") for k in (getattr(settings, "OCR_VISION_REQUIRED_KEYWORDS", []) or [])]
        required = [k for k in required if k]
        if not required:
            return True, {"ocr": ocr, "reason": "no_required_keywords"}
        hit = any(k in text for k in required)
        detail = {"ocr": ocr, "keywords": required[:8], "hit": hit, "action": action}
        if hit:
            return True, detail
        llm_judge = self._llm_judge_operable_page(action, ocr)
        if bool(llm_judge.get("operable")) and float(llm_judge.get("confidence") or 0.0) >= float(self._nav_min_confidence):
            detail["llm_page_judge"] = llm_judge
            detail["reason"] = "llm_unknown_page_operable"
            return True, detail
        if llm_judge:
            detail["llm_page_judge"] = llm_judge
        return hit, detail

    def get_mode_status(self):
        return {
            "mode": self.get_execution_mode(),
            "ocr_available": bool(self.ocr_engine.available()),
            "ocr_provider": self._last_ocr_provider or (self.ocr_engine.provider or ""),
            "ocr_last_error": self._last_ocr_error or "",
            "ocr_last_ms": int(self._last_ocr_ms or 0),
            "ocr_last_at": self._last_ocr_at or 0.0,
            "ocr_last_text": (self._last_ocr_text or "")[:280],
            "ocr_last_lines": len(self._last_ocr_lines or []),
            "ocr_last_blocks": len(self._last_ocr_blocks or []),
            "ocr_last_scene_tags": list(self._last_ocr_scene_tags or [])[:10],
            "ocr_last_action_candidates": len(self._last_ocr_action_candidates or []),
            "ocr_last_source": self._last_ocr_source or "",
            "last_click_driver": self._last_click_driver or "",
            "last_click_point": dict(self._last_click_point or {}),
            "last_click_error": self._last_click_error or "",
            "ocr_pin_fixed_row_click_enabled": bool(self._ocr_pin_fixed_row_click_enabled),
            "ocr_pin_click_test_confirm_popup": bool(self._ocr_pin_click_test_confirm_popup),
            "ocr_pin_click_test_max_wait_seconds": float(self._ocr_pin_click_test_max_wait_seconds),
            "pin_unpin_force_fixed_row_click": bool(self._pin_unpin_force_fixed_row_click),
            "pin_unpin_require_link_index": bool(self._pin_unpin_require_link_index),
            "ocr_pin_fixed_row_click_panel_x_ratio": float(self._ocr_pin_fixed_row_click_panel_x_ratio),
            "ocr_pin_fixed_row_click_offset_y_ratio": float(self._ocr_pin_fixed_row_click_offset_y_ratio),
            "ocr_pin_fixed_row_calibration_log_enabled": bool(self._ocr_pin_fixed_row_calibration_log_enabled),
            "ocr_pin_fixed_row_calibration_log_path": str(self._ocr_pin_fixed_row_calibration_log_path or ""),
            "last_fixed_row_click": dict(self._last_fixed_row_click_meta or {}),
            "dom_fallback_enabled": bool(self._dom_fallback_enabled()),
            "force_full_physical_chain": bool(self._force_full_physical_chain),
            "human_like_settings": self.get_human_like_settings(),
            "human_like_stats": self.get_human_like_stats(recent_limit=5),
            "plan_enabled": bool(self._llm_plan_enabled),
            "plan_shadow_mode": bool(self._llm_plan_shadow_mode),
            "plan_next_step_enabled": bool(self._llm_plan_next_step_enabled),
            "plan_next_step_max_turns": int(self._llm_plan_next_step_max_turns),
            "plan_next_step_min_confidence": float(self._llm_plan_next_step_min_confidence),
            "plan_situation_driven": bool(self._llm_plan_situation_driven),
            "plan_min_confidence": float(self._llm_plan_min_confidence),
            "plan_last_trace": dict(self._last_action_plan_trace or {}),
            "nav_enabled": bool(self._nav_llm_enabled),
            "nav_unknown_page_enabled": bool(self._nav_unknown_page_enabled),
            "nav_min_confidence": float(self._nav_min_confidence),
            "nav_max_scroll_rounds": int(self._nav_max_scroll_rounds),
            "nav_max_llm_calls": int(self._nav_max_llm_calls),
            "nav_last_trace": dict(self._last_nav_trace or {}),
        }

    def get_last_action_plan_trace(self):
        return dict(self._last_action_plan_trace or {})

    def _allowed_plan_actions(self):
        return {"pin_product", "unpin_product", "start_flash_sale", "stop_flash_sale"}

    def _default_verify_timeout(self, action):
        if action in {"start_flash_sale", "stop_flash_sale"}:
            return 2.6
        return 2.2

    def _planner_llm(self):
        agent = self._action_planner_agent or self._reaction_judge_agent
        llm = getattr(agent, "llm", None) if agent else None
        has_llm = bool(getattr(agent, "has_llm", False))
        if has_llm and llm is not None:
            return llm
        return None

    def _navigator_llm(self):
        if not self._nav_llm_enabled:
            return None
        agent = self._operation_navigator_agent or self._action_planner_agent or self._reaction_judge_agent
        llm = getattr(agent, "llm", None) if agent else None
        has_llm = bool(getattr(agent, "has_llm", False))
        if has_llm and llm is not None:
            return llm
        return None

    def _allow_nav_llm_now(self):
        now = time.time()
        if (now - float(self._nav_last_llm_at or 0.0)) < float(self._nav_min_interval):
            return False
        self._nav_last_llm_at = now
        return True

    def _build_plan_observation(self, command):
        getter = getattr(self.vision_agent, "get_operation_observation", None)
        if callable(getter):
            try:
                data = getter(use_cache=True, max_candidates=16)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        ocr = self._ocr_extract_page_text(use_cache=True)
        targets = []
        target_map = {}
        for idx, item in enumerate(list((ocr or {}).get("action_candidates") or [])[:16], start=1):
            if not isinstance(item, dict):
                continue
            tid = f"t{idx:02d}"
            payload = {
                "target_id": tid,
                "action": str(item.get("action") or ""),
                "text": str(item.get("text") or "")[:120],
                "role": str(item.get("role") or ""),
                "score": float(item.get("score") or 0.0),
                "rect": dict(item.get("rect") or {}),
                "source": str(item.get("source") or ""),
            }
            targets.append(payload)
            target_map[tid] = dict(payload)
        page_ctx = {}
        try:
            page_ctx = dict(getattr(self.vision_agent, "get_page_context", lambda: {})() or {})
        except Exception:
            page_ctx = {}
        return {
            "ts": time.time(),
            "page_context": {
                "page_type": str(page_ctx.get("page_type") or "non_target"),
                "is_operable": bool(page_ctx.get("is_operable")),
                "is_monitor_only": bool(page_ctx.get("is_monitor_only")),
                "source": str(page_ctx.get("source") or ""),
            },
            "ocr": {
                "available": bool(ocr.get("available", True)),
                "error": str(ocr.get("error") or ""),
                "provider": str(ocr.get("provider") or ""),
                "scene_tags": list(ocr.get("scene_tags") or [])[:10],
                "line_count": int(len(list(ocr.get("lines") or []))),
                "text_preview": str(ocr.get("text") or "")[:260],
                "line_preview": [
                    str((ln or {}).get("text") or "")[:80]
                    for ln in list(ocr.get("lines") or [])[:10]
                    if isinstance(ln, dict)
                ],
            },
            "targets": targets,
            "target_map": target_map,
            "blocked_regions": [],
            "command": dict(command or {}),
        }

    def _build_fallback_action_plan(self, command, reason="rules_fallback"):
        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        return {
            "action": action,
            "reason": reason,
            "confidence": 1.0,
            "steps": [
                {"op": "execute_action", "action": action, "link_index": link_index},
                {
                    "op": "verify_receipt",
                    "action": action,
                    "link_index": link_index,
                    "timeout_seconds": self._default_verify_timeout(action),
                },
            ],
        }

    def _sanitize_action_plan(self, raw_plan, command, observation):
        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        target_map = dict((observation or {}).get("target_map") or {})
        allowed_ops = {"click_target", "execute_action", "verify_receipt", "wait", "stop"}
        banned_coordinate_keys = {"x", "y", "point", "coords", "coord", "viewport_point", "screen_point"}

        out = {
            "action": action,
            "reason": str((raw_plan or {}).get("reason") or ""),
            "confidence": float((raw_plan or {}).get("confidence") or 0.0),
            "steps": [],
        }
        raw_steps = list((raw_plan or {}).get("steps") or [])

        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            if any(k in raw_step for k in banned_coordinate_keys):
                continue
            op = str(raw_step.get("op") or raw_step.get("type") or "").strip().lower()
            if op not in allowed_ops:
                continue

            if op == "execute_action":
                out["steps"].append(
                    {"op": "execute_action", "action": action, "link_index": link_index}
                )
            elif op == "click_target":
                target_id = str(raw_step.get("target_id") or "").strip()
                if not target_id:
                    continue
                target = target_map.get(target_id)
                if not isinstance(target, dict):
                    continue
                target_action = str(target.get("action") or "").strip().lower()
                if target_action and target_action != action:
                    continue
                out["steps"].append(
                    {
                        "op": "click_target",
                        "action": action,
                        "link_index": link_index,
                        "target_id": target_id,
                    }
                )
            elif op == "verify_receipt":
                timeout_seconds = float(
                    raw_step.get("timeout_seconds")
                    or raw_step.get("timeout")
                    or self._default_verify_timeout(action)
                )
                out["steps"].append(
                    {
                        "op": "verify_receipt",
                        "action": action,
                        "link_index": link_index,
                        "timeout_seconds": max(0.6, min(12.0, timeout_seconds)),
                    }
                )
            elif op == "wait":
                wait_seconds = float(raw_step.get("wait_seconds") or raw_step.get("seconds") or 0.5)
                out["steps"].append({"op": "wait", "wait_seconds": max(0.1, min(3.0, wait_seconds))})
            elif op == "stop":
                out["steps"].append({"op": "stop", "reason": str(raw_step.get("reason") or "")[:120]})

            if len(out["steps"]) >= self._llm_plan_max_steps:
                break

        has_executor = any(s.get("op") in {"click_target", "execute_action"} for s in out["steps"])
        if (not out["steps"]) or (not has_executor):
            return self._build_fallback_action_plan(command, reason="sanitized_to_fallback")
        if out.get("confidence", 0.0) < self._llm_plan_min_confidence:
            return self._build_fallback_action_plan(command, reason="low_confidence_fallback")
        return out

    def _sanitize_single_plan_step(self, raw_step, command, observation):
        if not isinstance(raw_step, dict):
            return None
        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        target_map = dict((observation or {}).get("target_map") or {})
        allowed_ops = {"click_target", "execute_action", "verify_receipt", "wait", "stop"}
        banned_coordinate_keys = {"x", "y", "point", "coords", "coord", "viewport_point", "screen_point"}
        if any(k in raw_step for k in banned_coordinate_keys):
            return None

        op = str(raw_step.get("op") or raw_step.get("type") or "").strip().lower()
        if op not in allowed_ops:
            return None
        if op == "execute_action":
            return {"op": "execute_action", "action": action, "link_index": link_index}
        if op == "click_target":
            target_id = str(raw_step.get("target_id") or "").strip()
            if not target_id:
                return None
            target = target_map.get(target_id)
            if not isinstance(target, dict):
                return None
            target_action = str(target.get("action") or "").strip().lower()
            if target_action and target_action != action:
                return None
            return {"op": "click_target", "action": action, "link_index": link_index, "target_id": target_id}
        if op == "verify_receipt":
            timeout_seconds = float(
                raw_step.get("timeout_seconds")
                or raw_step.get("timeout")
                or self._default_verify_timeout(action)
            )
            return {
                "op": "verify_receipt",
                "action": action,
                "link_index": link_index,
                "timeout_seconds": max(0.6, min(12.0, timeout_seconds)),
            }
        if op == "wait":
            wait_seconds = float(raw_step.get("wait_seconds") or raw_step.get("seconds") or 0.5)
            return {"op": "wait", "wait_seconds": max(0.1, min(3.0, wait_seconds))}
        if op == "stop":
            return {"op": "stop", "reason": str(raw_step.get("reason") or "")[:120]}
        return None

    def _pick_target_id(self, observation, action, keyword_hints=None):
        keyword_hints = [str(x or "").strip().lower() for x in (keyword_hints or []) if str(x or "").strip()]
        action = str(action or "").strip().lower()
        scored = []
        for item in list((observation or {}).get("targets") or []):
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("target_id") or "").strip()
            if not target_id:
                continue
            target_action = str(item.get("action") or "").strip().lower()
            if target_action and target_action != action:
                continue
            txt = str(item.get("text") or "").strip().lower()
            score = float(item.get("score") or 0.0)
            if keyword_hints and any(k in txt for k in keyword_hints):
                score += 5.0
            scored.append((score, target_id))
        if not scored:
            return ""
        scored.sort(key=lambda x: x[0], reverse=True)
        return str(scored[0][1] or "")

    def _detect_operation_situations(self, observation, history_steps=None, last_verify=None):
        obs = dict(observation or {})
        ctx = dict(obs.get("page_context") or {})
        ocr = dict(obs.get("ocr") or {})
        targets = list(obs.get("targets") or [])
        history = list(history_steps or [])
        situations = []

        def _add(code, severity="medium", evidence="", suggestion=""):
            situations.append(
                {
                    "code": str(code or ""),
                    "severity": str(severity or "medium"),
                    "evidence": str(evidence or "")[:160],
                    "suggestion": str(suggestion or "")[:80],
                }
            )

        if (not bool(ctx.get("is_operable"))) or str(ctx.get("page_type") or "non_target") == "non_target":
            _add(
                "non_operable_page",
                "high",
                f"page_type={ctx.get('page_type')}, source={ctx.get('source')}",
                "stop_or_wait",
            )

        ocr_available = bool(ocr.get("available", True))
        ocr_error = str(ocr.get("error") or "").strip()
        if (not ocr_available) or ocr_error:
            _add("ocr_unstable", "high", ocr_error or "ocr_unavailable", "wait_then_execute_action")

        text_blob = " ".join(
            [
                str(ocr.get("text_preview") or ""),
                " ".join(str(x or "") for x in list(ocr.get("line_preview") or [])),
            ]
        ).lower()
        if text_blob:
            if any(k in text_blob for k in ["确认", "confirm", "是否", "are you sure", "提示", "warning"]):
                _add("confirm_popup_like", "medium", "popup/confirm keywords detected", "click_target_or_verify")
            if any(k in text_blob for k in ["失败", "error", "错误", "denied", "forbidden", "insufficient"]):
                _add("error_popup_like", "high", "error keywords detected", "stop_or_retry")
            if any(k in text_blob for k in ["成功", "success", "已开启", "已置顶", "完成"]):
                _add("success_signal_like", "low", "success keywords detected", "verify_receipt")

        if len(targets) == 0:
            _add("target_pool_empty", "medium", "no action candidates", "execute_action")

        recent_exec = [x for x in history[-4:] if str((x or {}).get("state") or "") == "EXECUTE"]
        fail_count = sum(1 for x in recent_exec if not bool((x or {}).get("ok")))
        if fail_count >= 2:
            _add("repeated_step_failure", "high", f"recent_exec_failures={fail_count}", "retry_or_stop")

        recent_ops = [str((x or {}).get("op") or "") for x in recent_exec[-3:]]
        if len(recent_ops) >= 3 and len(set(recent_ops)) == 1 and fail_count >= 2:
            _add("stalled_no_progress", "high", f"same_op={recent_ops[-1]}, failures={fail_count}", "retry_or_stop")

        if isinstance(last_verify, dict) and (not bool(last_verify.get("ok"))) and history:
            _add("receipt_unconfirmed", "medium", "latest verify not confirmed", "verify_or_retry")

        return situations[:6]

    def _build_situation_policy(self, command, situations, observation, retries=0):
        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        codes = [str((s or {}).get("code") or "") for s in list(situations or []) if isinstance(s, dict)]
        code_set = set(codes)

        policy = {
            "policy_id": "default_continue",
            "priority": 50,
            "rationale": "default",
            "next_step": {"op": "execute_action", "action": action, "link_index": link_index},
            "fallback_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
        }

        confirm_target_id = self._pick_target_id(
            observation=observation,
            action=action,
            keyword_hints=["确认", "确定", "继续", "ok", "confirm", "yes"],
        )

        if "non_operable_page" in code_set:
            return {
                "policy_id": "hard_stop_non_operable",
                "priority": 100,
                "rationale": "page not operable",
                "next_step": {"op": "stop", "reason": "non_operable_page"},
                "fallback_step": {"op": "wait", "wait_seconds": 0.6},
            }

        if "error_popup_like" in code_set and int(retries or 0) >= int(self._llm_plan_max_retries or 0):
            return {
                "policy_id": "stop_after_error_retries",
                "priority": 95,
                "rationale": "error popup after retries exhausted",
                "next_step": {"op": "stop", "reason": "error_popup_retry_exhausted"},
                "fallback_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
            }

        if "confirm_popup_like" in code_set and confirm_target_id:
            return {
                "policy_id": "confirm_popup_click",
                "priority": 90,
                "rationale": "confirm-like popup detected; try confirm target",
                "next_step": {
                    "op": "click_target",
                    "action": action,
                    "link_index": link_index,
                    "target_id": confirm_target_id,
                },
                "fallback_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
            }

        if "success_signal_like" in code_set or "receipt_unconfirmed" in code_set:
            return {
                "policy_id": "verify_after_signal",
                "priority": 85,
                "rationale": "success or receipt-related signals detected",
                "next_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
                "fallback_step": {"op": "wait", "wait_seconds": 0.4},
            }

        if "stalled_no_progress" in code_set or "repeated_step_failure" in code_set:
            if int(retries or 0) >= int(self._llm_plan_max_retries or 0):
                return {
                    "policy_id": "stop_after_stall",
                    "priority": 80,
                    "rationale": "repeated failures and retries exhausted",
                    "next_step": {"op": "stop", "reason": "stalled_retry_exhausted"},
                    "fallback_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
                }
            return {
                "policy_id": "retry_after_stall",
                "priority": 78,
                "rationale": "repeated failures detected; retry action",
                "next_step": {"op": "execute_action", "action": action, "link_index": link_index},
                "fallback_step": {"op": "wait", "wait_seconds": 0.6},
            }

        if "target_pool_empty" in code_set:
            return {
                "policy_id": "reacquire_targets",
                "priority": 72,
                "rationale": "candidate target pool empty",
                "next_step": {"op": "execute_action", "action": action, "link_index": link_index},
                "fallback_step": {"op": "wait", "wait_seconds": 0.5},
            }

        if "ocr_unstable" in code_set:
            return {
                "policy_id": "wait_for_ocr_recover",
                "priority": 70,
                "rationale": "ocr unstable; wait before next step",
                "next_step": {"op": "wait", "wait_seconds": 0.7},
                "fallback_step": {"op": "execute_action", "action": action, "link_index": link_index},
            }

        if "error_popup_like" in code_set:
            return {
                "policy_id": "verify_then_retry_on_error",
                "priority": 68,
                "rationale": "error-like text detected; verify then retry if needed",
                "next_step": {"op": "verify_receipt", "action": action, "link_index": link_index},
                "fallback_step": {"op": "execute_action", "action": action, "link_index": link_index},
            }

        return policy

    def _rule_next_step_from_situations(self, command, situations, retries=0):
        if not situations:
            return None
        # 兼容旧调用，rule 走默认模板策略，observation 缺省时仅返回保守动作。
        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        code_set = {str((s or {}).get("code") or "") for s in situations if isinstance(s, dict)}
        if "non_operable_page" in code_set:
            return {"op": "stop", "reason": "non_operable_page"}
        if "success_signal_like" in code_set or "receipt_unconfirmed" in code_set:
            return {"op": "verify_receipt", "action": action, "link_index": link_index}
        if "stalled_no_progress" in code_set or "repeated_step_failure" in code_set:
            if int(retries or 0) >= int(self._llm_plan_max_retries or 0):
                return {"op": "stop", "reason": "stalled_retry_exhausted"}
            return {"op": "execute_action", "action": action, "link_index": link_index}
        if "ocr_unstable" in code_set:
            return {"op": "wait", "wait_seconds": 0.7}
        return {"op": "execute_action", "action": action, "link_index": link_index}

    def _build_llm_next_step_decision(
        self,
        command,
        observation,
        history_steps,
        plan,
        retries,
        started_at,
        situations=None,
        situation_policy=None,
    ):
        llm = self._planner_llm()
        if llm is None:
            return None

        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        targets = []
        for item in list((observation or {}).get("targets") or [])[:12]:
            if not isinstance(item, dict):
                continue
            targets.append(
                {
                    "target_id": str(item.get("target_id") or ""),
                    "action": str(item.get("action") or ""),
                    "text": str(item.get("text") or "")[:80],
                    "role": str(item.get("role") or ""),
                    "score": float(item.get("score") or 0.0),
                }
            )

        compact_history = []
        for item in list(history_steps or [])[-10:]:
            if not isinstance(item, dict):
                continue
            row = {
                "state": str(item.get("state") or ""),
                "op": str(item.get("op") or ""),
                "ok": bool(item.get("ok")),
            }
            if item.get("reason"):
                row["reason"] = str(item.get("reason"))
            if item.get("target_id"):
                row["target_id"] = str(item.get("target_id"))
            compact_history.append(row)

        payload = {
            "command": {"action": action, "link_index": link_index},
            "elapsed_ms": int(max(0.0, (time.time() - float(started_at or time.time())) * 1000.0)),
            "retries": int(retries or 0),
            "page_context": dict((observation or {}).get("page_context") or {}),
            "ocr": dict((observation or {}).get("ocr") or {}),
            "targets": targets,
            "initial_plan": list((plan or {}).get("steps") or [])[: self._llm_plan_max_steps],
            "history": compact_history,
            "situation_hints": list(situations or [])[:6],
            "situation_policy": dict(situation_policy or {}),
            "constraints": {
                "allowed_ops": ["click_target", "execute_action", "verify_receipt", "wait", "stop"],
                "forbidden": ["raw coordinates", "x/y", "point", "rect", "free-form js click"],
            },
        }
        prompt = (
            "你是直播运营动作执行控制器。"
            "请基于最新观察和历史步骤，判断“下一步应该做什么”。"
            "重点关注 situation_hints（新情况提示），优先处理高 severity 情况。"
            "situation_policy 给出了优先策略；若要偏离该策略，请在 reason 中说明。"
            "只能输出 JSON，不要输出任何额外文本。"
            "JSON格式："
            "{\"done\":true|false,\"confidence\":0~1,\"reason\":\"...\","
            "\"next_step\":{\"op\":\"click_target|execute_action|verify_receipt|wait|stop\",...}}"
            "。当 done=true 时可省略 next_step。"
            "严禁输出任何坐标字段。\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = llm.invoke(prompt)
            raw = str(getattr(response, "content", response) or "").strip()
            data = self._extract_json_payload(raw)
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            logger.warning(f"LLM下一步决策失败: {e}")
            return None

    def _build_llm_action_plan(self, command, observation):
        llm = self._planner_llm()
        if llm is None:
            return None

        action = str((command or {}).get("action") or "").strip().lower()
        link_index = (command or {}).get("link_index")
        targets = []
        for item in list((observation or {}).get("targets") or [])[:12]:
            if not isinstance(item, dict):
                continue
            targets.append(
                {
                    "target_id": str(item.get("target_id") or ""),
                    "action": str(item.get("action") or ""),
                    "text": str(item.get("text") or "")[:80],
                    "role": str(item.get("role") or ""),
                    "score": float(item.get("score") or 0.0),
                }
            )

        payload = {
            "command": {"action": action, "link_index": link_index},
            "page_context": dict((observation or {}).get("page_context") or {}),
            "ocr": dict((observation or {}).get("ocr") or {}),
            "targets": targets,
            "constraints": {
                "allowed_actions": sorted(list(self._allowed_plan_actions())),
                "allowed_ops": ["click_target", "execute_action", "verify_receipt", "wait", "stop"],
                "max_steps": int(self._llm_plan_max_steps),
                "forbidden": ["raw coordinates", "x/y", "point", "rect", "any free-form JS click"],
            },
        }
        prompt = (
            "你是直播运营动作规划器。"
            "请仅输出 JSON，不要输出其他文本。"
            "目标：根据当前命令和候选目标，给出受限动作计划。"
            "JSON 格式："
            "{\"action\":\"...\",\"confidence\":0~1,\"reason\":\"...\",\"steps\":["
            "{\"op\":\"click_target\",\"target_id\":\"t01\"} 或 "
            "{\"op\":\"execute_action\",\"action\":\"pin_product\",\"link_index\":1} 或 "
            "{\"op\":\"verify_receipt\",\"timeout_seconds\":2.2} 或 "
            "{\"op\":\"wait\",\"wait_seconds\":0.5} 或 "
            "{\"op\":\"stop\",\"reason\":\"...\"}"
            "]}\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            response = llm.invoke(prompt)
            raw = str(getattr(response, "content", response) or "").strip()
            return self._extract_json_payload(raw)
        except Exception as e:
            logger.warning(f"LLM动作规划失败，回退规则计划: {e}")
            return None

    def _append_plan_replay(self, payload):
        path = Path(self._llm_plan_replay_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入动作计划回放日志失败: {e}")

    def _attach_plan_trace_to_receipt(self, trace):
        if not isinstance(self.last_action_receipt, dict):
            return
        detail = dict(self.last_action_receipt.get("detail") or {})
        detail["action_plan"] = dict(trace or {})
        self.last_action_receipt["detail"] = detail

    def _execute_legacy_action(self, action, link_index=None):
        action = str(action or "").strip().lower()
        if action == "pin_product":
            return bool(self.pin_product(link_index=link_index))
        if action == "unpin_product":
            return bool(self.unpin_product(link_index=link_index))
        if action == "start_flash_sale":
            return bool(self.start_flash_sale())
        if action == "stop_flash_sale":
            return bool(self.stop_flash_sale())
        return False

    def _execute_click_target_step(self, action, step, observation):
        target_id = str((step or {}).get("target_id") or "").strip()
        target = dict(((observation or {}).get("target_map") or {}).get(target_id) or {})
        if not target_id or not target:
            return {
                "ok": False,
                "reason": "target_not_found",
                "target_id": target_id,
            }
        target_action = str(target.get("action") or "").strip().lower()
        if target_action and target_action != action:
            return {
                "ok": False,
                "reason": "target_action_mismatch",
                "target_id": target_id,
                "target_action": target_action,
            }
        preferred_text = str(target.get("text") or "").strip()
        result = self._perform_action_by_ocr_anchor(
            action,
            link_index=(step or {}).get("link_index"),
            preferred_text=preferred_text,
        )
        detail = dict(result or {})
        detail["target_id"] = target_id
        return {"ok": bool(result.get("ok")), "reason": str(result.get("reason") or ""), "detail": detail}

    def _verify_action_receipt(self, action, link_index=None, timeout_seconds=None):
        timeout_seconds = float(timeout_seconds or self._default_verify_timeout(action))
        timeout_seconds = max(0.6, min(12.0, timeout_seconds))

        dom_verify = None
        if self._dom_fallback_enabled():
            try:
                if action == "pin_product":
                    dom_verify = self._verify_pin_receipt(link_index=link_index, timeout_seconds=timeout_seconds)
                elif action == "unpin_product":
                    dom_verify = self._verify_unpin_receipt(link_index=link_index, timeout_seconds=timeout_seconds)
                elif action == "start_flash_sale":
                    dom_verify = self._verify_flash_sale_receipt(timeout_seconds=timeout_seconds)
                elif action == "stop_flash_sale":
                    dom_verify = self._verify_stop_flash_sale_receipt(timeout_seconds=timeout_seconds)
            except Exception:
                dom_verify = None
        if dom_verify:
            return {"ok": True, "source": "dom", "detail": dom_verify}

        ocr_verify = self._verify_receipt_by_ocr(action, link_index=link_index)
        if ocr_verify:
            return {"ok": True, "source": "ocr", "detail": ocr_verify}
        return {"ok": False, "source": "none", "detail": {}}

    def execute_action_with_plan(self, command, trigger_source=""):
        if not self._llm_plan_enabled:
            return None
        if not isinstance(command, dict):
            return None
        action = str(command.get("action") or "").strip().lower()
        if action not in self._allowed_plan_actions():
            return None

        observation = self._build_plan_observation(command)
        raw_plan = self._build_llm_action_plan(command, observation)
        if not isinstance(raw_plan, dict):
            raw_plan = self._build_fallback_action_plan(command, reason="no_llm_plan")
            plan_source = "rules"
        else:
            plan_source = "llm"
        plan = self._sanitize_action_plan(raw_plan, command, observation)

        started = time.time()
        trace = {
            "ts": started,
            "trigger_source": str(trigger_source or ""),
            "action": action,
            "shadow_mode": bool(self._llm_plan_shadow_mode),
            "plan_source": plan_source,
            "plan": dict(plan or {}),
            "steps": [],
            "result": {"ok": False, "reason": "not_executed"},
        }

        link_index = command.get("link_index")
        ok = False
        reason = ""
        last_verify = {}
        last_situations = []
        last_situation_policy = {}

        if self._llm_plan_shadow_mode:
            legacy_ok = self._execute_legacy_action(action, link_index=link_index)
            trace["steps"].append(
                {
                    "state": "EXECUTE",
                    "op": "shadow_legacy",
                    "ok": bool(legacy_ok),
                    "ts": time.time(),
                }
            )
            ok = bool(legacy_ok)
            reason = "shadow_legacy_ok" if ok else "shadow_legacy_failed"
        else:
            retries = 0
            executed_any = False
            step_queue = list(plan.get("steps") or [])[: self._llm_plan_max_steps]
            turns = 0
            while retries <= self._llm_plan_max_retries and (time.time() - started) <= self._llm_plan_timeout_seconds:
                if (time.time() - started) > self._llm_plan_timeout_seconds:
                    trace["steps"].append(
                        {
                            "state": "STOP",
                            "op": "timeout",
                            "ok": False,
                            "elapsed_ms": int((time.time() - started) * 1000),
                        }
                    )
                    reason = "timeout"
                    break

                if turns >= self._llm_plan_next_step_max_turns:
                    trace["steps"].append(
                        {
                            "state": "STOP",
                            "op": "max_turns",
                            "ok": False,
                            "turns": int(turns),
                        }
                    )
                    reason = "max_turns"
                    break

                step = None
                step_source = "plan_queue"
                llm_decision = None
                observation = self._build_plan_observation(command)
                situations = self._detect_operation_situations(
                    observation=observation,
                    history_steps=trace.get("steps"),
                    last_verify=last_verify,
                )
                last_situations = list(situations or [])
                situation_policy = self._build_situation_policy(
                    command=command,
                    situations=situations,
                    observation=observation,
                    retries=retries,
                )
                last_situation_policy = dict(situation_policy or {})

                should_llm_decide = bool(
                    self._llm_plan_next_step_enabled
                    and (
                        (not step_queue)
                        or (self._llm_plan_situation_driven and bool(situations))
                    )
                )

                if should_llm_decide:
                    llm_decision = self._build_llm_next_step_decision(
                        command=command,
                        observation=observation,
                        history_steps=trace.get("steps"),
                        plan=plan,
                        retries=retries,
                        started_at=started,
                        situations=situations,
                        situation_policy=situation_policy,
                    )
                    if isinstance(llm_decision, dict):
                        decision_conf = float(llm_decision.get("confidence") or 0.0)
                        if bool(llm_decision.get("done")):
                            verify_res = self._verify_action_receipt(action, link_index=link_index)
                            trace["steps"].append(
                                {
                                    "state": "VERIFY",
                                    "op": "llm_done_verify",
                                    "ok": bool(verify_res.get("ok")),
                                    "verify": verify_res,
                                    "reason": str(llm_decision.get("reason") or ""),
                                    "confidence": decision_conf,
                                    "ts": time.time(),
                                }
                            )
                            last_verify = dict(verify_res or {})
                            if verify_res.get("ok"):
                                ok = True
                                reason = "llm_done_verify_ok"
                            else:
                                reason = "llm_done_verify_failed"
                            break
                        if decision_conf >= self._llm_plan_next_step_min_confidence:
                            step = self._sanitize_single_plan_step(
                                llm_decision.get("next_step"),
                                command=command,
                                observation=observation,
                            )
                            step_source = "llm_next_step"
                    if step is None:
                        rule_step = dict((situation_policy or {}).get("next_step") or {})
                        if not rule_step:
                            rule_step = self._rule_next_step_from_situations(
                                command=command,
                                situations=situations,
                                retries=retries,
                            )
                        if isinstance(rule_step, dict):
                            step = self._sanitize_single_plan_step(
                                rule_step,
                                command=command,
                                observation=observation,
                            )
                            step_source = "situation_rule"
                            if (step is None) and isinstance((situation_policy or {}).get("fallback_step"), dict):
                                step = self._sanitize_single_plan_step(
                                    situation_policy.get("fallback_step"),
                                    command=command,
                                    observation=observation,
                                )
                                if step is not None:
                                    step_source = "situation_fallback_rule"
                        elif step_queue:
                            step = step_queue.pop(0)
                            step_source = "plan_queue_fallback"
                        else:
                            # 既无可执行规则也无计划步骤，回退一次 legacy，避免卡死。
                            retries += 1
                            retry_ok = self._execute_legacy_action(action, link_index=link_index)
                            trace["steps"].append(
                                {
                                    "state": "RETRY",
                                    "op": "legacy_retry",
                                    "retry": retries,
                                    "ok": bool(retry_ok),
                                    "reason": "llm_no_next_step",
                                    "situations": list(situations or []),
                                    "situation_policy": dict(situation_policy or {}),
                                    "ts": time.time(),
                                }
                            )
                            if retry_ok:
                                ok = True
                                reason = "legacy_retry_ok"
                                break
                            if retries > self._llm_plan_max_retries:
                                reason = "llm_no_next_step_retry_exhausted"
                                break
                            continue
                elif step_queue:
                    step = step_queue.pop(0)
                    step_source = "plan_queue"
                else:
                    if executed_any:
                        auto_verify = self._verify_action_receipt(action, link_index=link_index)
                        trace["steps"].append(
                            {
                                "state": "VERIFY",
                                "op": "auto_verify",
                                "ok": bool(auto_verify.get("ok")),
                                "verify": auto_verify,
                                "ts": time.time(),
                            }
                        )
                        last_verify = dict(auto_verify or {})
                        if auto_verify.get("ok"):
                            ok = True
                            reason = "auto_verify_ok"
                        else:
                            reason = "plan_finished_verify_failed"
                    else:
                        reason = "plan_finished_no_exec"
                    break

                turns += 1
                op = str((step or {}).get("op") or "").strip().lower()
                step_payload = {
                    "state": "EXECUTE",
                    "op": op,
                    "source": step_source,
                    "turn": int(turns),
                    "ts": time.time(),
                    "ok": False,
                    "situations": list(situations or []),
                    "situation_policy": dict(situation_policy or {}),
                }
                if isinstance(llm_decision, dict):
                    step_payload["llm_reason"] = str(llm_decision.get("reason") or "")
                    step_payload["llm_confidence"] = float(llm_decision.get("confidence") or 0.0)

                if op == "wait":
                    wait_seconds = float((step or {}).get("wait_seconds") or 0.5)
                    time.sleep(max(0.1, min(3.0, wait_seconds)))
                    step_payload["ok"] = True
                    step_payload["wait_seconds"] = wait_seconds
                elif op == "execute_action":
                    step_ok = self._execute_legacy_action(action, link_index=link_index)
                    step_payload["ok"] = bool(step_ok)
                    executed_any = executed_any or bool(step_ok)
                elif op == "click_target":
                    observation = self._build_plan_observation(command)
                    click_res = self._execute_click_target_step(action, step, observation)
                    step_payload.update(click_res)
                    executed_any = executed_any or bool(click_res.get("ok"))
                elif op == "verify_receipt":
                    verify_res = self._verify_action_receipt(
                        action,
                        link_index=link_index,
                        timeout_seconds=(step or {}).get("timeout_seconds"),
                    )
                    step_payload["ok"] = bool(verify_res.get("ok"))
                    step_payload["verify"] = verify_res
                    last_verify = dict(verify_res or {})
                    if step_payload["ok"]:
                        ok = True
                        reason = "verify_ok"
                elif op == "stop":
                    step_payload["ok"] = True
                    step_payload["reason"] = str((step or {}).get("reason") or "")
                    reason = "stopped_by_plan"

                trace["steps"].append(step_payload)
                if ok or op == "stop":
                    break

            if not reason:
                reason = "plan_exhausted"

        trace["result"] = {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "elapsed_ms": int((time.time() - started) * 1000),
            "last_verify": dict(last_verify or {}),
            "last_situations": list(last_situations or []),
            "last_situation_policy": dict(last_situation_policy or {}),
        }
        current_receipt_action = str((self.last_action_receipt or {}).get("action") or "")
        if current_receipt_action != action:
            self.last_action_receipt = {
                "action": action,
                "ok": bool(ok),
                "stage": "plan",
                "reason": str(reason or ""),
                "detail": {
                    "execution_mode": self.get_execution_mode(),
                    "plan_only": True,
                    "last_verify": dict(last_verify or {}),
                },
                "ts": time.time(),
            }
        self._last_action_plan_trace = dict(trace)
        self._attach_plan_trace_to_receipt(trace)
        self._append_plan_replay(
            {
                "ts": time.time(),
                "action": action,
                "trigger_source": str(trigger_source or ""),
                "ok": bool(ok),
                "reason": str(reason or ""),
                "shadow_mode": bool(self._llm_plan_shadow_mode),
                "plan_source": str(plan_source or ""),
                "plan": dict(plan or {}),
                "steps": list(trace.get("steps") or []),
                "receipt": dict(self.get_last_action_receipt() or {}),
            }
        )
        return bool(ok)

    def _link_index_hint_tokens(self, idx):
        try:
            idx_val = int(idx or 0)
        except Exception:
            idx_val = 0
        if idx_val <= 0:
            return []
        raw_tokens = [
            f"序号{idx_val}",
            f"第{idx_val}",
            f"{idx_val}号",
            f"{idx_val}号链接",
            f"{idx_val}号商品",
            f"link{idx_val}",
            f"item{idx_val}",
            f"product{idx_val}",
            f"no{idx_val}",
            f"number{idx_val}",
            f"#{idx_val}",
        ]
        out = []
        seen = set()
        for t in raw_tokens:
            n = self._norm_ocr_text(t)
            if (not n) or n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out

    def _verify_pin_unpin_receipt_with_index(self, action, ocr, idx):
        try:
            idx_val = int(idx or 0)
        except Exception:
            idx_val = 0
        if idx_val <= 0:
            return None
        lines = list((ocr or {}).get("lines") or [])
        if not lines:
            return None

        product_panel = self._pick_primary_block_rect(ocr or {}, "product_panel") or {}
        idx_tokens = self._link_index_hint_tokens(idx_val)

        def _is_pin_success_line(item):
            n = str(item.get("norm") or "")
            low = str(item.get("low") or "")
            if any(k in n for k in ["已置顶", "取消置顶", "置顶成功"]):
                return True
            return bool(re.search(r"(?<![a-z])(unpin|pinned|pinsuccess|pinnedsuccess)(?![a-z])", low))

        def _is_unpin_success_line(item):
            n = str(item.get("norm") or "")
            low = str(item.get("low") or "")
            if any(k in n for k in ["取消置顶成功", "已取消置顶", "未置顶商品", "撤销置顶"]):
                return True
            return bool(re.search(r"(?<![a-z])(unpinned|unpinsuccess|nopinned)(?![a-z])", low))

        def _looks_like_pin_button_line(item):
            n = str(item.get("norm") or "")
            low = str(item.get("low") or "")
            if ("取消置顶" in n) or bool(re.search(r"(?<![a-z])(unpin|pinned|unpinned)(?![a-z])", low)):
                return False
            if "置顶" in n:
                return True
            return bool(re.search(r"(?<![a-z])pin(?![a-z])", low))

        parsed_lines = []
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            raw = str((ln or {}).get("text") or "").strip()
            if not raw:
                continue
            if self._is_streamlit_panel_noise_text(raw) or self._is_browser_chrome_noise_text(raw):
                continue
            rect = (ln or {}).get("rect") or {}
            line_idx = self._extract_link_index_from_line(
                raw,
                rect=rect if isinstance(rect, dict) else {},
                product_panel_rect=product_panel,
            )
            norm = self._norm_ocr_text(raw)
            low = raw.lower()
            center = self._extract_rect_center(rect) if isinstance(rect, dict) else None
            cy = float(center[1]) if center else 0.0
            parsed_lines.append(
                {
                    "raw": raw,
                    "norm": norm,
                    "low": low,
                    "rect": rect if isinstance(rect, dict) else {},
                    "idx": int(line_idx or 0),
                    "cy": cy,
                }
            )
        if not parsed_lines:
            return None

        idx_rows = []
        for item in parsed_lines:
            has_idx = int(item.get("idx") or 0) == idx_val
            if (not has_idx) and idx_tokens:
                has_idx = any(tok in str(item.get("norm") or "") for tok in idx_tokens)
            if has_idx:
                idx_rows.append(item)
        if not idx_rows:
            return None

        for row in idx_rows:
            row_rect = row.get("rect") if isinstance(row.get("rect"), dict) else {}
            try:
                row_h = max(0.0, float(row_rect.get("y2") or 0.0) - float(row_rect.get("y1") or 0.0))
            except Exception:
                row_h = 0.0
            y_gate = max(42.0, row_h * 1.8)
            neighborhood = [x for x in parsed_lines if abs(float(x.get("cy") or 0.0) - float(row.get("cy") or 0.0)) <= y_gate]

            if action == "pin_product":
                if _is_pin_success_line(row):
                    return {"ok": True, "source": "ocr_index_bound_row_state", "action": action, "idx": idx_val}
                for item in neighborhood:
                    if _is_pin_success_line(item):
                        return {"ok": True, "source": "ocr_index_bound_neighbor_state", "action": action, "idx": idx_val}
                continue

            if action == "unpin_product":
                if _is_unpin_success_line(row):
                    return {"ok": True, "source": "ocr_index_bound_row_state", "action": action, "idx": idx_val}
                if _looks_like_pin_button_line(row):
                    return {"ok": True, "source": "ocr_index_bound_row_pin_button", "action": action, "idx": idx_val}
                for item in neighborhood:
                    if _is_unpin_success_line(item):
                        return {"ok": True, "source": "ocr_index_bound_neighbor_state", "action": action, "idx": idx_val}
                    if _looks_like_pin_button_line(item):
                        return {"ok": True, "source": "ocr_index_bound_neighbor_pin_button", "action": action, "idx": idx_val}
        return None

    def _verify_receipt_from_ocr_text(self, action, text_norm, link_index=None, ocr=None):
        text = str(text_norm or "")
        if not text:
            return None

        idx = int(link_index or 0) if link_index else 0
        if action in {"pin_product", "unpin_product"} and idx > 0:
            bound = self._verify_pin_unpin_receipt_with_index(action, ocr or {}, idx)
            if bound:
                return bound
            return None

        if action == "pin_product":
            keys = ["已置顶", "取消置顶", "unpin", "pinned", "置顶成功", "pinsuccess"]
            if any(k.lower().replace(" ", "") in text for k in keys):
                return {"ok": True, "source": "ocr_text", "action": action, "idx": idx}
            return None

        if action == "unpin_product":
            keys = ["取消置顶成功", "未置顶商品", "unpinned", "unpinsuccess", "撤销置顶"]
            if any(k.lower().replace(" ", "") in text for k in keys):
                return {"ok": True, "source": "ocr_text", "action": action, "idx": idx}
            return None

        if action == "start_flash_sale":
            keys = [
                "秒杀进行中",
                "结束秒杀活动",
                "秒杀已开启",
                "活动上架成功",
                "flashsale",
                "flashdeal",
                "endflashsale",
                "promolive",
            ]
            if any(k.lower().replace(" ", "") in text for k in keys):
                return {"ok": True, "source": "ocr_text", "action": action}
            return None

        if action == "stop_flash_sale":
            keys = [
                "秒杀活动上架",
                "秒杀上架",
                "开启秒杀",
                "开始秒杀",
                "flashsale",
                "startflashsale",
                "launchflashsale",
            ]
            if any(k.lower().replace(" ", "") in text for k in keys):
                return {"ok": True, "source": "ocr_text", "action": action}
            return None

        return None

    def _verify_receipt_by_ocr(self, action, link_index=None):
        ocr = self._ocr_extract_page_text(use_cache=False)
        text = self._norm_ocr_text(ocr.get("text") or "")
        return self._verify_receipt_from_ocr_text(action, text, link_index=link_index, ocr=ocr)

    def _set_action_receipt(self, action, ok, stage, reason="", detail=None):
        payload_detail = dict(detail or {})
        payload_detail.setdefault("execution_mode", self.get_execution_mode())
        trace = self._finalize_action_trace(action, ok=ok, note=reason)
        payload_detail.setdefault("human_like_settings", self.get_human_like_settings())
        payload_detail.setdefault("human_like_stats", self.get_human_like_stats(recent_limit=5))
        if trace:
            payload_detail.setdefault("human_trace", trace)
        self.last_action_receipt = {
            "action": action,
            "ok": bool(ok),
            "stage": stage,
            "reason": reason or "",
            "detail": payload_detail,
            "ts": time.time(),
        }

    def get_last_action_receipt(self):
        return dict(self.last_action_receipt or {})

    def _iter_contexts(self):
        """返回 page 及其 frame 上下文，便于跨 iframe 查找元素。"""
        page = self.vision_agent.page
        if not page:
            return []

        contexts = [("page", page)]
        seen_ids = {id(page)}

        try:
            frames = page.get_frames() or []
        except Exception:
            frames = []

        for idx, frame in enumerate(frames):
            if id(frame) in seen_ids:
                continue
            contexts.append((f"frame[{idx}]", frame))
            seen_ids.add(id(frame))

        return contexts

    def _ordered_contexts(self, prefer_ctx_name=None):
        page = self.vision_agent.page
        if not page:
            return []

        if self._run_js_include_frames:
            contexts = list(self._iter_contexts() or [])
        else:
            contexts = [("page", page)]
        if not contexts:
            return []

        prefer = str(prefer_ctx_name or self._last_js_context_name or "").strip()
        if not prefer:
            return contexts

        def _score(item):
            name = str(item[0] or "")
            if name == prefer:
                return 0
            if name == "page":
                return 1
            return 2

        contexts.sort(key=_score)
        return contexts

    def _run_js_in_contexts(self, script, *args, prefer_ctx_name=None, timeout=None, require_truthy=True):
        """在 page/frame 上下文中执行 JS，返回第一个 truthy 结果。"""
        contexts = self._ordered_contexts(prefer_ctx_name=prefer_ctx_name)
        if not contexts:
            return None, None

        total_limit = max(1, int(self._run_js_max_contexts))
        primary = contexts[:total_limit]
        timeout_s = max(0.1, float(timeout or self._run_js_timeout_seconds))

        for ctx_name, ctx in primary:
            try:
                result = ctx.run_js(script, *args, timeout=timeout_s)
                if (not require_truthy) or bool(result):
                    self._last_js_context_name = str(ctx_name or "page")
                    return ctx_name, result
            except Exception as e:
                logger.debug(f"JS执行失败(primary:{ctx_name}): {e}")
                continue

        # 仅补充尝试一个额外上下文，避免跨大量 frame 线性超时。
        if len(contexts) > total_limit:
            ctx_name, ctx = contexts[total_limit]
            fallback_timeout = max(0.1, float(self._run_js_fallback_timeout_seconds))
            try:
                result = ctx.run_js(script, *args, timeout=fallback_timeout)
                if (not require_truthy) or bool(result):
                    self._last_js_context_name = str(ctx_name or "page")
                    return ctx_name, result
            except Exception as e:
                logger.debug(f"JS执行失败(fallback:{ctx_name}): {e}")
        return None, None

    def _find_first_ele(self, selectors, preferred_ctx=None, preferred_selector=None, timeout=0.25, validator=None):
        """按顺序在 page/frame 上下文中查找第一个可用元素。"""
        contexts = self._iter_contexts()
        if preferred_ctx:
            contexts = [preferred_ctx] + [c for c in contexts if c[1] is not preferred_ctx[1]]

        ordered_selectors = list(selectors)
        if preferred_selector and preferred_selector in ordered_selectors:
            ordered_selectors = [preferred_selector] + [s for s in ordered_selectors if s != preferred_selector]

        for ctx_name, ctx in contexts:
            for selector in ordered_selectors:
                try:
                    eles = ctx.eles(selector, timeout=timeout)
                except Exception:
                    continue
                if not eles:
                    continue

                # 优先返回可交互元素；若都不可交互再降级返回第一个
                fallback = None
                for ele in eles:
                    if callable(validator):
                        try:
                            if not bool(validator(ele, selector, ctx_name)):
                                continue
                        except Exception:
                            continue
                    if fallback is None:
                        fallback = ele
                    if self._is_clickable_element(ele):
                        return (ctx_name, ctx, ele, selector)

                if fallback:
                    return (ctx_name, ctx, fallback, selector)

        return (None, None, None, None)

    def _is_clickable_element(self, ele):
        """判断元素是否具备可点击的几何信息。"""
        try:
            _ = ele.rect.mid_point
            return True
        except Exception:
            return False

    def _safe_click(self, ele, prefer_js=False):
        """点击元素；prefer_js=True 时优先 JS 点击以避免慢阻塞。"""
        self._human_action_delay("dom_click_pre")
        if self._force_full_physical_chain:
            try:
                mid = ele.rect.mid_point
                if isinstance(mid, tuple) and len(mid) >= 2:
                    human_click(int(mid[0]), int(mid[1]), jitter_px=self._human_click_jitter)
                    self._human_action_post_delay("dom_click_post_physical")
                    return True
            except Exception as e:
                logger.debug(f"物理点击失败: {e}")
            return False

        if prefer_js:
            try:
                ele.run_js("this.click && this.click();")
                self._human_action_post_delay("dom_click_post_js")
                return True
            except Exception as e:
                logger.debug(f"优先 JS 点击失败，尝试常规点击: {e}")

        try:
            ele.click()
            self._human_action_post_delay("dom_click_post")
            return True
        except Exception as e:
            logger.debug(f"常规点击失败，尝试 JS 点击: {e}")

        try:
            ele.run_js("this.click && this.click();")
            self._human_action_post_delay("dom_click_post_js_fallback")
            return True
        except Exception as e:
            logger.debug(f"JS 点击失败: {e}")
            return False

    def _log_chat_debug_candidates(self):
        """发送失败时打印候选输入元素，便于定位页面结构变化。"""
        probe_selectors = [
            'css:textarea',
            'css:div[contenteditable="true"]',
            'css:div[contenteditable="plaintext-only"]',
            'css:[data-e2e*="chat"]',
            'css:[data-e2e*="composer"]',
            'css:[data-e2e*="message"]',
        ]

        for ctx_name, ctx in self._iter_contexts():
            for selector in probe_selectors:
                try:
                    eles = ctx.eles(selector, timeout=0.3)
                except Exception:
                    continue
                if not eles:
                    continue
                logger.info(f"发送调试: {ctx_name} 命中 {selector} -> {len(eles)} 个")
                for ele in eles[:3]:
                    try:
                        logger.info(
                            "发送调试元素: tag={} data-e2e={} role={} aria={} class={} text={}".format(
                                ele.tag,
                                ele.attr('data-e2e'),
                                ele.attr('role'),
                                ele.attr('aria-label'),
                                (ele.attr('class') or '')[:80],
                                (ele.text or '').strip().replace("\n", " ")[:80]
                            )
                        )
                    except Exception:
                        continue

    def _find_send_button(self, preferred_ctx):
        send_selectors = [
            'css:[data-e2e="chat-send-button"]',
            'css:[data-e2e*="chat-send"]',
            'css:[data-e2e*="send-button"]',
            'css:[data-e2e*="send"]',
            'css:[data-e2e*="post"]',
            'css:[aria-label*="Send"]',
            'css:[aria-label*="send"]',
            'css:[aria-label*="发送"]',
            'css:button[type="submit"]',
        ]
        ctx_name, ctx, ele, selector = self._find_first_ele(
            send_selectors,
            preferred_ctx=preferred_ctx,
            preferred_selector=self._last_send_selector,
            timeout=self._fast_find_timeout,
        )
        if ele:
            self._last_send_selector = selector
        return (ctx_name, ctx, ele, selector)

    def _is_chat_input_element(self, ele, selector="", ctx_name=""):
        """过滤掉搜索框/通用文本框，尽量只命中聊天输入框。"""
        selector_text = str(selector or "").strip().lower()
        strong_selector_tokens = [
            'chat-input',
            'data-e2e*="chat"',
            'data-e2e*="comment"',
            'data-e2e*="composer"',
            'placeholder*="chat"',
            'placeholder*="message"',
            'placeholder*="comment"',
            'aria-label*="chat"',
            'aria-label*="message"',
            'aria-label*="comment"',
        ]
        if any(token in selector_text for token in strong_selector_tokens):
            return True

        attrs = []
        for key in ("data-e2e", "role", "aria-label", "placeholder", "class", "id", "name"):
            try:
                attrs.append(str(ele.attr(key) or ""))
            except Exception:
                attrs.append("")
        blob = " ".join(attrs).strip().lower()
        if not blob:
            return False

        negative_tokens = [
            "search",
            "keyword",
            "query",
            "password",
            "email",
            "phone",
            "username",
            "title",
            "caption",
            "note",
            "memo",
        ]
        if any(token in blob for token in negative_tokens):
            return False

        positive_tokens = [
            "chat",
            "comment",
            "message",
            "composer",
            "danmu",
            "弹幕",
            "聊天",
            "评论",
            "留言",
        ]
        if any(token in blob for token in positive_tokens):
            return True

        try:
            in_chat_container = bool(
                ele.run_js(
                    """
                    let cur = this;
                    let depth = 0;
                    while (cur && depth < 6) {
                      const dataE2E = (cur.getAttribute && cur.getAttribute('data-e2e')) || '';
                      const cls = (cur.className || '');
                      const aria = (cur.getAttribute && cur.getAttribute('aria-label')) || '';
                      const idv = (cur.id || '');
                      const all = `${dataE2E} ${cls} ${aria} ${idv}`.toLowerCase();
                      if (/chat|comment|message|composer|danmu|弹幕|聊天|评论/.test(all)) return true;
                      cur = cur.parentElement;
                      depth += 1;
                    }
                    return false;
                    """
                )
            )
            if in_chat_container:
                return True
        except Exception:
            pass

        return False

    def _find_input_box(self):
        input_selectors = [
            'css:[data-e2e="chat-input"] textarea',
            'css:[data-e2e*="chat-input"] textarea',
            'css:[data-e2e*="chat"] textarea',
            'css:[data-e2e*="composer"] textarea',
            'css:[data-e2e*="message"] textarea',
            'css:textarea[data-e2e*="chat"]',
            'css:textarea[data-e2e*="comment"]',
            'css:textarea[data-e2e*="message"]',
            'css:[data-e2e*="comment"] textarea',
            'css:[data-e2e*="chat"] [role="textbox"]',
            'css:[data-e2e*="composer"] [role="textbox"]',
            'css:[data-e2e*="comment"] [role="textbox"]',
            'css:div[data-e2e*="chat"][contenteditable="true"]',
            'css:div[data-e2e*="chat"][contenteditable="plaintext-only"]',
            'css:div[data-e2e*="comment"][contenteditable="true"]',
            'css:div[data-e2e*="comment"][contenteditable="plaintext-only"]',
            'css:textarea[placeholder*="message" i]',
            'css:textarea[placeholder*="chat" i]',
            'css:textarea[placeholder*="comment" i]',
            'css:[role="textbox"][aria-label*="message" i]',
            'css:[role="textbox"][aria-label*="chat" i]',
            'css:[role="textbox"][aria-label*="comment" i]',
        ]
        ctx_name, ctx, ele, selector = self._find_first_ele(
            input_selectors,
            preferred_selector=self._last_input_selector,
            timeout=self._fast_find_timeout,
            validator=self._is_chat_input_element,
        )
        if not ele:
            ctx_name, ctx, ele, selector = self._find_first_ele(
                input_selectors,
                preferred_selector=self._last_input_selector,
                timeout=self._full_find_timeout,
                validator=self._is_chat_input_element,
            )
        if not ele:
            # 兜底：慢速再扫一遍，容忍 TikTok 动态加载抖动
            ctx_name, ctx, ele, selector = self._find_first_ele(
                input_selectors,
                preferred_selector=self._last_input_selector,
                timeout=1.2,
                validator=self._is_chat_input_element,
            )
        if ele:
            self._last_input_selector = selector
        return (ctx_name, ctx, ele, selector)

    def _submit_by_enter(self, ctx, input_box, prefer_keyboard=False):
        """无发送按钮时尝试通过回车发送。"""
        try:
            input_box.run_js("this.focus && this.focus();")
        except Exception:
            pass

        if prefer_keyboard:
            try:
                self._safe_click(input_box, prefer_js=True)
            except Exception:
                pass
            try:
                input_box.run_js("this.focus && this.focus();")
            except Exception:
                pass
            try:
                human_press("enter")
                return True
            except Exception:
                pass
            try:
                ctx.actions.type('\n')
                return True
            except Exception:
                pass
            if self._force_full_physical_chain:
                return False
        if self._force_full_physical_chain:
            return False
        try:
            # 先尝试通过键盘事件触发发送（优先于纯换行）
            ok = input_box.run_js(
                """
                this.focus && this.focus();
                const evt = (type) => new KeyboardEvent(type, {
                    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
                });
                this.dispatchEvent(evt('keydown'));
                this.dispatchEvent(evt('keypress'));
                this.dispatchEvent(evt('keyup'));
                return true;
                """
            )
            if ok:
                return True
        except Exception:
            pass
        try:
            # 次选：输入换行
            input_box.input('\n')
            return True
        except Exception:
            pass
        if self._os_keyboard_fallback_enabled or self._message_keyboard_only_enabled:
            try:
                human_press("enter")
                return True
            except Exception:
                pass
        try:
            # 某些页面需要在上下文 actions 中发送按键
            ctx.actions.type('\n')
            return True
        except Exception:
            return False

    def _click_send_by_js(self, input_box):
        """
        在输入框附近通过 JS 寻找并点击“发送”按钮，适配非标准 button 结构。
        """
        if self._force_full_physical_chain:
            return False
        try:
            return bool(input_box.run_js(
                """
                const inputEl = this;
                const isVisible = (el) => {
                  if (!el) return false;
                  const s = window.getComputedStyle(el);
                  if (!s || s.display === 'none' || s.visibility === 'hidden') return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 8 && r.height > 8;
                };
                const ir = inputEl.getBoundingClientRect();
                const inputCY = (ir.top + ir.bottom) / 2;
                const root = inputEl.closest('[data-e2e*=\"chat\"], [data-e2e*=\"comment\"], form') || document;
                const candidates = Array.from(
                  root.querySelectorAll('[data-e2e*=\"send\"], [data-e2e*=\"post\"], button, [role=\"button\"], [aria-label*=\"发送\"], [aria-label*=\"Send\"]')
                );
                let best = null;
                let bestScore = -1e9;
                for (const el of candidates) {
                  if (!isVisible(el)) continue;
                  const r = el.getBoundingClientRect();
                  const text = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('data-e2e') || '')).toLowerCase();
                  let score = 0;
                  if (text.includes('send') || text.includes('发送') || text.includes('post')) score += 8;
                  if (r.left >= ir.left - 10) score += 2;
                  const cy = (r.top + r.bottom) / 2;
                  const dy = Math.abs(cy - inputCY);
                  score += Math.max(0, 4 - dy / 30);
                  const dx = Math.abs(r.left - ir.right);
                  score += Math.max(0, 4 - dx / 40);
                  if (score > bestScore) {
                    bestScore = score;
                    best = el;
                  }
                }
                if (!best) return false;
                try {
                  if (typeof best.click === 'function') {
                    best.click();
                    return true;
                  }
                } catch (e) {}
                try {
                  return !!best.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                } catch (e) {
                  return false;
                }
                """
            ))
        except Exception:
            return False

    def _sanitize_outgoing_text(self, text):
        """清洗异常按键残留，避免把控制字符发到公屏。"""
        text = (text or "")
        text = text.replace("\r", " ").replace("\n", " ")
        text = text.replace("⌘a⌫", "").replace("Ctrl+a", "").replace("ctrl+a", "")
        text = re.sub(r"^(?:\s*[⌘⌥⌃⇧⌫⎋]+)+", "", text)
        text = re.sub(r"^(?:\s*(?:cmd|command|meta|ctrl|control)\s*\+\s*a\s*)+", "", text, flags=re.IGNORECASE)
        text = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    def _submit_by_form(self, input_box):
        """优先触发 form 提交事件，兼容依赖 submit 事件的页面。"""
        if self._force_full_physical_chain:
            return False
        try:
            return bool(input_box.run_js(
                """
                const inputEl = this;
                const form = inputEl.closest('form');
                if (!form) return false;
                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                if (typeof form.requestSubmit === 'function') {
                  try { form.requestSubmit(); } catch (e) {}
                }
                return true;
                """
            ))
        except Exception:
            return False

    def _read_input_text(self, input_box):
        try:
            value = input_box.attr("value")
            if value:
                return str(value).strip()
        except Exception:
            pass
        try:
            txt = input_box.run_js(
                """
                const el = this;
                const ownText = (el.innerText || el.textContent || '');
                const value = (typeof el.value === 'string') ? el.value : '';
                const nested = el.querySelector && el.querySelector('[contenteditable="true"], [contenteditable="plaintext-only"]');
                const nestedText = nested ? (nested.innerText || nested.textContent || '') : '';
                return String(value || ownText || nestedText || '').trim();
                """
            )
            if txt:
                return str(txt).strip()
        except Exception:
            pass
        try:
            txt = input_box.text
            if txt:
                return txt.strip()
        except Exception:
            pass
        return ""

    def _input_text_by_keyboard(self, input_box, text, reason=""):
        mode = self._normalize_keyboard_input_mode(self._message_keyboard_input_mode)
        self._human_action_delay(reason or "keyboard_input_pre")
        try:
            mid = input_box.rect.mid_point
            if isinstance(mid, tuple) and len(mid) >= 2:
                human_click(int(mid[0]), int(mid[1]), jitter_px=self._human_click_jitter)
        except Exception:
            self._safe_click(input_box)
        try:
            input_box.run_js("this.focus && this.focus();")
        except Exception:
            pass
        self._clear_input_box_content(
            input_box,
            allow_system_hotkey=((platform.system() or "").lower() == "darwin"),
        )
        pasted = False
        if mode in {"paste", "auto"}:
            # auto: 长文本优先粘贴，短文本优先手打。
            if mode == "paste" or len(str(text or "")) >= 28:
                pasted = bool(human_paste(text))
        if (not pasted) and text:
            human_typewrite(
                text,
                min_interval=self._os_key_min_interval,
                max_interval=self._os_key_max_interval,
            )
        self._human_action_post_delay(reason or "keyboard_input_post")
        typed = self._read_input_text(input_box)
        return self._looks_unsent(typed, text)

    def _clear_input_box_content(self, input_box, allow_system_hotkey=False):
        try:
            ok = bool(
                input_box.run_js(
                    """
                    const el = this;
                    try { el.focus && el.focus(); } catch (e) {}
                    if (el.isContentEditable) {
                      el.innerText = '';
                      el.textContent = '';
                    } else if ('value' in el) {
                      el.value = '';
                    } else {
                      return false;
                    }
                    try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
                    try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
                    return true;
                    """
                )
            )
            if ok:
                return True
        except Exception:
            pass
        if allow_system_hotkey:
            try:
                human_select_all_and_delete()
                return True
            except Exception:
                return False
        return False

    def _input_text(self, input_box, text):
        """兼容 textarea/contenteditable 的输入写入。"""
        if self._message_keyboard_only_enabled:
            try:
                if self._input_text_by_keyboard(input_box, text, reason="keyboard_only_input"):
                    return
                logger.warning("键盘输入未命中输入框，回退DOM输入")
            except Exception as e:
                logger.warning(f"键盘输入失败，回退DOM输入: {e}")
            if self._force_full_physical_chain:
                return
        try:
            input_box.run_js("this.focus && this.focus();")
        except Exception:
            pass

        self._safe_click(input_box)

        # 优先用 JS 清空，避免快捷键文本被输入到聊天框
        try:
            input_box.run_js(
                """
                if (this.isContentEditable) {
                  this.innerText = '';
                  this.textContent = '';
                } else if ('value' in this) {
                  this.value = '';
                }
                this.dispatchEvent(new Event('input', { bubbles: true }));
                """
            )
        except Exception:
            pass

        try:
            input_box.input(text)
        except Exception:
            # 最后降级：直接写入 DOM 值并派发 input 事件
            input_box.run_js(
                "if (this.isContentEditable) { this.innerText = arguments[0]; }"
                "else if ('value' in this) { this.value = arguments[0]; }"
                "this.dispatchEvent(new Event('input', {bubbles:true}));",
                text
            )

        typed = self._read_input_text(input_box)
        if self._looks_unsent(typed, text):
            return

        if not self._os_keyboard_fallback_enabled:
            return

        # DOM 输入结果不符合预期时，回退到系统键盘逐字符输入
        try:
            self._human_action_delay("os_keyboard_fallback_pre")
            try:
                mid = input_box.rect.mid_point
                if isinstance(mid, tuple) and len(mid) >= 2:
                    human_click(int(mid[0]), int(mid[1]), jitter_px=self._human_click_jitter)
            except Exception:
                pass
            self._clear_input_box_content(
                input_box,
                allow_system_hotkey=((platform.system() or "").lower() == "darwin"),
            )
            human_typewrite(
                text,
                min_interval=self._os_key_min_interval,
                max_interval=self._os_key_max_interval,
            )
            self._human_action_post_delay("os_keyboard_fallback_post")
        except Exception as e:
            logger.debug(f"系统键盘回退输入失败: {e}")

    def _looks_unsent(self, value, expected_text):
        """输入框仍保留本次消息，判定为未真正发送。"""
        val = (value or "").strip()
        exp = (expected_text or "").strip()
        if not val:
            return False
        if not exp:
            return True
        if val == exp:
            return True
        probe = exp[: max(8, min(20, len(exp)))]
        if probe and probe in val:
            return True
        if len(val) >= max(10, int(len(exp) * 0.7)) and val in exp:
            return True
        return False

    def _confirm_sent(self, input_box, expected_text, retries=3, interval=0.08):
        """发送后验收：输入框应被消费/清空，否则按失败处理。"""
        for _ in range(max(1, retries)):
            time.sleep(max(0.02, interval))
            after_value = self._read_input_text(input_box)
            if not self._looks_unsent(after_value, expected_text):
                return True
        logger.debug("发送后输入框仍保留消息文本，判定未成功发送")
        return False

    def _send_message_once(self, text):
        text = self._sanitize_outgoing_text(text)
        if not text:
            return False
        self._human_action_delay("send_message_pre")

        input_ctx_name, input_ctx, input_box, input_selector = self._find_input_box()
        if not input_box:
            logger.warning("未找到聊天输入框，发送失败")
            self._log_chat_debug_candidates()
            return False

        logger.debug(f"发送消息命中输入框上下文: {input_ctx_name} selector: {input_selector}")
        self._input_text(input_box, text)
        time.sleep(random.uniform(0.01, 0.03))

        if self._message_keyboard_only_enabled:
            enter_ok = self._submit_by_enter(input_ctx, input_box, prefer_keyboard=True)
            if enter_ok and self._confirm_sent(input_box, text, retries=4, interval=0.06):
                self._human_action_post_delay("send_message_post_keyboard_enter")
                return True
            logger.warning("键盘回车未确认发送，回退按钮/表单提交链路")

        # 先走 JS 快速发送，避免元素 click 的长等待阻塞
        if (not self._force_full_physical_chain) and self._click_send_by_js(input_box):
            if self._confirm_sent(input_box, text, retries=1, interval=0.04):
                self._human_action_post_delay("send_message_post_js")
                return True

        send_ctx_name, _, send_btn, send_selector = self._find_send_button((input_ctx_name, input_ctx))
        if send_btn:
            logger.debug(f"发送消息命中发送按钮上下文: {send_ctx_name} selector: {send_selector}")
            clicked = self._safe_click(send_btn, prefer_js=(not self._force_full_physical_chain))
            if clicked and self._confirm_sent(input_box, text, retries=2, interval=0.05):
                self._human_action_post_delay("send_message_post_button")
                return True

        if (not self._force_full_physical_chain) and self._submit_by_form(input_box):
            if self._confirm_sent(input_box, text, retries=1, interval=0.04):
                self._human_action_post_delay("send_message_post_form")
                return True

        enter_ok = self._submit_by_enter(input_ctx, input_box, prefer_keyboard=self._message_keyboard_only_enabled)
        if not enter_ok:
            return False

        ok = self._confirm_sent(input_box, text, retries=3, interval=0.05)
        if ok:
            self._human_action_post_delay("send_message_post_enter")
        return ok

    def _requires_foreground_guard_for_message_send(self):
        return bool(
            self._message_keyboard_only_enabled
            or self._force_full_physical_chain
            or self._os_keyboard_fallback_enabled
        )

    def _looks_like_tiktok_live_url(self, url, title=""):
        url_text = str(url or "").strip().lower()
        title_text = str(title or "").strip().lower()
        if "mock_tiktok_shop" in url_text:
            return True
        if ("tiktok.com" not in url_text) and ("tiktok" not in title_text):
            return False
        live_tokens = [
            "/live",
            "/@",
            "live/dashboard",
            "streamer/live",
            "creator/live",
        ]
        if any(token in url_text for token in live_tokens):
            return True
        if any(token in title_text for token in ["tiktok live", "直播", "live"]):
            return True
        return False

    def _get_frontmost_window_snapshot(self):
        if (platform.system() or "").lower() != "darwin":
            return {"ok": False, "error": "unsupported_platform"}
        script = (
            'tell application "System Events"\n'
            'set frontProc to first application process whose frontmost is true\n'
            'set appName to name of frontProc\n'
            'set winTitle to ""\n'
            'try\n'
            'tell frontProc to set winTitle to name of front window\n'
            'end try\n'
            'return appName & "||" & winTitle\n'
            "end tell"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=1.2,
                check=False,
            )
        except Exception as e:
            return {"ok": False, "error": f"osascript_exception:{e}"}
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:180]
            return {"ok": False, "error": f"osascript_failed:{err}"}
        out = str(proc.stdout or "").strip()
        app_name, sep, title = out.partition("||")
        if not sep:
            return {"ok": False, "error": "osascript_bad_output", "raw": out[:120]}
        return {
            "ok": True,
            "app": app_name.strip(),
            "title": title.strip(),
        }

    def _get_frontmost_tab_url(self, app_name):
        app = str(app_name or "").strip()
        if not app:
            return ""
        if app in {"Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Arc"}:
            script = f'tell application "{app}" to return URL of active tab of front window'
        elif app == "Safari":
            script = 'tell application "Safari" to return URL of front document'
        else:
            return ""
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        return str(proc.stdout or "").strip()

    def _is_message_page_context_allowed(self, ctx):
        page_type = str((ctx or {}).get("page_type") or "").strip().lower()
        return page_type in {"tiktok_live_room", "tiktok_live_dashboard"}

    def _precheck_message_send(self):
        if not self.vision_agent.page:
            return False, "browser_not_connected", {}

        try:
            ctx = dict(self.vision_agent.get_page_context() or {})
        except Exception:
            ctx = {}
        page_type_allowed = self._is_message_page_context_allowed(ctx)
        mac_foreground_guard = self._requires_foreground_guard_for_message_send() and (platform.system() or "").lower() == "darwin"
        if (not page_type_allowed) and (not mac_foreground_guard):
            return False, f"page_type_blocked:{ctx.get('page_type')}", {"page_context": ctx}

        try:
            page_title = str(getattr(self.vision_agent.page, "title", "") or "")
            page_url = str(getattr(self.vision_agent.page, "url", "") or "")
        except Exception:
            page_title, page_url = "", ""
        if not self._looks_like_tiktok_live_url(page_url, page_title):
            return False, "browser_page_not_tiktok_live", {"url": page_url, "title": page_title, "page_context": ctx}

        # 只在涉及系统级键鼠输入时启用前台窗口门禁，防止误输入到其它应用。
        if mac_foreground_guard:
            snap = self._get_frontmost_window_snapshot()
            if not bool(snap.get("ok")):
                return False, "frontmost_window_unknown", {"snapshot": snap, "page_context": ctx}
            front_app = str(snap.get("app") or "").strip()
            browser_apps = {"Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Arc", "Safari"}
            if front_app not in browser_apps:
                return False, "front_app_not_browser", {"snapshot": snap, "page_context": ctx}
            active_url = self._get_frontmost_tab_url(front_app)
            if active_url:
                if not self._looks_like_tiktok_live_url(active_url, str(snap.get("title") or "")):
                    return False, "front_tab_not_tiktok_live", {"snapshot": snap, "front_tab_url": active_url[:240]}
            elif not self._looks_like_tiktok_live_url("", str(snap.get("title") or "")):
                return False, "front_window_not_live_like", {"snapshot": snap}
            if not page_type_allowed:
                logger.info(
                    "消息发送门禁：page_type 未命中，已按前台 TikTok 直播页校验放行 "
                    f"(page_type={ctx.get('page_type')})"
                )

        if not page_type_allowed:
            return False, f"page_type_blocked:{ctx.get('page_type')}", {"page_context": ctx}

        return True, "ok", {"page_context": ctx, "url": page_url, "title": page_title}

    def can_send_message(self, log_reason=True):
        ok, reason, detail = self._precheck_message_send()
        if (not ok) and bool(log_reason):
            logger.warning(f"消息发送门禁未通过: reason={reason}, detail={str(detail)[:320]}")
        return ok

    def send_message(self, text):
        """
        发送弹幕/回复
        :param text: 回复内容
        """
        if not self.vision_agent.page:
            logger.warning("浏览器未连接，无法发送消息")
            return False

        try:
            if not text or not text.strip():
                return False

            text = self._sanitize_outgoing_text(text)
            if not text:
                return False

            if len(text) > SEND_MESSAGE_MAX_CHARS:
                text = text[:SEND_MESSAGE_MAX_CHARS]
                logger.info(f"回复已截断到 {SEND_MESSAGE_MAX_CHARS} 字符，避免输入框超限")

            elapsed = time.time() - self.last_reply_at
            if elapsed < self.reply_interval:
                time.sleep(self.reply_interval - elapsed)

            if not self.vision_agent.ensure_connection():
                logger.warning("发送前页面连接不可用")
                return False

            if not self.can_send_message(log_reason=True):
                return False

            ok = self._send_message_once(text)
            if not ok:
                # 页面可能在发送时断连，强制重连后重试一次
                if self.vision_agent.ensure_connection(force=True):
                    if self.can_send_message(log_reason=True):
                        ok = self._send_message_once(text)

            if ok:
                self.last_reply_at = time.time()
                logger.info(f"已发送回复: {text}")
                return True

            logger.warning("发送消息失败：输入或提交未成功")
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    def perform_action_by_image(self, template_name):
        """
        通过图像识别寻找按钮并点击
        :param template_name: 模板文件名 (如 'pin_btn.png')
        """
        logger.info(f"正在寻找按钮: {template_name}")
        self._human_action_delay("image_action_pre")
        coords = find_button_on_screen(self.vision_agent, template_name)
        
        if coords:
            x, y = coords
            logger.info(f"找到按钮 {template_name}，坐标: ({x}, {y})，准备点击...")
            human_click(x, y, jitter_px=self._human_click_jitter)
            self._human_action_post_delay("image_action_post")
            return True
        else:
            logger.warning(f"未找到按钮: {template_name}")
            return False

    def _ensure_action_page(self, action):
        """页面门禁：仅在可操作页执行运营动作。"""
        if self._is_screen_ocr_info_mode():
            ocr_ok, ocr_detail = self._is_ocr_operable_page(action)
            if ocr_ok:
                return True
            focused = self._focus_action_page_for_screen_ocr(action)
            if focused:
                ocr_ok_retry, ocr_detail_retry = self._is_ocr_operable_page(action)
                if ocr_ok_retry:
                    logger.info(f"screen_ocr 前台拉起后页面门禁放行: action={action}")
                    return True
                if isinstance(ocr_detail_retry, dict):
                    ocr_detail = ocr_detail_retry
            if self._pin_unpin_dom_rescue_enabled and action in {"pin_product", "unpin_product"}:
                ensure_action_page = getattr(self.vision_agent, "ensure_action_page", None)
                dom_page_ready = False
                if callable(ensure_action_page):
                    try:
                        dom_page_ready = bool(ensure_action_page(action))
                    except Exception:
                        dom_page_ready = False
                if dom_page_ready:
                    logger.warning(
                        f"screen_ocr门禁OCR未通过，已按DOM兜底放行: action={action}, reason={getattr(ocr_detail, 'get', lambda *_: '')('reason') if isinstance(ocr_detail, dict) else ''}"
                    )
                    return True
            self._set_action_receipt(action, False, "precheck", "screen_ocr_non_operable_page", ocr_detail)
            logger.warning(
                f"执行动作失败: action={action}, screen_ocr门禁未通过, reason={getattr(ocr_detail, 'get', lambda *_: '')('reason') if isinstance(ocr_detail, dict) else ''}"
            )
            return False

        if not self.vision_agent.page:
            self._set_action_receipt(action, False, "precheck", "browser_not_connected")
            logger.warning(f"执行动作失败: action={action}, 浏览器未连接")
            return False

        # 纯 OCR 门禁：不走 DOM 页面判定（适用于 ocr_only/screen_ocr，或 ocr_vision 且禁用 DOM 回退）。
        ocr_gate_only = (self._is_ocr_info_only_mode() and (not self._dom_execution_enabled())) or (
            self._is_ocr_vision_mode() and (not self._dom_fallback_enabled())
        )
        if ocr_gate_only:
            try:
                ctx = self.vision_agent.get_page_context() or {}
            except Exception:
                ctx = {}
            if self._is_ctx_operable_for_action(action, ctx) and (not self._is_pin_unpin_action(action)):
                return True
            ocr_ok, ocr_detail = self._is_ocr_operable_page(action)
            if ocr_ok:
                return True
            if self._pin_unpin_dom_rescue_enabled and action in {"pin_product", "unpin_product"}:
                ensure_action_page = getattr(self.vision_agent, "ensure_action_page", None)
                dom_page_ready = False
                if callable(ensure_action_page):
                    try:
                        dom_page_ready = bool(ensure_action_page(action))
                    except Exception:
                        dom_page_ready = False
                if dom_page_ready:
                    logger.warning(
                        f"OCR门禁未通过，已按DOM兜底放行: action={action}, reason={getattr(ocr_detail, 'get', lambda *_: '')('reason') if isinstance(ocr_detail, dict) else ''}"
                    )
                    return True
            reason = "ocr_non_operable_page" if self._is_ocr_info_only_mode() else "ocr_vision_non_operable_page"
            self._set_action_receipt(action, False, "precheck", reason, ocr_detail)
            logger.warning(
                f"执行动作失败: action={action}, OCR门禁未通过, reason={getattr(ocr_detail, 'get', lambda *_: '')('reason') if isinstance(ocr_detail, dict) else ''}"
            )
            return False

        dom_ok = self.vision_agent.ensure_action_page(action)
        if dom_ok:
            return True

        # OCR 模式下：若 OCR 能确认页面可操作，允许放行（兼容页面结构变化）。
        if self._is_ocr_vision_mode():
            ocr_ok, ocr_detail = self._is_ocr_operable_page(action)
            if ocr_ok:
                logger.info(f"OCR 页面门禁放行: action={action}")
                return True
            # OCR 不可用时，保留原有行为：按 DOM 门禁失败处理。
            if isinstance(ocr_detail, dict) and ocr_detail.get("reason") not in {"ocr_unavailable"}:
                self._set_action_receipt(action, False, "precheck", "ocr_non_operable_page", ocr_detail)
                logger.warning(
                    f"执行动作失败: action={action}, OCR判定不可操作, reason={ocr_detail.get('reason')}"
                )
                return False

        if not dom_ok:
            ctx = self.vision_agent.get_page_context()
            reason = f"non_operable_page:{ctx.get('page_type')}"
            self._set_action_receipt(action, False, "precheck", reason, ctx)
            logger.warning(
                f"执行动作失败: action={action}, 当前页不可操作, "
                f"page_type={ctx.get('page_type')}, url={ctx.get('url')}"
            )
            return False
        return True

    def _verify_pin_receipt(self, link_index=None, timeout_seconds=2.0):
        """置顶动作成功回执校验：检测已置顶/取消置顶状态或成功提示。"""
        script = """
        const idx = Number(arguments[0] || 0);
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const text = norm((document.body && document.body.innerText) || '');
        const parseNum = (v) => {
          const n = Number(v);
          return Number.isFinite(n) && n > 0 ? n : null;
        };
        const rowMatchesIndex = (row, idx) => {
          if (!(idx > 0)) return true;
          const attrVals = [
            row.getAttribute && row.getAttribute('data-index'),
            row.getAttribute && row.getAttribute('data-product-index'),
            row.getAttribute && row.getAttribute('data-id'),
            row.dataset && row.dataset.index,
            row.dataset && row.dataset.productIndex,
            row.dataset && row.dataset.id,
          ];
          for (const v of attrVals) {
            const n = parseNum(v);
            if (n === idx) return true;
          }
          const raw = String(row.innerText || row.textContent || '').toLowerCase();
          const patterns = [
            new RegExp(`序号\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`第\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`${idx}\\\\s*号\\\\s*(?:链?接|连?接|商品|橱窗)`),
            new RegExp(`(?:link|item|product)\\\\s*(?:no\\\\.?|number)?\\\\s*#?\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`#\\\\s*${idx}(?!\\\\d)`),
          ];
          return patterns.some(re => re.test(raw));
        };

        const successHints = ['置顶成功','pinnedsuccess','pinsuccessful','pinnedsuccessfully'];
        if (successHints.some(h => text.includes(h))) {
          return {ok:true, source:'toast'};
        }

        const pinnedHints = ['已置顶', '取消置顶', 'unpin', 'pinned'];
        const rows = Array.from(document.querySelectorAll('tr,li,[data-e2e*="product"],[class*="product"],[class*="item"],div'));
        for (const row of rows) {
          if (!rowMatchesIndex(row, idx)) continue;
          const rt = norm(row.innerText || row.textContent || '');
          if (pinnedHints.some(h => rt.includes(norm(h)))) {
            return {ok:true, source:'row_state'};
          }
        }

        // 无序号场景兜底：页面出现“取消置顶/unpin”按钮即可视为完成
        const btns = Array.from(document.querySelectorAll('button,[role="button"],div[role="button"],span[role="button"]'));
        const btnText = btns.map(b => norm(b.innerText || b.textContent || b.getAttribute('aria-label') || '')).join(' ');
        if (btnText.includes('取消置顶') || btnText.includes('unpin')) {
          return {ok:true, source:'button_state'};
        }

        return null;
        """
        deadline = time.time() + max(0.5, timeout_seconds)
        while time.time() < deadline:
            _, result = self._run_js_in_contexts(script, int(link_index or 0))
            if result and result.get("ok"):
                return result
            time.sleep(random.uniform(0.11, 0.24))
        return None

    def _verify_flash_sale_receipt(self, timeout_seconds=2.4):
        """秒杀上架成功回执校验：检测成功提示或活动进行中状态。"""
        script = """
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const text = norm((document.body && document.body.innerText) || '');

        const successHints = [
          '秒杀上架成功', '活动上架成功', '秒杀已开启',
          'flashsalestarted', 'flashsaleislive', 'promostarted', 'launchsuccess'
        ];
        if (successHints.some(h => text.includes(norm(h)))) {
          return {ok:true, source:'toast'};
        }

        const stateHints = [
          '结束秒杀', '停止秒杀', '秒杀进行中', '秒杀中',
          'endflash', 'stopflash', 'flashlive', 'running'
        ];
        if (stateHints.some(h => text.includes(norm(h)))) {
          return {ok:true, source:'state'};
        }

        return null;
        """
        deadline = time.time() + max(0.6, timeout_seconds)
        while time.time() < deadline:
            _, result = self._run_js_in_contexts(script)
            if result and result.get("ok"):
                return result
            time.sleep(random.uniform(0.11, 0.24))
        return None

    def _verify_stop_flash_sale_receipt(self, timeout_seconds=2.4):
        """秒杀结束成功回执校验：检测结束提示或恢复到可上架状态。"""
        script = """
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const text = norm((document.body && document.body.innerText) || '');

        const successHints = [
          '结束秒杀成功', '秒杀已结束', '活动已结束',
          'flashsaleended', 'promostopped', 'stopsuccess'
        ];
        if (successHints.some(h => text.includes(norm(h)))) {
          return {ok:true, source:'toast'};
        }

        // 结束后通常会回到“可上架”态（出现上架/开启按钮）
        const readyHints = [
          '秒杀活动上架', '秒杀上架', '开启秒杀', '开始秒杀',
          'launchflashsale', 'startflashsale'
        ];
        if (readyHints.some(h => text.includes(norm(h)))) {
          return {ok:true, source:'ready_state'};
        }

        // 若仍处于进行中（存在结束按钮），说明未结束成功
        const runningHints = ['结束秒杀', '停止秒杀', '秒杀进行中', '秒杀中', 'stopflash', 'endflash'];
        if (runningHints.some(h => text.includes(norm(h)))) {
          return null;
        }

        return null;
        """
        deadline = time.time() + max(0.6, timeout_seconds)
        while time.time() < deadline:
            _, result = self._run_js_in_contexts(script)
            if result and result.get("ok"):
                return result
            time.sleep(random.uniform(0.11, 0.24))
        return None

    def _verify_unpin_receipt(self, link_index=None, timeout_seconds=2.0):
        """取消置顶成功回执校验。"""
        script = """
        const idx = Number(arguments[0] || 0);
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const text = norm((document.body && document.body.innerText) || '');
        const parseNum = (v) => {
          const n = Number(v);
          return Number.isFinite(n) && n > 0 ? n : null;
        };
        const rowMatchesIndex = (row, idx) => {
          if (!(idx > 0)) return true;
          const attrVals = [
            row.getAttribute && row.getAttribute('data-index'),
            row.getAttribute && row.getAttribute('data-product-index'),
            row.getAttribute && row.getAttribute('data-id'),
            row.dataset && row.dataset.index,
            row.dataset && row.dataset.productIndex,
            row.dataset && row.dataset.id,
          ];
          for (const v of attrVals) {
            const n = parseNum(v);
            if (n === idx) return true;
          }
          const raw = String(row.innerText || row.textContent || '').toLowerCase();
          const patterns = [
            new RegExp(`序号\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`第\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`${idx}\\\\s*号\\\\s*(?:链?接|连?接|商品|橱窗)`),
            new RegExp(`(?:link|item|product)\\\\s*(?:no\\\\.?|number)?\\\\s*#?\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`#\\\\s*${idx}(?!\\\\d)`),
          ];
          return patterns.some(re => re.test(raw));
        };
        const successHints = ['已取消置顶','取消置顶成功','unpinned','unpinsuccess'];
        if (successHints.some(h => text.includes(norm(h)))) {
          return {ok:true, source:'toast'};
        }

        const btnSel = 'button,[role="button"],div[role="button"],span[role="button"]';
        const txt = (el) => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
        const unpinWords = ['取消置顶','unpin','pinned'];
        const pinWords = ['置顶','pin','top'];
        const isUnpinBtn = (el) => unpinWords.some(w => txt(el).includes(norm(w)));
        const isPinBtn = (el) => pinWords.some(w => txt(el).includes(norm(w))) && !isUnpinBtn(el);
        const isVisible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const s = window.getComputedStyle(el);
          return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
        };

        if (idx > 0) {
          const rows = Array.from(document.querySelectorAll('tr,li,[data-e2e*="product"],[class*="product"],[class*="item"],div'));
          for (const row of rows) {
            if (!rowMatchesIndex(row, idx)) continue;
            const hasUnpin = Array.from(row.querySelectorAll(btnSel)).some(b => isVisible(b) && isUnpinBtn(b));
            if (!hasUnpin) return {ok:true, source:'row_state'};
            return null;
          }
        }

        const anyUnpin = Array.from(document.querySelectorAll(btnSel)).some(b => isVisible(b) && isUnpinBtn(b));
        if (!anyUnpin || text.includes(norm('未置顶商品')) || text.includes(norm('nopinned'))) {
          return {ok:true, source:'global_state'};
        }
        return null;
        """
        deadline = time.time() + max(0.5, timeout_seconds)
        while time.time() < deadline:
            _, result = self._run_js_in_contexts(script, int(link_index or 0))
            if result and result.get("ok"):
                return result
            time.sleep(random.uniform(0.11, 0.24))
        return None

    def _pin_product_by_dom(self, link_index=None, force=False):
        """
        优先基于 DOM 执行置顶动作。
        link_index: 1-based 链接序号（例如 3 表示 3 号链接）
        """
        if (not force) and (not self._dom_fallback_enabled()):
            return False
        script = """
        const idx = Number(arguments[0] || 0);
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const parseNum = (v) => {
          const n = Number(v);
          return Number.isFinite(n) && n > 0 ? n : null;
        };
        const isVisible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const s = window.getComputedStyle(el);
          return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const click = (el) => {
          if (!el || !isVisible(el)) return false;
          el.scrollIntoView({block:'center', inline:'center'});
          try {
            if (typeof el.click === 'function') {
              el.click();
              return true;
            }
          } catch (e) {}
          try {
            return !!el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
          } catch (e) {
            return false;
          }
        };
        const btnSel = 'button,[role="button"],div[role="button"],span[role="button"]';
        const pinWords = ['置顶','pin','top'];
        const pinnedWords = ['取消置顶','已置顶','unpin','pinned'];
        const txt = (el) => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
        const isPinnedCtl = (el) => pinnedWords.some(w => txt(el).includes(norm(w)));
        const isPinCtl = (el) => pinWords.some(w => txt(el).includes(w)) && !isPinnedCtl(el);
        const rowMatchesIndex = (row, idx) => {
          if (!(idx > 0)) return true;
          const attrVals = [
            row.getAttribute && row.getAttribute('data-index'),
            row.getAttribute && row.getAttribute('data-product-index'),
            row.getAttribute && row.getAttribute('data-id'),
            row.dataset && row.dataset.index,
            row.dataset && row.dataset.productIndex,
            row.dataset && row.dataset.id,
          ];
          for (const v of attrVals) {
            const n = parseNum(v);
            if (n === idx) return true;
          }
          const raw = String(row.innerText || row.textContent || '').toLowerCase();
          const patterns = [
            new RegExp(`序号\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`第\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`${idx}\\\\s*号\\\\s*(?:链?接|连?接|商品|橱窗)`),
            new RegExp(`(?:link|item|product)\\\\s*(?:no\\\\.?|number)?\\\\s*#?\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`#\\\\s*${idx}(?!\\\\d)`),
          ];
          return patterns.some(re => re.test(raw));
        };

        if (idx > 0) {
          const directBtn = Array.from(document.querySelectorAll(`[data-product-index="${idx}"]`))
            .find(b => isVisible(b) && (isPinnedCtl(b) || isPinCtl(b)));
          if (directBtn) {
            if (isPinnedCtl(directBtn)) return {ok:true, mode:'direct', idx, already:true};
            if (click(directBtn)) return {ok:true, mode:'direct', idx, clicked:true};
          }

          const rows = Array.from(document.querySelectorAll('tr,li,[data-e2e*="product"],[class*="product"],[class*="item"],div'));
          for (const row of rows) {
            if (!rowMatchesIndex(row, idx)) continue;
            const pinnedBtn = Array.from(row.querySelectorAll(btnSel)).find(b => isVisible(b) && isPinnedCtl(b));
            if (pinnedBtn) return {ok:true, mode:'row', idx, already:true};
            const btn = Array.from(row.querySelectorAll(btnSel)).find(b => isVisible(b) && isPinCtl(b));
            if (btn && click(btn)) return {ok:true, mode:'row', idx, clicked:true};
          }
          return null;
        }

        const globalBtn = Array.from(document.querySelectorAll(btnSel)).find(b => isVisible(b) && isPinCtl(b));
        if (globalBtn && click(globalBtn)) return {ok:true, mode:'global', idx, clicked:true};
        return null;
        """
        self._human_action_delay("pin_dom_exec_pre")
        ctx_name, result = self._run_js_in_contexts(script, int(link_index or 0))
        if result:
            if result.get("already"):
                logger.info(f"DOM 置顶已是目标状态: ctx={ctx_name}, result={result}")
                self._set_action_receipt("pin_product", True, "verify", "already_pinned", {"ctx": ctx_name, "result": result})
                return True

            self._human_action_post_delay("pin_dom_click_post")
            verify = self._verify_pin_receipt(link_index=link_index, timeout_seconds=2.2)
            if verify:
                logger.info(f"DOM 置顶执行成功并通过回执校验: ctx={ctx_name}, result={result}, verify={verify}")
                self._set_action_receipt("pin_product", True, "verify", "ok", {"ctx": ctx_name, "result": result, "verify": verify})
                return True
            if self._is_ocr_vision_mode():
                ocr_verify = self._verify_receipt_by_ocr("pin_product", link_index=link_index)
                if ocr_verify:
                    logger.info(f"OCR 回执确认置顶成功: {ocr_verify}")
                    self._set_action_receipt(
                        "pin_product",
                        True,
                        "verify_ocr",
                        "ok",
                        {"ctx": ctx_name, "result": result, "verify": ocr_verify},
                    )
                    return True

            logger.warning(f"DOM 置顶点击后未检测到成功回执: ctx={ctx_name}, result={result}")
            self._set_action_receipt("pin_product", False, "verify", "receipt_not_confirmed", {"ctx": ctx_name, "result": result})
            return False
        return False

    def _unpin_product_by_dom(self, link_index=None, force=False):
        """优先基于 DOM 执行取消置顶动作。"""
        if (not force) and (not self._dom_fallback_enabled()):
            return False
        script = """
        const idx = Number(arguments[0] || 0);
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const parseNum = (v) => {
          const n = Number(v);
          return Number.isFinite(n) && n > 0 ? n : null;
        };
        const isVisible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const s = window.getComputedStyle(el);
          return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const click = (el) => {
          if (!el || !isVisible(el)) return false;
          el.scrollIntoView({block:'center', inline:'center'});
          try {
            if (typeof el.click === 'function') {
              el.click();
              return true;
            }
          } catch (e) {}
          try {
            return !!el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
          } catch (e) {
            return false;
          }
        };
        const btnSel = 'button,[role="button"],div[role="button"],span[role="button"]';
        const txt = (el) => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
        const isUnpinBtn = (el) => ['取消置顶','unpin','pinned'].some(w => txt(el).includes(norm(w)));
        const rowMatchesIndex = (row, idx) => {
          if (!(idx > 0)) return true;
          const attrVals = [
            row.getAttribute && row.getAttribute('data-index'),
            row.getAttribute && row.getAttribute('data-product-index'),
            row.getAttribute && row.getAttribute('data-id'),
            row.dataset && row.dataset.index,
            row.dataset && row.dataset.productIndex,
            row.dataset && row.dataset.id,
          ];
          for (const v of attrVals) {
            const n = parseNum(v);
            if (n === idx) return true;
          }
          const raw = String(row.innerText || row.textContent || '').toLowerCase();
          const patterns = [
            new RegExp(`序号\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`第\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`${idx}\\\\s*号\\\\s*(?:链?接|连?接|商品|橱窗)`),
            new RegExp(`(?:link|item|product)\\\\s*(?:no\\\\.?|number)?\\\\s*#?\\\\s*${idx}(?!\\\\d)`),
            new RegExp(`#\\\\s*${idx}(?!\\\\d)`),
          ];
          return patterns.some(re => re.test(raw));
        };

        if (idx > 0) {
          const directBtn = Array.from(document.querySelectorAll(`[data-product-index="${idx}"]`))
            .find(b => isVisible(b) && isUnpinBtn(b));
          if (directBtn && click(directBtn)) return {ok:true, mode:'direct', idx, clicked:true};

          const rows = Array.from(document.querySelectorAll('tr,li,[data-e2e*="product"],[class*="product"],[class*="item"],div'));
          for (const row of rows) {
            if (!rowMatchesIndex(row, idx)) continue;
            const btn = Array.from(row.querySelectorAll(btnSel)).find(b => isVisible(b) && isUnpinBtn(b));
            if (btn && click(btn)) return {ok:true, mode:'row', idx, clicked:true};
            return {ok:true, mode:'row', idx, already:true};
          }
          return null;
        }

        const anyBtn = Array.from(document.querySelectorAll(btnSel)).find(b => isVisible(b) && isUnpinBtn(b));
        if (anyBtn && click(anyBtn)) return {ok:true, mode:'global', clicked:true};
        return {ok:true, mode:'global', already:true};
        """
        self._human_action_delay("unpin_dom_exec_pre")
        ctx_name, result = self._run_js_in_contexts(script, int(link_index or 0))
        if result:
            if result.get("already"):
                self._set_action_receipt("unpin_product", True, "verify", "already_unpinned", {"ctx": ctx_name, "result": result})
                return True
            self._human_action_post_delay("unpin_dom_click_post")
            verify = self._verify_unpin_receipt(link_index=link_index, timeout_seconds=2.2)
            if verify:
                self._set_action_receipt("unpin_product", True, "verify", "ok", {"ctx": ctx_name, "result": result, "verify": verify})
                return True
            if self._is_ocr_vision_mode():
                ocr_verify = self._verify_receipt_by_ocr("unpin_product", link_index=link_index)
                if ocr_verify:
                    self._set_action_receipt(
                        "unpin_product",
                        True,
                        "verify_ocr",
                        "ok",
                        {"ctx": ctx_name, "result": result, "verify": ocr_verify},
                    )
                    return True
            self._set_action_receipt("unpin_product", False, "verify", "receipt_not_confirmed", {"ctx": ctx_name, "result": result})
            return False
        return False

    def _start_flash_sale_by_dom(self):
        """优先基于 DOM 执行秒杀上架动作。"""
        if not self._dom_fallback_enabled():
            return False
        script = """
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const isVisible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const s = window.getComputedStyle(el);
          return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const click = (el) => {
          if (!el || !isVisible(el)) return false;
          el.scrollIntoView({block:'center', inline:'center'});
          try {
            if (typeof el.click === 'function') {
              el.click();
              return true;
            }
          } catch (e) {}
          try {
            return !!el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
          } catch (e) {
            return false;
          }
        };
        const btnSel = 'button,[role="button"],div[role="button"],span[role="button"]';
        const words = ['秒杀','flashsale','flash','促销','活动上架','上架'];
        const startWords = ['开启','开始','上架','launch','start','open','enable','go'];
        const stopWords = ['结束秒杀','停止秒杀','下架','stop','end'];

        // Mock 场景兜底：若在未开播页，先自动点击“立即开始直播”再继续执行秒杀上架。
        try {
          const href = String(location.href || '').toLowerCase();
          if (href.includes('mock_tiktok_shop=1')) {
            const liveState = norm((document.querySelector('#liveStateText') || {}).textContent || '');
            const startLiveBtn = document.querySelector('#btnStartLive');
            const startLiveVisible = isVisible(startLiveBtn);
            const seemsNotLive = liveState.includes('未开播') || (
              startLiveBtn && startLiveBtn.disabled === false
            );
            if (seemsNotLive && startLiveVisible) {
              click(startLiveBtn);
            }
          }
        } catch (e) {}

        const btn = Array.from(document.querySelectorAll(btnSel)).find(el => {
          if (!isVisible(el)) return false;
          const t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
          const hasMainWord = words.some(w => t.includes(w));
          const hasStartWord = startWords.some(w => t.includes(w));
          const hasStopWord = stopWords.some(w => t.includes(w));
          return hasMainWord && hasStartWord && !hasStopWord;
        });
        if (btn && click(btn)) return {ok:true, clicked:true};
        return null;
        """
        self._human_action_delay("flash_dom_exec_pre")
        ctx_name, result = self._run_js_in_contexts(script)
        if result:
            self._human_action_post_delay("flash_dom_click_post")
            verify = self._verify_flash_sale_receipt(timeout_seconds=2.6)
            if verify:
                logger.info(f"DOM 秒杀上架执行成功并通过回执校验: ctx={ctx_name}, verify={verify}")
                self._set_action_receipt("start_flash_sale", True, "verify", "ok", {"ctx": ctx_name, "result": result, "verify": verify})
                return True
            if self._is_ocr_vision_mode():
                ocr_verify = self._verify_receipt_by_ocr("start_flash_sale")
                if ocr_verify:
                    logger.info(f"OCR 回执确认秒杀上架成功: {ocr_verify}")
                    self._set_action_receipt(
                        "start_flash_sale",
                        True,
                        "verify_ocr",
                        "ok",
                        {"ctx": ctx_name, "result": result, "verify": ocr_verify},
                    )
                    return True
            logger.warning(f"DOM 秒杀点击后未检测到成功回执: ctx={ctx_name}, result={result}")
            self._set_action_receipt("start_flash_sale", False, "verify", "receipt_not_confirmed", {"ctx": ctx_name, "result": result})
            return False
        return False

    def _stop_flash_sale_by_dom(self):
        """优先基于 DOM 执行秒杀结束动作。"""
        if not self._dom_fallback_enabled():
            return False
        script = """
        const norm = (s) => (s || '').toLowerCase().replace(/\\s+/g, '');
        const isVisible = (el) => {
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const s = window.getComputedStyle(el);
          return r.width > 6 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const click = (el) => {
          if (!el || !isVisible(el)) return false;
          el.scrollIntoView({block:'center', inline:'center'});
          try {
            if (typeof el.click === 'function') {
              el.click();
              return true;
            }
          } catch (e) {}
          try {
            return !!el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
          } catch (e) {
            return false;
          }
        };
        const btnSel = 'button,[role="button"],div[role="button"],span[role="button"]';
        const words = ['秒杀','flashsale','flash','促销','活动'];
        const stopWords = ['结束','停止','下架','关闭','撤下','end','stop','off','close','disable'];
        const startWords = ['上架','开启','开始','launch','start','open','enable','go'];

        const btn = Array.from(document.querySelectorAll(btnSel)).find(el => {
          if (!isVisible(el)) return false;
          const t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
          const hasMainWord = words.some(w => t.includes(norm(w)));
          const hasStopWord = stopWords.some(w => t.includes(norm(w)));
          const hasStartWord = startWords.some(w => t.includes(norm(w)));
          return hasMainWord && hasStopWord && !hasStartWord;
        });
        if (btn && click(btn)) return {ok:true, clicked:true};
        return null;
        """
        self._human_action_delay("flash_stop_dom_exec_pre")
        ctx_name, result = self._run_js_in_contexts(script)
        if result:
            self._human_action_post_delay("flash_stop_dom_click_post")
            verify = self._verify_stop_flash_sale_receipt(timeout_seconds=2.6)
            if verify:
                logger.info(f"DOM 秒杀结束执行成功并通过回执校验: ctx={ctx_name}, verify={verify}")
                self._set_action_receipt("stop_flash_sale", True, "verify", "ok", {"ctx": ctx_name, "result": result, "verify": verify})
                return True
            if self._is_ocr_vision_mode():
                ocr_verify = self._verify_receipt_by_ocr("stop_flash_sale")
                if ocr_verify:
                    logger.info(f"OCR 回执确认秒杀结束成功: {ocr_verify}")
                    self._set_action_receipt(
                        "stop_flash_sale",
                        True,
                        "verify_ocr",
                        "ok",
                        {"ctx": ctx_name, "result": result, "verify": ocr_verify},
                    )
                    return True
            logger.warning(f"DOM 秒杀结束点击后未检测到成功回执: ctx={ctx_name}, result={result}")
            self._set_action_receipt("stop_flash_sale", False, "verify", "receipt_not_confirmed", {"ctx": ctx_name, "result": result})
            return False
        return False

    def _execute_pin_unpin_fixed_chain(self, action, link_index=None):
        idx = self._normalize_link_index(link_index)
        if self._pin_unpin_require_link_index and (not idx):
            self._set_action_receipt(
                action,
                False,
                "precheck",
                "link_index_required_for_fixed_row_click",
                {"link_index": link_index},
            )
            return False

        ocr_try = self._perform_action_by_ocr_anchor(action, link_index=idx)
        if not bool((ocr_try or {}).get("ok")):
            ocr_reason = str((ocr_try or {}).get("reason") or "").strip().lower()
            dom_rescue_ok = False
            dom_rescue_detail = {}
            can_rescue = bool(
                self._pin_unpin_dom_rescue_enabled
                and action in {"pin_product", "unpin_product"}
                and idx > 0
                and ocr_reason in self._pin_unpin_dom_rescue_reasons
            )
            if can_rescue:
                try:
                    ensure_action_page = getattr(self.vision_agent, "ensure_action_page", None)
                    if callable(ensure_action_page):
                        ensure_action_page(action)
                except Exception:
                    pass
                try:
                    if action == "pin_product":
                        dom_rescue_ok = bool(self._pin_product_by_dom(link_index=idx, force=True))
                    else:
                        dom_rescue_ok = bool(self._unpin_product_by_dom(link_index=idx, force=True))
                except Exception as e:
                    dom_rescue_ok = False
                    dom_rescue_detail = {"error": str(e)}
                if dom_rescue_ok:
                    logger.warning(
                        f"pin/unpin OCR锚点未命中，已DOM兜底成功: action={action}, link_index={idx}, ocr_reason={ocr_reason}"
                    )
                    return True
            self._set_action_receipt(
                action,
                False,
                "verify_ocr_anchor",
                str((ocr_try or {}).get("reason") or "ocr_target_not_found"),
                {
                    "link_index": idx,
                    "ocr_try": dict(ocr_try or {}),
                    "dom_rescue_enabled": bool(self._pin_unpin_dom_rescue_enabled),
                    "dom_rescue_attempted": bool(can_rescue),
                    "dom_rescue_ok": bool(dom_rescue_ok),
                    "dom_rescue_detail": dict(dom_rescue_detail or {}),
                },
            )
            return False

        ocr_verify = self._verify_receipt_by_ocr(action, link_index=idx)
        if ocr_verify:
            self._set_action_receipt(
                action,
                True,
                "verify_ocr_anchor",
                "ok",
                {"link_index": idx, "ocr_try": (ocr_try or {}).get("detail") or {}, "verify": ocr_verify},
            )
            return True

        self._set_action_receipt(
            action,
            False,
            "verify_ocr_anchor",
            "ocr_fixed_click_receipt_not_confirmed",
            {
                "link_index": idx,
                "ocr_try": dict(ocr_try or {}),
                "verify_ocr": dict(ocr_verify or {}),
            },
        )
        return False

    def pin_product(self, link_index=None):
        """置顶商品（统一走 OCR 序号导航 + 固定行点击链路）。"""
        self._begin_action_trace("pin_product")
        self._human_action_delay("pin_product_entry")
        self._log_execution_mode("pin_product")
        if not self._ensure_action_page("pin_product"):
            return False
        return self._execute_pin_unpin_fixed_chain("pin_product", link_index=link_index)
        
    def start_flash_sale(self):
        """开启秒杀（优先 DOM，图片模板兜底）。"""
        self._begin_action_trace("start_flash_sale")
        self._human_action_delay("start_flash_sale_entry")
        self._log_execution_mode("start_flash_sale")
        if not self._ensure_action_page("start_flash_sale"):
            return False
        use_ocr_action = self._is_ocr_vision_mode() or (
            self._is_ocr_info_only_mode() and (not self._dom_execution_enabled())
        )
        if use_ocr_action:
            ocr_try = self._perform_action_by_ocr_anchor("start_flash_sale")
            if ocr_try.get("ok"):
                if self._dom_fallback_enabled():
                    verify = self._verify_flash_sale_receipt(timeout_seconds=2.6)
                    if verify:
                        self._set_action_receipt(
                            "start_flash_sale",
                            True,
                            "verify_ocr_anchor",
                            "ok",
                            {"ocr_try": ocr_try.get("detail") or {}, "verify": verify},
                        )
                        return True
                ocr_verify = self._verify_receipt_by_ocr("start_flash_sale")
                if ocr_verify:
                    self._set_action_receipt(
                        "start_flash_sale",
                        True,
                        "verify_ocr_anchor",
                        "ok",
                        {"ocr_try": ocr_try.get("detail") or {}, "verify": ocr_verify},
                    )
                    return True
            else:
                logger.debug(f"OCR秒杀锚点未命中，转DOM: {ocr_try.get('reason')}")
            if self._is_ocr_info_only_mode() or (self._is_ocr_vision_mode() and (not self._dom_fallback_enabled())):
                self._set_action_receipt(
                    "start_flash_sale",
                    False,
                    "verify_ocr_anchor",
                    "ocr_receipt_not_confirmed_dom_disabled",
                    {"ocr_try": ocr_try},
                )
                return False
        if self._start_flash_sale_by_dom():
            return True
        ok = self.perform_action_by_image("flash_sale_icon.png")
        self._set_action_receipt("start_flash_sale", ok, "image_fallback", "ok" if ok else "image_fallback_failed")
        return ok

    def stop_flash_sale(self):
        """结束秒杀（优先 OCR 锚点/DOM，不做图片模板兜底，避免误点）。"""
        self._begin_action_trace("stop_flash_sale")
        self._human_action_delay("stop_flash_sale_entry")
        self._log_execution_mode("stop_flash_sale")
        if not self._ensure_action_page("stop_flash_sale"):
            return False
        use_ocr_action = self._is_ocr_vision_mode() or (
            self._is_ocr_info_only_mode() and (not self._dom_execution_enabled())
        )
        if use_ocr_action:
            ocr_try = self._perform_action_by_ocr_anchor("stop_flash_sale")
            if ocr_try.get("ok"):
                if self._dom_fallback_enabled():
                    verify = self._verify_stop_flash_sale_receipt(timeout_seconds=2.6)
                    if verify:
                        self._set_action_receipt(
                            "stop_flash_sale",
                            True,
                            "verify_ocr_anchor",
                            "ok",
                            {"ocr_try": ocr_try.get("detail") or {}, "verify": verify},
                        )
                        return True
                ocr_verify = self._verify_receipt_by_ocr("stop_flash_sale")
                if ocr_verify:
                    self._set_action_receipt(
                        "stop_flash_sale",
                        True,
                        "verify_ocr_anchor",
                        "ok",
                        {"ocr_try": ocr_try.get("detail") or {}, "verify": ocr_verify},
                    )
                    return True
            else:
                logger.debug(f"OCR结束秒杀锚点未命中，转DOM: {ocr_try.get('reason')}")
            if self._is_ocr_info_only_mode() or (self._is_ocr_vision_mode() and (not self._dom_fallback_enabled())):
                self._set_action_receipt(
                    "stop_flash_sale",
                    False,
                    "verify_ocr_anchor",
                    "ocr_receipt_not_confirmed_dom_disabled",
                    {"ocr_try": ocr_try},
                )
                return False
        ok = self._stop_flash_sale_by_dom()
        if not ok:
            reason = "dom_stop_flash_sale_failed" if self._dom_fallback_enabled() else "dom_disabled_no_ocr_confirm"
            self._set_action_receipt("stop_flash_sale", False, "dom", reason)
        return ok

    def unpin_product(self, link_index=None):
        """取消置顶（统一走 OCR 序号导航 + 固定行点击链路）。"""
        self._begin_action_trace("unpin_product")
        self._human_action_delay("unpin_product_entry")
        self._log_execution_mode("unpin_product")
        if not self._ensure_action_page("unpin_product"):
            return False
        return self._execute_pin_unpin_fixed_chain("unpin_product", link_index=link_index)
