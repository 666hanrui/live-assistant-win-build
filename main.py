import time
import threading
import random
import json
import re
import os
import sys
import subprocess
import signal
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from collections import deque
from datetime import datetime
from pathlib import Path
from DrissionPage import ChromiumPage, ChromiumOptions
from agents.vision_agent import VisionAgent
from agents.atmosphere_agent import AtmosphereAgent
from agents.operations_agent import OperationsAgent
from agents.knowledge_agent import KnowledgeAgent
from agents.voice_command_agent import VoiceCommandAgent
from agents.analytics_agent import AnalyticsAgent
from utils.logger import logger
from utils.platform_utils import build_chrome_debug_commands, build_chrome_debug_launch_args
import app_config.settings as settings

class LiveAssistant:
    def __init__(self):
        self.is_running = False
        self.vision = VisionAgent()
        self.atmosphere = AtmosphereAgent()
        self.knowledge = KnowledgeAgent()
        self.operations = OperationsAgent(self.vision)
        if hasattr(self.operations, "set_reaction_judge"):
            self.operations.set_reaction_judge(self.knowledge)
        if hasattr(self.operations, "set_action_planner"):
            self.operations.set_action_planner(self.knowledge)
        if hasattr(self.operations, "set_operation_navigator"):
            self.operations.set_operation_navigator(self.knowledge)
        self.operation_execution_mode = "ocr_vision"
        self.dom_operation_mode_explicit = False
        if hasattr(self.operations, "set_execution_mode"):
            self.operation_execution_mode = self.operations.set_execution_mode(
                getattr(settings, "OPERATION_EXECUTION_MODE", "ocr_vision")
            )
        self.dom_operation_mode_explicit = self.operation_execution_mode == "dom"
        self.web_info_source_mode = "ocr_only"
        self.dom_web_info_mode_explicit = False
        if hasattr(self.vision, "set_info_source_mode"):
            self.web_info_source_mode = self.vision.set_info_source_mode(
                getattr(settings, "WEB_INFO_SOURCE_MODE", "ocr_only")
            )
        self.dom_web_info_mode_explicit = self.web_info_source_mode == "dom"
        self.voice = VoiceCommandAgent(self.vision)
        self.analytics = AnalyticsAgent()
        self._thread = None
        self._stop_event = threading.Event()
        # 存储最近 100 条弹幕用于前端显示
        self.danmu_log = deque(maxlen=100)
        self.voice_input_log = deque(maxlen=200)
        self.last_danmu_time = time.time()
        self.last_proactive_time = 0.0
        self.reply_language = settings.DEFAULT_REPLY_LANGUAGE
        self.tone_template = ""
        self.reply_enabled = True
        self.proactive_enabled = settings.PROACTIVE_ENABLED
        self.voice_command_enabled = settings.VOICE_COMMAND_ENABLED
        self._runtime_state_file = Path("data/runtime_state.json")
        self.next_proactive_interval = random.uniform(
            settings.PROACTIVE_MIN_INTERVAL,
            settings.PROACTIVE_MAX_INTERVAL
        )
        self.message_cache = {}
        self.user_reply_cache = {}
        self.global_reply_cache = {}
        self.voice_command_cache = {}
        self.voice_action_cache = {}
        self.danmu_action_cache = {}
        self.sent_message_cache = {}
        self.last_voice_poll_at = 0.0
        self.last_voice_health_log_at = 0.0
        self.last_voice_forced_restart_at = 0.0
        self._google_voice_error_streak = 0
        self._google_voice_error_last_at = 0.0
        self.last_llm_query_at = 0.0
        self.last_report_check_at = 0.0
        self._start_lock = threading.Lock()
        self.is_starting = False
        self.last_start_error = ""
        self.last_start_detail = ""
        self.last_start_at = 0.0
        self._last_browser_rebind_attempt_at = 0.0
        self.cloud_asr_test_enabled = False
        self.cloud_asr_test_url = "https://www.bilibili.com/"
        self._cloud_asr_test_prev_provider = ""
        self._cloud_asr_test_prev_input_mode = ""
        self._cloud_asr_test_prev_mic_index = None
        self._cloud_asr_test_prev_mic_hint = ""
        self.cloud_asr_test_log = deque(maxlen=240)
        self._load_runtime_state()

    def _get_active_language(self):
        valid_languages = set(settings.REPLY_LANGUAGES.values())
        if self.reply_language in valid_languages:
            return self.reply_language
        return settings.DEFAULT_REPLY_LANGUAGE

    def _get_voice_fallback_languages(self, active_language):
        """
        为语音口令选择回退语言（严格同语族）：
        - 统一语言为英文时，仅允许英文语族；
        - 统一语言为中文时，仅允许中文语族。
        """
        active = str(active_language or "").strip()
        base = active.split("-")[0].lower() if active else ""
        raw = list(getattr(settings, "VOICE_COMMAND_FALLBACK_LANGUAGES", []) or [])
        if not raw:
            return []

        max_langs = max(1, int(getattr(settings, "VOICE_COMMAND_MAX_LANGS", 2) or 2))

        def _dedupe(seq):
            out = []
            seen = set()
            for item in seq:
                item = str(item or "").strip()
                if not item or item in seen:
                    continue
                seen.add(item)
                out.append(item)
            return out

        raw = _dedupe(raw)
        same_family = [lang for lang in raw if str(lang).split("-")[0].lower() == base] if base else list(raw)
        candidates = same_family

        # fallback 不包含 active 本身；总语言数上限由 max_langs 控制（包含 active）。
        candidates = [lang for lang in candidates if lang != active]
        if max_langs <= 1:
            return []
        return candidates[: max_langs - 1]

    def _build_cloud_test_languages(self, active_language):
        """
        播放器流 ASR 对比测试的语言链：
        - 不受统一语言“同语族”限制，避免测试页语音与统一语言不一致时持续无文本。
        - 至少包含 zh-CN / en-US，兼容 B 站中英内容。
        """
        def _normalize_lang(code):
            s = str(code or "").strip()
            if not s:
                return ""
            lower = s.lower()
            alias = {
                "zh": "zh-CN",
                "zh-cn": "zh-CN",
                "zh-hans": "zh-CN",
                "en": "en-US",
                "en-us": "en-US",
            }
            return alias.get(lower, s)

        langs = []

        def _add(code):
            norm = _normalize_lang(code)
            if norm and norm not in langs:
                langs.append(norm)

        _add(active_language)
        for code in list(getattr(settings, "VOICE_DASHSCOPE_LANGUAGE_HINTS", []) or []):
            _add(code)
        for code in list(getattr(settings, "VOICE_COMMAND_FALLBACK_LANGUAGES", []) or []):
            _add(code)
        _add("zh-CN")
        _add("en-US")

        max_langs = max(2, int(getattr(settings, "VOICE_COMMAND_MAX_LANGS", 2) or 2))
        return langs[: max_langs]

    def _language_family(self, lang_code):
        code = str(lang_code or "").strip().lower()
        if code.startswith("zh"):
            return "zh"
        if code.startswith("en"):
            return "en"
        return ""

    def _is_voice_language_match(self, text, detected_lang, active_language):
        expected = self._language_family(active_language)
        if not expected:
            return True
        detected = self._language_family(detected_lang)
        raw = str(text or "")
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", raw))
        has_latin = bool(re.search(r"[A-Za-z]", raw))

        if expected == "en":
            if detected and detected != "en":
                return False
            if has_cjk:
                return False
            return True

        if expected == "zh":
            if detected and detected != "zh":
                return False
            if has_latin:
                return False
            return True

        return True

    def get_unified_language(self):
        """统一语言：回复/暖场/知识库问答共用。"""
        return self._get_active_language()

    def get_startup_state(self):
        return {
            "is_running": bool(self.is_running),
            "is_starting": bool(self.is_starting),
            "last_start_error": self.last_start_error or "",
            "last_start_detail": self.last_start_detail or "",
            "last_start_at": self.last_start_at or 0.0,
        }

    def get_browser_connected(self, allow_rebind=True):
        """
        连接状态自愈判断：
        - 优先复用当前 page
        - page 丢失时，若 DevTools 端口仍在线，可按需尝试轻量重挂接
        """
        if self.get_web_info_source_mode() == "screen_ocr":
            try:
                return bool(self.vision.ensure_connection())
            except Exception:
                return False

        try:
            page = getattr(self.vision, "page", None)
            if page and self.vision._page_alive():
                return True
        except Exception:
            pass

        if not allow_rebind:
            return False

        try:
            if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                now = time.time()
                # 限流重挂接，避免 dashboard 高频 rerun 触发反复扫描标签页。
                if now - self._last_browser_rebind_attempt_at < 5.0:
                    return False
                self._last_browser_rebind_attempt_at = now
                return bool(self.connect_browser())
        except Exception:
            pass
        return False

    def _load_runtime_state(self):
        """加载上次运行的回复设置。"""
        try:
            if not self._runtime_state_file.exists():
                return

            data = json.loads(self._runtime_state_file.read_text(encoding="utf-8"))
            language = data.get("unified_language") or data.get("reply_language")
            tone_template = data.get("tone_template")
            reply_enabled = data.get("reply_enabled")
            proactive_enabled = data.get("proactive_enabled")
            voice_enabled = data.get("voice_command_enabled")
            voice_mic_index = data.get("voice_mic_device_index")
            voice_mic_name_hint = data.get("voice_mic_name_hint")
            voice_input_mode = str(data.get("voice_input_mode") or "").strip().lower()
            voice_asr_provider = str(data.get("voice_asr_provider") or "").strip().lower()
            provider_aliases = {
                "dashscope": "dashscope_funasr",
                "aliyun_funasr": "dashscope_funasr",
                "funasr": "dashscope_funasr",
                "hybrid": "hybrid_local_cloud",
                "local_cloud": "hybrid_local_cloud",
                "cloud_local": "hybrid_local_cloud",
            }
            voice_asr_provider = provider_aliases.get(voice_asr_provider, voice_asr_provider)
            operation_execution_mode = str(data.get("operation_execution_mode") or "").strip().lower()
            web_info_source_mode = str(data.get("web_info_source_mode") or "").strip().lower()
            dom_operation_mode_explicit = data.get("dom_operation_mode_explicit")
            if dom_operation_mode_explicit is None:
                dom_operation_mode_explicit = data.get("dom_mode_opt_in")
            dom_web_info_mode_explicit = data.get("dom_web_info_mode_explicit")
            if dom_web_info_mode_explicit is None:
                dom_web_info_mode_explicit = data.get("dom_info_mode_opt_in")
            human_like_settings = data.get("human_like_settings") or data.get("human_like") or {}

            if language in set(settings.REPLY_LANGUAGES.values()):
                self.reply_language = language
            if isinstance(tone_template, str):
                self.tone_template = tone_template.strip()
            if isinstance(reply_enabled, bool):
                self.reply_enabled = reply_enabled
            if isinstance(proactive_enabled, bool):
                self.proactive_enabled = proactive_enabled
            if isinstance(voice_enabled, bool):
                self.voice_command_enabled = voice_enabled
            if hasattr(self.voice, "set_preferred_microphone"):
                if voice_mic_index is not None or voice_mic_name_hint is not None:
                    self.voice.set_preferred_microphone(
                        device_index=voice_mic_index,
                        name_hint=voice_mic_name_hint,
                        restart_if_running=False,
                    )
            mode_aliases = {
                "tab_media": "tab_audio_asr",
                "tab_media_asr": "tab_audio_asr",
                "system_audio": "system_audio_asr",
            }
            voice_input_mode = mode_aliases.get(voice_input_mode, voice_input_mode)
            if voice_input_mode in {
                "python_asr",
                "local_python_asr",
                "python_local",
                "local",
                "system_loopback_asr",
                "loopback_asr",
                "system_audio_asr",
                "tab_audio_asr",
                "loopback",
                "web_speech",
            }:
                self.voice.input_mode = voice_input_mode
                if hasattr(self.voice, "_sync_capture_mode"):
                    self.voice._sync_capture_mode()
            if voice_asr_provider in {"google", "auto", "sphinx", "whisper_local", "dashscope_funasr", "hybrid_local_cloud"}:
                settings.VOICE_PYTHON_ASR_PROVIDER = voice_asr_provider
            if operation_execution_mode:
                op_mode = operation_execution_mode
                explicit_dom_op = bool(dom_operation_mode_explicit) if isinstance(dom_operation_mode_explicit, bool) else False
                if op_mode == "dom" and not explicit_dom_op:
                    logger.info("运行态配置未显式授权 DOM 操作模式，自动降级为 ocr_vision。")
                    op_mode = "ocr_vision"
                self.set_operation_execution_mode(op_mode, persist=False, explicit=explicit_dom_op)
            if web_info_source_mode:
                info_mode = web_info_source_mode
                explicit_dom_info = bool(dom_web_info_mode_explicit) if isinstance(dom_web_info_mode_explicit, bool) else False
                if info_mode == "dom" and not explicit_dom_info:
                    logger.info("运行态配置未显式授权 DOM 信息源，自动降级为 ocr_only。")
                    info_mode = "ocr_only"
                self.set_web_info_source_mode(info_mode, persist=False, explicit=explicit_dom_info)
            if isinstance(human_like_settings, dict) and human_like_settings:
                self.set_human_like_settings(human_like_settings, persist=False)

            logger.info(
                "已加载上次设置: "
                f"language={self.reply_language}, "
                f"tone_template={'on' if self.tone_template else 'off'}, "
                f"reply={'on' if self.reply_enabled else 'off'}, "
                f"proactive={'on' if self.proactive_enabled else 'off'}, "
                f"voice_command={'on' if self.voice_command_enabled else 'off'}, "
                f"operation_mode={self.get_operation_execution_mode()}, "
                f"web_info_mode={self.get_web_info_source_mode()}"
            )
        except Exception as e:
            logger.warning(f"加载本地设置失败，使用默认设置: {e}")

    def _save_runtime_state(self):
        """持久化当前回复设置到本地文件。"""
        payload = {
            "unified_language": self._get_active_language(),
            "reply_language": self._get_active_language(),
            "tone_template": self.tone_template,
            "reply_enabled": bool(self.reply_enabled),
            "proactive_enabled": bool(self.proactive_enabled),
            "voice_command_enabled": bool(self.voice_command_enabled),
            "voice_input_mode": str(getattr(self.voice, "input_mode", getattr(settings, "VOICE_COMMAND_INPUT_MODE", "python_asr")) or "python_asr"),
            "operation_execution_mode": self.get_operation_execution_mode(),
            "dom_operation_mode_explicit": bool(self.dom_operation_mode_explicit),
            "web_info_source_mode": self.get_web_info_source_mode(),
            "dom_web_info_mode_explicit": bool(self.dom_web_info_mode_explicit),
            "human_like_settings": self.get_human_like_settings(),
        }
        if hasattr(self.voice, "get_preferred_microphone"):
            try:
                mic_cfg = self.voice.get_preferred_microphone() or {}
                payload["voice_mic_device_index"] = mic_cfg.get("deviceIndex")
                payload["voice_mic_name_hint"] = mic_cfg.get("nameHint") or ""
            except Exception:
                pass
        payload["voice_asr_provider"] = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local")
        try:
            self._runtime_state_file.parent.mkdir(parents=True, exist_ok=True)
            self._runtime_state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"保存本地设置失败: {e}")

    def update_reply_settings(self, language=None, tone_template=None):
        """运行时更新回复语言与语气模板。"""
        if language in set(settings.REPLY_LANGUAGES.values()):
            self.reply_language = language
        if tone_template is not None:
            self.tone_template = tone_template.strip()
        # 暖场与回复统一使用同一语言配置，并持久化到本地。
        self.reply_language = self._get_active_language()
        if self.voice_command_enabled:
            # 统一语言切换后立即同步语音识别语言，避免继续使用旧语言参数。
            try:
                active_language = self._get_active_language()
                self.voice.ensure_started(
                    language=active_language,
                    fallback_languages=self._get_voice_fallback_languages(active_language),
                    silence_restart_seconds=settings.VOICE_COMMAND_SILENCE_RESTART_SECONDS,
                )
            except Exception:
                pass
        self._save_runtime_state()
        logger.info(
            f"回复设置已更新并保存: language={self.reply_language}, tone_template={'on' if self.tone_template else 'off'}"
        )

    def set_voice_command_enabled(self, enabled: bool):
        self.voice_command_enabled = bool(enabled)
        self._save_runtime_state()
        logger.info(f"语音口令监听设置已更新: {'on' if self.voice_command_enabled else 'off'}")
        if not self.voice_command_enabled:
            try:
                self.voice.stop()
            except Exception:
                pass

    def set_reply_enabled(self, enabled: bool):
        self.reply_enabled = bool(enabled)
        self._save_runtime_state()
        logger.info(f"自动回复设置已更新: {'on' if self.reply_enabled else 'off'}")

    def set_proactive_enabled(self, enabled: bool):
        self.proactive_enabled = bool(enabled)
        self._save_runtime_state()
        logger.info(f"自动暖场设置已更新: {'on' if self.proactive_enabled else 'off'}")

    def get_operation_execution_mode(self):
        if hasattr(self.operations, "get_execution_mode"):
            try:
                mode = self.operations.get_execution_mode()
                if mode:
                    self.operation_execution_mode = str(mode)
            except Exception:
                pass
        return str(self.operation_execution_mode or "ocr_vision")

    def set_operation_execution_mode(self, mode, persist=True, explicit=None):
        target = str(mode or "").strip().lower()
        if hasattr(self.operations, "set_execution_mode"):
            try:
                target = self.operations.set_execution_mode(target)
            except Exception:
                target = "ocr_vision"
        if target not in {"dom", "ocr_vision"}:
            target = "ocr_vision"
        self.operation_execution_mode = target
        if explicit is None:
            self.dom_operation_mode_explicit = target == "dom"
        else:
            self.dom_operation_mode_explicit = bool(explicit) and target == "dom"
        if persist:
            self._save_runtime_state()
        logger.info(
            f"运营执行模式已更新: {target}, "
            f"dom_explicit={'on' if self.dom_operation_mode_explicit else 'off'}"
        )
        return target

    def get_operation_mode_status(self):
        try:
            if hasattr(self.operations, "get_mode_status"):
                status = self.operations.get_mode_status() or {}
                if isinstance(status, dict):
                    return status
        except Exception:
            pass
        return {"mode": self.get_operation_execution_mode()}

    def get_human_like_settings(self):
        try:
            if hasattr(self.operations, "get_human_like_settings"):
                cfg = self.operations.get_human_like_settings() or {}
                if isinstance(cfg, dict):
                    return cfg
        except Exception:
            pass
        return {}

    def set_human_like_settings(self, config, persist=True):
        cfg = dict(config or {})
        try:
            if hasattr(self.operations, "set_human_like_settings"):
                self.operations.set_human_like_settings(**cfg)
        except Exception as e:
            logger.warning(f"应用拟人化执行参数失败: {e}")
        if persist:
            self._save_runtime_state()
        return self.get_human_like_settings()

    def get_human_like_stats(self):
        try:
            if hasattr(self.operations, "get_human_like_stats"):
                data = self.operations.get_human_like_stats() or {}
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def get_web_info_source_mode(self):
        if hasattr(self.vision, "get_info_source_mode"):
            try:
                mode = self.vision.get_info_source_mode()
                if mode:
                    self.web_info_source_mode = str(mode)
            except Exception:
                pass
        return str(self.web_info_source_mode or "ocr_only")

    def set_web_info_source_mode(self, mode, persist=True, explicit=None):
        target = str(mode or "").strip().lower()
        if target not in {"dom", "ocr_hybrid", "ocr_only", "screen_ocr"}:
            target = "ocr_only"
        if hasattr(self.vision, "set_info_source_mode"):
            try:
                target = self.vision.set_info_source_mode(target)
            except Exception:
                target = "ocr_only"
        self.web_info_source_mode = target
        if explicit is None:
            self.dom_web_info_mode_explicit = target == "dom"
        else:
            self.dom_web_info_mode_explicit = bool(explicit) and target == "dom"
        if persist:
            self._save_runtime_state()
        logger.info(
            f"网页信息源模式已更新: {target}, "
            f"dom_explicit={'on' if self.dom_web_info_mode_explicit else 'off'}"
        )
        return target

    def get_web_info_source_status(self):
        try:
            if hasattr(self.vision, "get_info_source_status"):
                status = self.vision.get_info_source_status() or {}
                if isinstance(status, dict):
                    return status
        except Exception:
            pass
        return {"mode": self.get_web_info_source_mode()}

    def set_voice_mic_device(self, device_index=None, name_hint=None):
        if hasattr(self.voice, "set_preferred_microphone"):
            self.voice.set_preferred_microphone(
                device_index=device_index,
                name_hint=name_hint,
                restart_if_running=True,
            )
        self._save_runtime_state()

    def get_voice_mic_device(self):
        if hasattr(self.voice, "get_preferred_microphone"):
            try:
                return self.voice.get_preferred_microphone() or {}
            except Exception:
                return {}
        return {}

    def probe_voice_microphone(self, duration_seconds=2.5):
        if hasattr(self.voice, "probe_local_microphone"):
            try:
                active_language = self._get_active_language()
                return self.voice.probe_local_microphone(
                    language=active_language,
                    fallback_languages=self._get_voice_fallback_languages(active_language),
                    duration_seconds=duration_seconds,
                )
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "probe_not_supported"}

    def execute_manual_voice_text(self, text, source="manual_voice_test"):
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "empty_text"}
        has_wake = self._pass_voice_wake_word(text)
        command = self._parse_operation_command_text(text)
        if not command:
            return {"ok": False, "error": "no_command_detected", "has_wake": has_wake}
        page_ready = self._prepare_action_page_for_command(command, trigger_source=source)
        if not bool(page_ready.get("ok")):
            return {
                "ok": False,
                "has_wake": has_wake,
                "command": command,
                "error": "action_page_not_ready",
                "page_prepare": page_ready,
            }
        ok = self._execute_operation_command(command, trigger_source=source, skip_page_prepare=True)
        return {
            "ok": bool(ok),
            "has_wake": has_wake,
            "command": command,
            "page_prepare": page_ready,
        }

    def apply_pin_click_stable_defaults(self, persist=True):
        """
        固化“置顶链路”稳定默认配置（非 DOM，screen_ocr + 物理链路）。
        """
        op_mode = self.set_operation_execution_mode("ocr_vision", persist=False, explicit=False)
        web_mode = self.set_web_info_source_mode("screen_ocr", persist=False, explicit=False)
        human_cfg = self.set_human_like_settings(
            {
                "force_full_physical_chain": True,
                "ocr_physical_click_enabled": True,
                "ocr_vision_allow_dom_fallback": False,
                "pin_click_test_confirm_popup": True,
            },
            persist=False,
        )
        if persist:
            self._save_runtime_state()
        return {
            "ok": True,
            "operation_execution_mode": str(op_mode or ""),
            "web_info_source_mode": str(web_mode or ""),
            "human_like_settings": dict(human_cfg or {}),
        }

    def run_pin_click_regression_check(
        self,
        link_index=9,
        command_text="",
        source="pin_click_regression_check",
        apply_stable_defaults=True,
    ):
        """
        置顶回归自检：
        - 可选先固化稳定默认配置
        - 触发“助播，置顶N号链接”
        - 返回执行结果、回执和关键运行态
        """
        try:
            idx = int(link_index)
        except Exception:
            idx = 9
        if idx <= 0:
            idx = 9

        defaults_result = {}
        if apply_stable_defaults:
            defaults_result = self.apply_pin_click_stable_defaults(persist=True)

        cmd = str(command_text or "").strip()
        if not cmd:
            cmd = f"助播，置顶{idx}号链接"
        execute_result = self.execute_manual_voice_text(cmd, source=source)

        receipt = {}
        op_status = {}
        web_status = {}
        try:
            if hasattr(self.operations, "get_last_action_receipt"):
                receipt = self.operations.get_last_action_receipt() or {}
        except Exception:
            receipt = {}
        try:
            if hasattr(self.operations, "get_mode_status"):
                op_status = self.operations.get_mode_status() or {}
        except Exception:
            op_status = {}
        try:
            if hasattr(self.vision, "get_info_source_status"):
                web_status = self.vision.get_info_source_status() or {}
        except Exception:
            web_status = {}

        receipt_reason = str((receipt or {}).get("reason") or "")
        return {
            "ok": bool(execute_result.get("ok")),
            "command_text": cmd,
            "link_index": int(idx),
            "receipt_reason": receipt_reason,
            "defaults_applied": dict(defaults_result or {}),
            "execute_result": dict(execute_result or {}),
            "action_receipt": dict(receipt or {}),
            "operation_status": {
                "mode": str((op_status or {}).get("mode") or ""),
                "last_click_driver": str((op_status or {}).get("last_click_driver") or ""),
                "last_click_point": dict((op_status or {}).get("last_click_point") or {}),
                "last_click_error": str((op_status or {}).get("last_click_error") or ""),
                "last_fixed_row_click": dict((op_status or {}).get("last_fixed_row_click") or {}),
                "nav_last_trace": dict((op_status or {}).get("nav_last_trace") or {}),
            },
            "web_status": {
                "mode": str((web_status or {}).get("mode") or self.get_web_info_source_mode()),
                "ocr_page_type": str((web_status or {}).get("ocr_page_type") or ""),
                "ocr_error": str((web_status or {}).get("ocr_error") or ""),
            },
        }

    def _prepare_action_page_for_command(self, command, trigger_source=""):
        """
        执行动作前先确保在可执行页面：
        1) 优先在已连接/已打开标签页中切到可执行页；
        2) 对手动触发链路，必要时兜底连接内置 Mock 可执行页。
        """
        action = str((command or {}).get("action") or "").strip().lower()
        if action not in {"pin_product", "unpin_product", "repin_product", "start_flash_sale", "stop_flash_sale"}:
            return {"ok": True, "skipped": True, "reason": "non_action_command"}

        source = str(trigger_source or "").strip().lower()
        allow_mock_fallback = any(tok in source for tok in ["manual", "pin_click_test", "typing_trigger"])

        strict_ocr_detail = {}
        strict_ocr_reason = ""

        def _strict_ocr_operable_check():
            ops = getattr(self, "operations", None)
            checker = getattr(ops, "_is_ocr_operable_page", None)
            if not callable(checker):
                return None, {}
            try:
                ocr_ok, ocr_detail = checker(action)
            except Exception as e:
                logger.warning(f"执行前页面准备失败(strict_ocr_check): action={action}, err={e}")
                return None, {"reason": "strict_ocr_check_exception", "error": str(e)}
            detail = ocr_detail if isinstance(ocr_detail, dict) else {}
            return bool(ocr_ok), detail

        ensure_action_page = getattr(self.vision, "ensure_action_page", None)
        if callable(ensure_action_page):
            try:
                if bool(ensure_action_page(action)):
                    if allow_mock_fallback and self.get_web_info_source_mode() == "screen_ocr":
                        strict_ok, strict_detail = _strict_ocr_operable_check()
                        if strict_ok is False:
                            strict_ocr_detail = dict(strict_detail or {})
                            strict_ocr_reason = str(strict_ocr_detail.get("reason") or "strict_ocr_non_operable")
                            logger.warning(
                                f"执行前页面准备二次门禁未通过: action={action}, reason={strict_ocr_reason}"
                            )
                        else:
                            return {
                                "ok": True,
                                "reason": "vision_action_page_ready_strict_ocr" if strict_ok is True else "vision_action_page_ready",
                            }
                    else:
                        return {"ok": True, "reason": "vision_action_page_ready"}
            except Exception as e:
                logger.warning(f"执行前页面准备失败(action_page_check): action={action}, err={e}")

        if allow_mock_fallback and hasattr(self, "connect_mock_shop"):
            try:
                if bool(self.connect_mock_shop(view="dashboard_live")):
                    if callable(ensure_action_page):
                        try:
                            if bool(ensure_action_page(action)):
                                return {"ok": True, "reason": "mock_shop_connected"}
                        except Exception as e:
                            logger.warning(f"执行前页面准备失败(mock_after_ensure): action={action}, err={e}")
                    return {"ok": True, "reason": "mock_shop_connected_no_ensure"}
            except Exception as e:
                logger.warning(f"执行前页面准备失败(connect_mock_shop): action={action}, err={e}")

        try:
            ctx = self.vision.get_page_context() if hasattr(self.vision, "get_page_context") else {}
        except Exception:
            ctx = {}
        return {
            "ok": False,
            "reason": strict_ocr_reason or "operable_page_not_found",
            "action": action,
            "page_type": str((ctx or {}).get("page_type") or ""),
            "url": str((ctx or {}).get("url") or ""),
            "source": source,
            "strict_ocr_detail": dict(strict_ocr_detail or {}),
        }

    def set_voice_asr_provider(self, provider):
        provider = str(provider or "").strip().lower()
        provider_aliases = {
            "dashscope": "dashscope_funasr",
            "aliyun_funasr": "dashscope_funasr",
            "funasr": "dashscope_funasr",
            "hybrid": "hybrid_local_cloud",
            "local_cloud": "hybrid_local_cloud",
            "cloud_local": "hybrid_local_cloud",
        }
        provider = provider_aliases.get(provider, provider)
        if provider not in {"google", "auto", "sphinx", "whisper_local", "dashscope_funasr", "hybrid_local_cloud"}:
            return False
        settings.VOICE_PYTHON_ASR_PROVIDER = provider
        if bool(getattr(self.voice, "_local_running", False)):
            try:
                self.voice.stop()
                active_language = self._get_active_language()
                fallback_languages = self._get_voice_fallback_languages(active_language)
                self.voice.start(language=active_language, fallback_languages=fallback_languages)
            except Exception:
                pass
        self._save_runtime_state()
        return True

    def set_voice_input_mode(self, mode):
        target = str(mode or "").strip().lower()
        aliases = {
            "tab_media": "tab_audio_asr",
            "tab_media_asr": "tab_audio_asr",
            "system_audio": "system_audio_asr",
        }
        target = aliases.get(target, target)
        allowed = {
            "python_asr",
            "local_python_asr",
            "python_local",
            "local",
            "system_loopback_asr",
            "loopback_asr",
            "system_audio_asr",
            "tab_audio_asr",
            "loopback",
            "web_speech",
        }
        if target not in allowed:
            return False
        try:
            self.voice.input_mode = target
            if hasattr(self.voice, "_sync_capture_mode"):
                self.voice._sync_capture_mode()
            if bool(getattr(self.voice, "_local_running", False)) or bool(getattr(self.voice, "_tab_audio_running", False)):
                self.voice.stop()
                active_language = self._get_active_language()
                fallback_languages = self._get_voice_fallback_languages(active_language)
                self.voice.start(language=active_language, fallback_languages=fallback_languages)
            self._save_runtime_state()
            return True
        except Exception:
            return False

    def _normalize_text(self, text):
        text = (text or "").strip().lower()
        return "".join(ch for ch in text if ch.isalnum() or ('\u4e00' <= ch <= '\u9fff'))

    def _normalize_voice_command_text(self, text):
        """
        语音口令归一化：
        - 吸收 ASR 常见同音/繁简体偏差，提升口令命中率
        """
        s = (text or "").strip().lower()
        if not s:
            return ""

        phrase_map = {
            "致頂": "置顶",
            "致顶": "置顶",
            "治顶": "置顶",
            "製頂": "置顶",
            "制顶": "置顶",
            "置頂": "置顶",
            "頂置": "顶置",
            "鏈接": "链接",
            "鏈結": "链接",
            "連接": "连接",
            "連結": "链接",
            "秒殺": "秒杀",
            "秒沙": "秒杀",
            "秒刹": "秒杀",
            "描杀": "秒杀",
            "秒莎": "秒杀",
            "上線": "上线",
            "開啟": "开启",
            "開始": "开始",
            "開一下": "开一下",
            "祝播": "助播",
            "祝bo": "助播",
            "助bo": "助播",
            "致定": "置顶",
            "致钉": "置顶",
            "置定": "置顶",
            "制定": "置顶",
            "制顶": "置顶",
            "至顶": "置顶",
            "置丁": "置顶",
            "制丁": "置顶",
            "制足": "置顶",
            "置足": "置顶",
            "至底": "置顶",
            "置底": "置顶",
            "鏈街": "链接",
            "鏈路": "链接",
            "绿接": "链接",
            "令接": "链接",
            "练接": "链接",
            "联接": "链接",
            "鏈潔": "链接",
            "链洁": "链接",
            "連接": "链接",
            "flash sell": "flash sale",
            "flashsale": "flash sale",
            "flashcell": "flash sale",
            "slash sale": "flash sale",
            "flesh sale": "flash sale",
            "flash sail": "flash sale",
            "co host": "cohost",
            "co-host": "cohost",
            "co hoster": "cohost",
            "co-hoster": "cohost",
            "co hostess": "cohost",
            "live assistant": "liveassistant",
            "stream assistant": "streamassistant",
            "assistance": "assistant",
            "assistants": "assistant",
            "a system": "assistant",
            "hey assistant": "assistant",
            "un pin": "unpin",
            "re pin": "repin",
            "re-pin": "repin",
            "repin": "repin",
            "pin link": "pinlink",
            "unpin link": "unpinlink",
            "re pin link": "repinlink",
            "repin link": "repinlink",
            "number too": "number two",
            "number to": "number two",
            "number for": "number four",
            "number fore": "number four",
            "number tree": "number three",
            "number free": "number three",
            "link too": "link two",
            "link to": "link two",
            "link for": "link four",
            "link fore": "link four",
            "link tree": "link three",
            "link free": "link three",
            # 英文口令常见口语/误识别：to the top 被识别成 two the top
            "one two the top": "one to the top",
            "one too the top": "one to the top",
            "pick one two the top": "pin one to the top",
            "pick one too the top": "pin one to the top",
            # flash sale 口令常见误识别
            "lounge the flash sale": "launch flash sale",
            "lounge flash sale": "launch flash sale",
            "lanch flash sale": "launch flash sale",
            "launche flash sale": "launch flash sale",
            "launch the flash sail": "launch flash sale",
            "launch flash sail": "launch flash sale",
            "launch the flash cell": "launch flash sale",
            "launch flash cell": "launch flash sale",
            "launch the flash seal": "launch flash sale",
            "launch flash seal": "launch flash sale",
            "lounge the flash sail": "launch flash sale",
            "lounge flash sail": "launch flash sale",
            "lounge the flash cell": "launch flash sale",
            "lounge flash cell": "launch flash sale",
            "lanch the flash sale": "launch flash sale",
            "lunch the flash sale": "launch flash sale",
            "lunch flash sale": "launch flash sale",
            "lunge the flash sale": "launch flash sale",
            "launge the flash sale": "launch flash sale",
            "long the flash sale": "launch flash sale",
            "long to the flash sale": "launch flash sale",
            "long the flash sail": "launch flash sale",
            "long to the flash sail": "launch flash sale",
            # repin / pop again 常见口语
            "pop the link again": "repin link",
            "pop link again": "repin link",
            "pin the link again": "repin link",
            "pin link again": "repin link",
            "unpin and pin again": "repin link",
            "unpin then pin again": "repin link",
            "取消置顶并重新置顶": "重新置顶",
            "取消置顶后重新置顶": "重新置顶",
            "取消置顶再置顶": "重新置顶",
            "再置顶一下": "重新置顶",
            "重置顶一下": "重新置顶",
            "重新顶一下": "重新置顶",
            "顶回去": "重新置顶",
            "开秒杀": "开始秒杀",
            "上秒杀": "开始秒杀",
            "启秒杀": "开始秒杀",
            "关秒杀": "停止秒杀",
            "停秒杀": "停止秒杀",
        }
        for src, dst in phrase_map.items():
            s = s.replace(src.lower(), dst.lower())

        # 更宽松的中文口令纠错：覆盖“置顶/链接”附近的常见同音误识别。
        s = re.sub(r"(?:制|置|至|致|治)(?:顶|定|丁|底|足)", "置顶", s)
        s = re.sub(r"(?:链|連|联|令|绿|练)(?:接|结|潔|節)", "链接", s)
        s = re.sub(r"(?:重|再)(?:置顶|顶一下|顶上|顶回去|顶回)", "重新置顶", s)
        s = re.sub(r"(?:开|上|启)(?:一下|一下子)?秒杀", "开始秒杀", s)
        s = re.sub(r"(?:(?:关|停)(?:一下|掉)?|下掉)秒杀", "停止秒杀", s)
        # 英文口令纠错：
        # 1) ASR 常把 pin 识别为 pick/peak/peek/pic/pig/pink（仅在命令语境替换，避免过度改写）。
        s = re.sub(
            r"\b(?:pick|peak|peek|pic|pig|pink)\b(?=\s+(?:the\s+)?(?:link|item|product|number|no|[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)\b)",
            "pin",
            s,
        )
        # 2) ASR 常把 "to the top" 识别为 "two/too the top"，统一到 "top" 语义。
        s = re.sub(r"\b(?:to|two|too)\s+the\s+top\b", " top", s)
        s = re.sub(r"\b(?:to|two|too)\s+top\b", " top", s)
        # 2.1) 唤醒词近音归一化（assistant/cohost）。
        s = re.sub(r"\bco[\s\-]*(?:host|hoster|hostess)\b", "cohost", s)
        s = re.sub(r"\b(?:assist(?:ant|ants|ance)?|a\s+system)\b", "assistant", s)
        # 3) ASR 常把 launch/flash/sale 读成近音词，统一为 "launch flash sale"。
        s = re.sub(
            r"\b(?:lounge|lanch|launche|lunch|lunge|launge|launching|launched|long)\b(?=\s+(?:the\s+)?(?:flash|slash|flesh|flush|flask)\b)",
            "launch",
            s,
        )
        s = re.sub(
            r"\b(?:flash|slash|flesh|flush|flask)\s+(?:a\s+)?(?:sale|sail|cell|seal)\b",
            "flash sale",
            s,
        )
        s = re.sub(
            r"\blaunch\s+(?:the\s+)?flash\s+sale\b",
            "launch flash sale",
            s,
        )
        # 4) pin/unpin/link 常见英语误识别归一化（只在命令语境近邻替换，降低误判）。
        s = re.sub(
            r"\b(?:pick|peak|peek|pig|pink|bin)\b(?=\s+(?:the\s+)?(?:link|item|product|number|no|[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)\b)",
            "pin",
            s,
        )
        s = re.sub(
            r"\b(?:pop|pup|pap|prop)\b(?=\s+(?:the\s+)?(?:link|item|product)\b)",
            "repin",
            s,
        )
        s = re.sub(
            r"\b(?:on\s*pin|and\s*pin|open)\b(?=\s+(?:the\s+)?(?:link|item|product|number|no|[0-9]+)\b)",
            "unpin",
            s,
        )
        s = re.sub(r"\b(?:ling|linq|linc)\b", "link", s)
        s = re.sub(r"\b(?:spin)\b(?=\s+(?:the\s+)?(?:link|item|product|number|no|[0-9]+)\b)", "pin", s)

        char_map = str.maketrans({
            "號": "号",
            "頂": "顶",
            "鏈": "链",
            "結": "结",
            "連": "连",
            "殺": "杀",
            "開": "开",
            "線": "线",
            "啟": "启",
            "臺": "台",
        })
        s = s.translate(char_map)
        return re.sub(r"\s+", " ", s).strip()

    def _prune_expired(self, cache, ttl_seconds):
        now = time.time()
        expired_keys = [k for k, ts in cache.items() if now - ts > ttl_seconds]
        for k in expired_keys:
            cache.pop(k, None)

    def _is_duplicate_message(self, user, text):
        norm = self._normalize_text(text)
        if not norm:
            return False
        key = f"{(user or '').lower()}::{norm}"
        now = time.time()
        last = self.message_cache.get(key)
        self.message_cache[key] = now
        self._prune_expired(self.message_cache, settings.SAME_MESSAGE_COOLDOWN_SECONDS * 2)
        return bool(last and now - last < settings.SAME_MESSAGE_COOLDOWN_SECONDS)

    def _is_duplicate_reply(self, user, reply):
        norm = self._normalize_text(reply)
        if not norm:
            return False

        now = time.time()
        user_key = f"{(user or '').lower()}::{norm}"
        last_user = self.user_reply_cache.get(user_key)
        if last_user and now - last_user < settings.SAME_USER_REPLY_COOLDOWN_SECONDS:
            return True

        last_global = self.global_reply_cache.get(norm)
        if last_global and now - last_global < settings.GLOBAL_REPLY_COOLDOWN_SECONDS:
            return True

        self.user_reply_cache[user_key] = now
        self.global_reply_cache[norm] = now
        self._prune_expired(self.user_reply_cache, settings.SAME_USER_REPLY_COOLDOWN_SECONDS * 2)
        self._prune_expired(self.global_reply_cache, settings.GLOBAL_REPLY_COOLDOWN_SECONDS * 3)
        return False

    def _remember_sent_message(self, text):
        norm = self._normalize_text(text)
        if not norm:
            return

        now = time.time()
        self.sent_message_cache[norm] = now

        # 发送形如 "@user xxx" 时，同时记录正文，便于过滤回显
        text = (text or "").strip()
        if text.startswith("@") and " " in text:
            body = text.split(" ", 1)[1].strip()
            body_norm = self._normalize_text(body)
            if body_norm:
                self.sent_message_cache[body_norm] = now

        self._prune_expired(self.sent_message_cache, settings.SELF_ECHO_TTL_SECONDS * 2)

    def _is_self_echo_message(self, user, text):
        if not settings.SELF_ECHO_IGNORE_ENABLED:
            return False

        user_lower = (user or "").strip().lower()
        if user_lower and user_lower in settings.SELF_USERNAMES:
            return True

        incoming = self._normalize_text(text)
        if len(incoming) < settings.SELF_ECHO_MIN_CHARS:
            return False

        now = time.time()
        self._prune_expired(self.sent_message_cache, settings.SELF_ECHO_TTL_SECONDS * 2)
        for sent_norm, ts in self.sent_message_cache.items():
            if now - ts > settings.SELF_ECHO_TTL_SECONDS:
                continue
            if len(sent_norm) < settings.SELF_ECHO_MIN_CHARS:
                continue
            if incoming == sent_norm:
                return True
            if sent_norm in incoming and len(sent_norm) >= settings.SELF_ECHO_MIN_CHARS:
                return True
            if incoming in sent_norm and len(incoming) >= settings.SELF_ECHO_MIN_CHARS:
                return True
        return False

    def _is_llm_candidate(self, text):
        """判断该弹幕是否应触发 LLM 回复。"""
        if not text:
            return False

        normalized = text.strip()
        lowered = normalized.lower()

        if "?" in normalized or "？" in normalized:
            return True

        if any(token in lowered for token in settings.REPLY_IGNORE_KEYWORDS):
            return False

        return any(token in lowered for token in settings.REPLY_TRIGGER_KEYWORDS)

    def _enforce_unified_output_language(self, text, language=None, force_tone=False):
        """
        统一输出语言入口：
        - 所有回复/暖场最终都按统一语言输出
        - 语气模板可任意语言，作为风格参考
        """
        raw = str(text or "").strip()
        if not raw:
            return raw
        lang_code = language or self._get_active_language()
        try:
            if hasattr(self.knowledge, "ensure_output_language"):
                return self.knowledge.ensure_output_language(
                    raw,
                    lang_code,
                    tone_template=self.tone_template,
                    force_tone=bool(force_tone),
                )
        except Exception as e:
            logger.debug(f"统一语言校正失败，回退原文: {e}")
        return raw

    def _is_command_user(self, user):
        """
        仅允许指定账号触发运营动作。
        默认不限制发送者；仅在显式开启 COMMAND_REQUIRE_ALLOWED_USERS 时校验白名单。
        """
        if not bool(getattr(settings, "COMMAND_REQUIRE_ALLOWED_USERS", False)):
            return True
        allowed = settings.COMMAND_ALLOWED_USERS
        if not allowed:
            return True
        return (user or "").strip().lower() in allowed

    def _parse_operation_command(self, user, text):
        """
        解析主播运营口令：
        1) 将3号链接置顶一下
        2) 将秒杀活动上架一下
        """
        if not text or not self._is_command_user(user):
            return None
        return self._parse_operation_command_text(text)

    def _parse_operation_command_text(self, text):
        if not text:
            return None

        normalized_input = self._normalize_voice_command_text(text)
        raw_lower = normalized_input if normalized_input else str(text).lower()
        normalized = re.sub(r"\s+", "", raw_lower)
        normalized = re.sub(r"[，。,.!！?？:：;；、~～]", "", normalized)
        ascii_text = re.sub(r"[^a-z0-9]+", " ", raw_lower).strip()
        ascii_tokens = [tok for tok in ascii_text.split() if tok]
        en_top_position_hint = bool(re.search(r"(?:totop|ontop|twotop|tootop|tothetop|onthetop|twothetop|toothetop)", normalized))
        cn_digit_map = {
            "零": 0, "〇": 0,
            "一": 1, "壹": 1, "幺": 1,
            "二": 2, "两": 2, "贰": 2, "貳": 2,
            "三": 3, "叁": 3, "參": 3,
            "四": 4, "肆": 4,
            "五": 5, "伍": 5,
            "六": 6, "陆": 6, "陸": 6,
            "七": 7, "柒": 7,
            "八": 8, "捌": 8,
            "九": 9, "玖": 9,
        }
        en_num_map = {
            "zero": 0,
            "one": 1, "first": 1,
            "two": 2, "second": 2,
            "three": 3, "third": 3,
            "four": 4, "fourth": 4,
            "five": 5, "fifth": 5,
            "six": 6, "sixth": 6,
            "seven": 7, "seventh": 7,
            "eight": 8, "eighth": 8,
            "nine": 9, "ninth": 9,
            "ten": 10, "tenth": 10,
            "eleven": 11, "eleventh": 11,
            "twelve": 12, "twelfth": 12,
            "thirteen": 13, "thirteenth": 13,
            "fourteen": 14, "fourteenth": 14,
            "fifteen": 15, "fifteenth": 15,
            "sixteen": 16, "sixteenth": 16,
            "seventeen": 17, "seventeenth": 17,
            "eighteen": 18, "eighteenth": 18,
            "nineteen": 19, "nineteenth": 19,
            "twenty": 20, "twentieth": 20,
            # 英文 ASR 常见同音误识别（仅在“口令索引位”被正则捕获时生效）
            "to": 2, "too": 2,
            "for": 4, "fore": 4,
            "tree": 3, "free": 3,
            "won": 1,
            "ate": 8,
            "sex": 6, "sicks": 6,
        }
        cn_num_token = r"[零〇一二两三四五六七八九十壹贰貳叁參肆伍陆陸柒捌玖拾廿卅幺]{1,4}"
        # 英文数字词按“长词优先”排列，避免 seventeen 被 seven 抢先匹配。
        en_num_token = (
            r"seventeenth|seventeen|seventh|seven|"
            r"thirteenth|thirteen|third|three|"
            r"fourteenth|fourteen|fourth|four|"
            r"fifteenth|fifteen|fifth|five|"
            r"sixteenth|sixteen|sixth|six|"
            r"eighteenth|eighteen|eighth|eight|"
            r"nineteenth|nineteen|ninth|nine|"
            r"twentieth|twenty|second|two|"
            r"eleventh|eleven|first|one|"
            r"twelfth|twelve|tenth|ten|"
            r"too|to|fore|for|tree|free|won|ate|sex|sicks|zero"
        )

        def _parse_cn_number(token):
            token = (token or "").strip()
            if not token:
                return None
            token = token.replace("號", "号").replace("#", "")
            token = re.sub(r"^第", "", token)
            token = re.sub(r"(?:个|號|号|链接|连接|商品|橱窗)$", "", token)
            repl = str.maketrans({
                "壹": "一", "贰": "二", "貳": "二", "叁": "三", "參": "三",
                "肆": "四", "伍": "五", "陆": "六", "陸": "六", "柒": "七",
                "捌": "八", "玖": "九", "拾": "十", "〇": "零",
            })
            token = token.translate(repl)
            if token in cn_digit_map:
                return cn_digit_map[token]
            if token == "廿":
                return 20
            if token == "卅":
                return 30
            if token.startswith("廿"):
                ones = _parse_cn_number(token[1:])
                return (20 + (ones or 0)) if token[1:] else 20
            if token.startswith("卅"):
                ones = _parse_cn_number(token[1:])
                return (30 + (ones or 0)) if token[1:] else 30
            if token == "十":
                return 10
            if "十" in token:
                parts = token.split("十", 1)
                left = parts[0]
                right = parts[1]
                tens = cn_digit_map.get(left, 1) if left else 1
                ones = cn_digit_map.get(right, 0) if right else 0
                val = tens * 10 + ones
                return val if val > 0 else None
            return None

        def _parse_index(raw):
            if not raw:
                return None
            raw = str(raw).strip()
            if raw.startswith("#"):
                raw = raw[1:]
            if raw.isdigit():
                idx_val = int(raw)
                return idx_val if idx_val > 0 else None
            idx_val = _parse_cn_number(raw)
            if idx_val and idx_val > 0:
                return idx_val
            idx_val = en_num_map.get(str(raw).lower())
            if idx_val and idx_val > 0:
                return idx_val
            return None

        def _token_like(token, candidate):
            token = str(token or "").strip().lower()
            candidate = str(candidate or "").strip().lower()
            if (not token) or (not candidate):
                return False
            if token == candidate:
                return True
            # 数字词只允许精准或常见同音，避免把普通词误判成序号。
            if candidate in en_num_map:
                return token in {candidate, "to", "too", "for", "fore", "tree", "free", "won", "ate", "sex", "sicks"}
            # 短命令词（pin/top）要求更严格，避免把 stop/topic 等误判成 top。
            if len(candidate) <= 3:
                return False
            if token.isdigit() or candidate.isdigit():
                return False
            if abs(len(token) - len(candidate)) > 2:
                return False
            return SequenceMatcher(None, token, candidate).ratio() >= 0.76

        def _has_token_like(candidates):
            return any(_token_like(tok, cand) for tok in ascii_tokens for cand in candidates)

        def _has_ascii_token(candidates):
            cand_set = {str(c).strip().lower() for c in candidates if str(c).strip()}
            return any(str(tok).strip().lower() in cand_set for tok in ascii_tokens)

        def _first_ascii_index_token():
            for tok in ascii_tokens:
                idx = _parse_index(tok)
                if idx:
                    return tok
            return None

        def _extract_link_index(strict_hint=True):
            idx_token = (
                r"[0-9]+|"
                + cn_num_token
                + r"|"
                + en_num_token
            )
            hint_forward = [
                rf"(?:link|item|product)(?:number|no|#)?({idx_token})",
                rf"(?:number|no|#)({idx_token})",
                rf"(?:第)({idx_token})",
                rf"(?:第)?({idx_token})(?:个)?(?:链接|连接|商品|橱窗)",
                rf"(?:第)?({idx_token})(?:号|#|个)(?:链接|连接|商品|橱窗)?",
                rf"(?:第)?({idx_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)?(?:秒杀|活动)",
            ]
            hint_reverse = [
                rf"(#?{idx_token})(?:号|#)?(?:link|item|product|链接|连接|商品|橱窗)",
                rf"(?:第)?({idx_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)",
                rf"(?:link|item|product)(?:number|no|#)?(?:the)?({idx_token})",
            ]
            for pattern in hint_forward + hint_reverse:
                m = re.search(pattern, normalized)
                if m:
                    idx = _parse_index(m.group(1))
                    if idx:
                        return idx

            if strict_hint:
                return None

            fallback_num = re.search(rf"({idx_token})", normalized)
            if fallback_num:
                idx = _parse_index(fallback_num.group(1))
                if idx:
                    return idx
            ascii_token = _first_ascii_index_token()
            return _parse_index(ascii_token) if ascii_token else None

        # 秒杀命令优先级最高，避免 ASR 混入“置顶/取消置顶”词时误触发商品操作。
        flash_markers = [
            "秒杀", "flashsale", "flashdeal", "flashpromo", "flashpromotion",
            "limiteddeal", "limitedoffer", "promotion", "promo", "deal", "saleevent"
        ]
        flash_stop_actions_cn = [
            "结束", "停止", "下架", "关闭", "撤下", "停一下", "收一下",
            "关", "停", "关掉", "停掉", "下掉",
        ]
        flash_stop_actions_en = [
            "end", "stop", "close", "disable", "off", "finish", "over", "remove",
        ]
        flash_actions_cn = ["上架", "开启", "开始", "开一下", "挂一下", "开", "上", "上线", "挂", "拉起"]
        flash_actions_en = [
            "launch", "start", "enable", "open", "golive", "live", "on", "run", "push", "publish",
            # ASR 英文动词误识别兜底
            "lounge", "lanch", "launche", "lunch", "lunge", "launge", "long"
        ]
        has_flash_marker = any(marker in normalized for marker in flash_markers)
        has_en_flash_surface = (
            any(k in normalized for k in ["flashsale", "flashdeal", "flashpromo", "flashpromotion", "limiteddeal", "limitedoffer", "saleevent"])
            or (
                _has_token_like(["flash", "slash", "flesh", "flush", "flask"])
                and _has_token_like(["sale", "sail", "cell", "seal", "deal", "promo", "promotion"])
            )
            or (
                _has_token_like(["limited"])
                and _has_token_like(["offer", "deal", "promotion", "promo", "sale", "event"])
            )
        )
        has_cn_flash_stop = any(k in normalized for k in flash_stop_actions_cn)
        has_en_flash_stop = _has_ascii_token(flash_stop_actions_en) or ("turnoff" in normalized)
        has_cn_flash_start = any(k in normalized for k in flash_actions_cn)
        has_en_flash_start = _has_ascii_token(flash_actions_en) or ("turnon" in normalized)

        flash_stop_cmd = (
            ("秒杀" in normalized and any(k in normalized for k in ["结束", "停止", "下架", "关闭", "撤下", "停一下", "关", "停", "关掉", "停掉", "下掉"]))
            or ("结束秒杀" in normalized)
            or ("停止秒杀" in normalized)
            or ("下架秒杀" in normalized)
            or ("活动" in normalized and "结束" in normalized and "秒杀" in normalized)
            or (
                (has_flash_marker or has_en_flash_surface)
                and (has_cn_flash_stop or has_en_flash_stop)
            )
            or ("flash" in normalized and "sale" in normalized and any(k in normalized for k in ["stop", "end", "close", "disable", "off", "finish", "over", "remove"]))
            or ("endflashsale" in normalized)
            or ("stopflashsale" in normalized)
            or ("turnoffflashsale" in normalized)
            or ("closeflashsale" in normalized)
            or (
                _has_token_like(["flash", "slash", "flesh", "flush", "flask"])
                and _has_token_like(["sale", "sail", "cell", "seal", "deal", "promo", "promotion"])
                and _has_token_like(["stop", "end", "close", "disable", "off", "finish", "over", "remove"])
            )
        )
        if flash_stop_cmd:
            return {"action": "stop_flash_sale"}

        flash_sale_cmd = (
            ("秒杀" in normalized and any(k in normalized for k in ["上架", "开启", "开始", "开一下", "挂一下", "开", "上", "挂", "上线", "拉起"]))
            or ("秒杀活动" in normalized and any(k in normalized for k in ["上架", "上线", "开一下", "开始", "开", "挂", "上"]))
            or ("活动" in normalized and "上架" in normalized and "秒杀" in normalized)
            or ("上架" in normalized and "秒杀" in normalized)
            or (
                (has_flash_marker or has_en_flash_surface)
                and (has_cn_flash_start or has_en_flash_start)
            )
            or ("flash" in normalized and "sale" in normalized and any(k in normalized for k in ["start", "launch", "lounge", "lanch", "launche", "lunch", "lunge", "launge", "long", "on", "enable", "open"]))
            or ("put" in normalized and "flashsale" in normalized and "live" in normalized)
            or ("put" in normalized and "flashdeal" in normalized and any(k in normalized for k in ["live", "on", "up"]))
            or ("start" in normalized and any(k in normalized for k in ["flashsale", "flashpromo", "promo", "promotion"]))
            or ("golive" in normalized and "flash" in normalized)
            or (
                any(k in normalized for k in ["flashsale", "flashdeal", "flashpromo", "flashpromotion"])
                and any(k in normalized for k in ["start", "launch", "enable", "open", "on", "up", "live", "golive"])
            )
            or ("makeflashsale" in normalized and "live" in normalized)
            or ("turnonflashsale" in normalized)
            or ("putflashsaleon" in normalized)
            or (
                _has_token_like(["flash", "slash", "flesh", "flush", "flask"])
                and _has_token_like(["sale", "sail", "cell", "seal", "deal", "promo", "promotion"])
                and _has_token_like(["launch", "lounge", "lanch", "launche", "lunch", "lunge", "launge", "long", "start", "enable", "open", "live", "golive", "publish", "push", "run"])
            )
        )
        if flash_sale_cmd:
            flash_idx = _extract_link_index(strict_hint=True)
            if flash_idx:
                return {"action": "start_flash_sale", "link_index": flash_idx}
            return {"action": "start_flash_sale"}

        # 取消置顶后重新置顶：支持“pop the link again / repin link / 取消置顶并重新置顶”
        repin_patterns = [
            rf"(?:repin|pinagain|pinitagain|pinback|restick|refeature)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)",
            rf"(?:repin|pinagain|pinitagain|pinback|restick|refeature)(?:the)?(?:link|item|product)(?:number|no|#)?([0-9]+)",
            rf"(?:repin|pop)(?:the)?(?:link|item|product)(?:number|no|#)?({en_num_token}|[0-9]+)(?:again)?",
            rf"(?:unpin|depin|untop)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:and|then)?(?:pin|repin)",
            rf"(?:pin|repin)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:again|back)",
            rf"(?:取消置顶并重新置顶|取消置顶后重新置顶|取消置顶再置顶|重新置顶)(?:第)?([0-9]+|{cn_num_token})(?:号|个)?(?:链接|连接|商品|橱窗)?",
        ]
        for pattern in repin_patterns:
            m = re.search(pattern, normalized)
            if m:
                idx = _parse_index(m.group(1))
                if idx:
                    return {"action": "repin_product", "link_index": idx}

        repin_intent = any(
            k in normalized
            for k in [
                "重新置顶",
                "再置顶",
                "重置顶",
                "repin",
                "pinagain",
                "pinitagain",
                "pinback",
                "popthelinkagain",
                "poplinkagain",
                "repinlink",
                "unpinandpin",
                "unpinthenpin",
                "restick",
                "refeature",
            ]
        )
        if repin_intent:
            idx = _extract_link_index(strict_hint=True)
            if idx:
                return {"action": "repin_product", "link_index": idx}
            return {"action": "repin_product"}

        # 取消置顶：将3号链接取消置顶 / 取消3号链接置顶 / unpin link 3
        unpin_patterns = [
            rf"(?:将|把)?([0-9]+|{cn_num_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)?(?:取消置顶|取消顶置|撤销置顶|去掉置顶|下掉置顶|取消pin)",
            rf"(?:取消|撤销|去掉|下掉)(?:第)?([0-9]+|{cn_num_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)?(?:置顶|顶置|pin)?",
            rf"(?:取消置顶|取消顶置|撤销置顶|去掉置顶|下掉置顶)(?:第)?([0-9]+|{cn_num_token})(?:(?:号|#|个)(?:链接|连接|商品|橱窗)?|(?:链接|连接|商品|橱窗))",
            rf"(?:unpin|depin|untop|unset|unstick|unfeature|takeofftop|takefromtop)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)",
            rf"(?:remove|cancel)(?:the)?pin(?:from)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)",
            rf"(?:take|move|remove)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:off|from)?top",
            rf"(?:link|item|product)(?:number|no|#)?({en_num_token}|[0-9]+)(?:unpin|depin|untop|unset|remove(?:the)?pin|removefromtop)",
            # 纯数字口令需要带索引语义，避免误判。
            rf"(?:unpin|depin|untop|unset|unstick|unfeature)(?:the)?(?:link|item|product)(?:number|no|#)?([0-9]+)",
            rf"(?:remove|cancel)(?:the)?pin(?:from)?(?:link|item|product)(?:number|no|#)?([0-9]+)",
        ]
        for pattern in unpin_patterns:
            m = re.search(pattern, normalized)
            if m:
                idx = _parse_index(m.group(1))
                if idx:
                    return {"action": "unpin_product", "link_index": idx}

        has_unpin_intent = any(
            k in normalized
            for k in [
                "取消置顶",
                "取消顶置",
                "撤销置顶",
                "去掉置顶",
                "下掉置顶",
                "移除置顶",
                "unpin",
                "depin",
                "untop",
                "removepin",
                "cancelpin",
                "unsetpin",
                "unstick",
                "unfeature",
                "removefromtop",
                "takeofftop",
                "takefromtop",
            ]
        )
        # 兼容“取消链接置顶/撤销链接置顶”等中间夹有目标词的口令。
        if not has_unpin_intent:
            has_unpin_intent = bool(
                re.search(
                    rf"(?:取消|撤销|去掉|下掉|移除)(?:第?(?:[0-9]+|{cn_num_token})(?:号|#|个)?)?(?:链接|连接|商品|橱窗)?(?:置顶|顶置)",
                    normalized,
                )
            )
        if not has_unpin_intent:
            has_unpin_intent = bool(
                re.search(
                    r"(?:remove|cancel)(?:the)?pin(?:from)?(?:link|item|product)?",
                    normalized,
                )
            )
        if has_unpin_intent:
            fallback_num = re.search(
                rf"([0-9]+|{cn_num_token}|{en_num_token})",
                normalized
            )
            if fallback_num:
                raw_idx = fallback_num.group(1)
                # “取消置顶一下”中的“一下”不应当被识别成 1 号链接。
                if raw_idx in {"一", "壹", "幺"}:
                    has_index_hint = any(
                        h in normalized for h in ["一号", "第一", "第一个", "1号", "1个", "#1", "link1", "item1", "product1", "number1", "no1"]
                    )
                    if (not has_index_hint) and ("一下" in normalized):
                        raw_idx = None
                idx = _parse_index(raw_idx)
                if idx:
                    return {"action": "unpin_product", "link_index": idx}
            # 固定行相对点击模式下，取消置顶必须给出序号，避免误点。
            return None

        # 置顶指定链接：将3号链接置顶 / 置顶3号链接 / 将三号链接置顶
        pin_patterns = [
            rf"(?:将|把)?([0-9]+|{cn_num_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)?(?:置顶|顶一下|顶上去|顶上|置上去|pin|top)",
            rf"(?:置顶|pin|top)(?:第)?([0-9]+|{cn_num_token})(?:(?:号|#|个)(?:链接|连接|商品|橱窗)?|(?:链接|连接|商品|橱窗))",
            rf"(?:把|将)?(?:第)?([0-9]+|{cn_num_token})(?:号|#|个)?(?:链接|连接|商品|橱窗)(?:给我)?(?:置顶|顶上去|顶一下)",
            rf"(?:pin|top|feature|stick|highlight)(?:the)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:link|item|product)?",
            rf"(?:pin|pick|top|feature|stick|highlight|choose|select)(?:the)?({en_num_token}|[0-9]+)(?:to|two|too)?(?:the)?top",
            r"(?:link|item|product)([0-9]+)(?:pin|top)",
            rf"(?:link|item|product)(?:number|no|#)?({en_num_token}|[0-9]+)(?:please)?(?:pin|top|feature|stick|highlight)",
            rf"(?:pin|top|feature|stick|highlight)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)",
            rf"(?:put|set|move|bring)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:on|to|into)?top",
            rf"(?:make)(?:the)?(?:link|item|product)?(?:number|no|#)?({en_num_token}|[0-9]+)(?:at|on)?top",
            rf"(?:link|item|product)(?:number|no|#)?({en_num_token}|[0-9]+)(?:to|into|on)?top",
            # 数字口令必须带索引语义，避免把 "top1 / topic3" 误判为置顶命令。
            rf"(?:pin|top|feature|stick|highlight)(?:the)?(?:link|item|product)(?:number|no|#)?([0-9]+)",
            rf"(?:pin|top|feature|stick|highlight)(?:number|no|#)([0-9]+)",
            rf"(?:bring|move)(?:link|item|product)?([0-9]+)(?:totop|on?top)",
        ]
        for pattern in pin_patterns:
            m = re.search(pattern, normalized)
            if m:
                idx = _parse_index(m.group(1))
                if idx:
                    return {"action": "pin_product", "link_index": idx}

        # 语音识别常见漏词兜底：只要出现“置顶/pin/top”并带数字，就执行置顶
        en_command_word = (
            bool(re.search(r"(^|[^a-z])(pin|top|feature|stick|highlight)([^a-z]|$)", raw_lower))
            or _has_token_like(["pin", "top", "feature", "stick", "highlight"])
        )
        en_target_hint = _has_token_like(["link", "item", "product", "number", "no"])
        en_top_hint = _has_token_like(["top"])
        has_pin_intent = ("置顶" in normalized) or ("顶上" in normalized) or (en_command_word and (en_target_hint or en_top_hint or en_top_position_hint))
        if has_pin_intent:
            raw_idx = None
            fallback_num = re.search(
                rf"([0-9]+|{cn_num_token}|{en_num_token})",
                normalized
            )
            if fallback_num:
                raw_idx = fallback_num.group(1)
            if not raw_idx:
                raw_idx = _first_ascii_index_token()
            if raw_idx:
                # 规避“置顶一下”等口头语误触发：单独“一”只有在带索引语义时才作为 1 号链接
                if raw_idx in {"一", "壹", "幺"}:
                    has_index_hint = any(
                        h in normalized for h in ["一号", "第一", "第一个", "1号", "1个", "#1", "link1", "item1", "product1", "number1", "no1"]
                    )
                    if (not has_index_hint) and ("一下" in normalized):
                        raw_idx = None
                # 规避英语口头语“this one”等：英文数字词需要索引语义提示（link/item/number/no）。
                if raw_idx and re.fullmatch(r"[a-z]+", raw_idx):
                    has_index_hint = any(
                        h in normalized for h in ["link", "item", "product", "number", "no", "#", "号", "链接", "商品"]
                    )
                    if not has_index_hint and en_top_position_hint:
                        has_index_hint = True
                    if not has_index_hint:
                        raw_idx = None
                # 纯数字也要求出现索引语义，避免 "top1 这款不错" 误触发。
                if raw_idx and str(raw_idx).isdigit():
                    has_index_hint = any(
                        h in normalized for h in ["号", "第", "#", "链接", "商品", "link", "item", "product", "number", "no"]
                    )
                    # 允许 "pin 3" 这类极简命令，但要求有独立英文命令词（避免 topic3）。
                    if not has_index_hint and not bool(re.search(r"(^|[^a-z])(pin|feature|stick)([^a-z]|$)", raw_lower)):
                        raw_idx = None
                # 英文场景需要避免把 "top1" 当作指令；只有显式索引语义才放行。
                if raw_idx and re.fullmatch(r"[0-9]+", str(raw_idx)):
                    if "top" in ascii_text and "top " not in ascii_text and " top " not in f" {ascii_text} ":
                        has_index_hint = any(h in normalized for h in ["link", "item", "product", "number", "no"])
                        if not has_index_hint:
                            raw_idx = None

                idx = _parse_index(raw_idx)
                if idx:
                    return {"action": "pin_product", "link_index": idx}

        return None

    def _execute_operation_command(self, command, trigger_source="", log_entry=None, skip_page_prepare=False):
        if not command:
            return False

        action = command.get("action")
        receipt = {}
        reconnect_reason = ""
        source = trigger_source or "unknown"

        if not bool(skip_page_prepare):
            page_ready = self._prepare_action_page_for_command(command, trigger_source=source)
            if not bool(page_ready.get("ok")):
                reason = str(page_ready.get("reason") or "operable_page_not_found")
                logger.warning(f"运营动作取消执行: source={source}, action={action}, reason={reason}")
                if log_entry is not None:
                    log_entry["action"] = str(action or "unknown")
                    log_entry["status"] = "action_failed"
                    log_entry["action_receipt"] = reason
                return False

        screen_ocr_mode = self.get_web_info_source_mode() == "screen_ocr"

        if not self.vision.ensure_connection():
            reconnect_reason = "precheck_connection_lost"
            # 先尝试强制重连当前会话，再尝试全量 connect_browser。
            if not self.vision.ensure_connection(force=True):
                if not screen_ocr_mode:
                    self.connect_browser()

        if (not screen_ocr_mode) and (not self.vision.page):
            logger.warning(f"运营动作取消执行: source={source}, reason=browser_not_connected")
            if log_entry is not None:
                log_entry["action"] = str(action or "unknown")
                log_entry["status"] = "action_failed"
                log_entry["action_receipt"] = "browser_not_connected"
            return False
        if screen_ocr_mode and (not self.vision.ensure_connection()):
            logger.warning(f"运营动作取消执行: source={source}, reason=screen_capture_unavailable")
            if log_entry is not None:
                log_entry["action"] = str(action or "unknown")
                log_entry["status"] = "action_failed"
                log_entry["action_receipt"] = "screen_capture_unavailable"
            return False

        def _action_name_from_command():
            idx = command.get("link_index")
            if action == "pin_product":
                return f"pin_product_{idx}"
            if action == "unpin_product":
                return f"unpin_product_{idx}" if idx else "unpin_product"
            if action == "start_flash_sale":
                return f"start_flash_sale_{idx}" if idx else "start_flash_sale"
            if action == "stop_flash_sale":
                return "stop_flash_sale"
            if action == "repin_product":
                return f"repin_product_{idx}" if idx else "repin_product"
            return str(action or "unknown")

        def _run_operation_once():
            planner_enabled = bool(getattr(settings, "OPS_LLM_PLAN_ENABLED", False))
            if planner_enabled and hasattr(self.operations, "execute_action_with_plan"):
                try:
                    plan_ok = self.operations.execute_action_with_plan(command, trigger_source=trigger_source or "unknown")
                    if plan_ok is not None:
                        return bool(plan_ok), _action_name_from_command(), True
                except Exception as e:
                    logger.warning(f"受限计划执行失败，回退旧链路: {e}")

            if action == "pin_product":
                idx = command.get("link_index")
                return bool(self.operations.pin_product(link_index=idx)), _action_name_from_command(), False
            if action == "unpin_product":
                idx = command.get("link_index")
                return bool(self.operations.unpin_product(link_index=idx)), _action_name_from_command(), False
            if action == "start_flash_sale":
                return bool(self.operations.start_flash_sale()), _action_name_from_command(), False
            if action == "stop_flash_sale":
                return bool(self.operations.stop_flash_sale()), _action_name_from_command(), False
            if action == "repin_product":
                idx = command.get("link_index")
                unpin_ok = bool(self.operations.unpin_product(link_index=idx))
                time.sleep(0.08)
                pin_ok = bool(self.operations.pin_product(link_index=idx))
                return bool(unpin_ok and pin_ok), _action_name_from_command(), False
            return False, _action_name_from_command(), False

        ok, action_name, _planned = _run_operation_once()

        if hasattr(self.operations, "get_last_action_receipt"):
            try:
                receipt = self.operations.get_last_action_receipt() or {}
            except Exception:
                receipt = {}

        # 页面断连/不可操作导致失败时，重连后再重试一次，降低“说了没反应”的概率。
        retryable_reason = str((receipt or {}).get("reason") or "")
        need_retry = (
            (not ok)
            and (
                reconnect_reason
                or ("screen_capture_unavailable" in retryable_reason)
                or ("browser_not_connected" in retryable_reason)
                or ("non_operable_page" in retryable_reason)
            )
        )
        if need_retry:
            retried = bool(self.vision.ensure_connection(force=True))
            if (not retried) and (not screen_ocr_mode):
                retried = bool(self.connect_browser())
            if retried:
                ok, action_name, _planned_retry = _run_operation_once()
                if hasattr(self.operations, "get_last_action_receipt"):
                    try:
                        receipt = self.operations.get_last_action_receipt() or {}
                    except Exception:
                        receipt = {}

        logger.info(
            f"运营动作触发: source={source}, action={action_name}, ok={ok}, "
            f"receipt_reason={receipt.get('reason') if isinstance(receipt, dict) else ''}"
        )
        if isinstance(receipt, dict):
            try:
                trace = ((receipt.get("detail") or {}).get("human_trace") or {})
                if trace:
                    logger.info(
                        "运营动作拟人统计: "
                        f"action={trace.get('action')} ok={trace.get('ok')} "
                        f"elapsed={trace.get('elapsed_ms')}ms "
                        f"delay={trace.get('delay_ms')}ms/{trace.get('delay_count')}次 "
                        f"last_delay={trace.get('last_delay_reason')}:{trace.get('last_delay_ms')}ms"
                    )
            except Exception:
                pass

        if log_entry is not None:
            log_entry["action"] = action_name
            log_entry["status"] = "action_done" if ok else "action_failed"
            if receipt:
                log_entry["action_receipt"] = receipt.get("reason") or receipt.get("stage") or ""
        return ok

    def _is_duplicate_voice_command(self, text):
        norm = self._normalize_text(text)
        if not norm:
            return False
        now = time.time()
        last = self.voice_command_cache.get(norm)
        self.voice_command_cache[norm] = now
        self._prune_expired(self.voice_command_cache, settings.VOICE_COMMAND_COOLDOWN_SECONDS * 2)
        return bool(last and now - last < settings.VOICE_COMMAND_COOLDOWN_SECONDS)

    def _voice_action_key(self, command):
        if not isinstance(command, dict):
            return ""
        action = (command.get("action") or "").strip().lower()
        if action == "pin_product":
            idx = command.get("link_index")
            if idx:
                return f"pin_product:{idx}"
            return "pin_product"
        if action == "unpin_product":
            idx = command.get("link_index")
            if idx:
                return f"unpin_product:{idx}"
            return "unpin_product"
        if action == "start_flash_sale":
            idx = command.get("link_index")
            if idx:
                return f"start_flash_sale:{idx}"
            return "start_flash_sale"
        if action == "stop_flash_sale":
            return "stop_flash_sale"
        if action == "repin_product":
            idx = command.get("link_index")
            if idx:
                return f"repin_product:{idx}"
            return "repin_product"
        return action

    def _is_duplicate_voice_action(self, command):
        key = self._voice_action_key(command)
        if not key:
            return False
        now = time.time()
        last = self.voice_action_cache.get(key)
        self.voice_action_cache[key] = now
        self._prune_expired(self.voice_action_cache, settings.VOICE_COMMAND_COOLDOWN_SECONDS * 2)
        return bool(last and now - last < settings.VOICE_COMMAND_COOLDOWN_SECONDS)

    def _is_duplicate_danmu_action(self, command):
        key = self._voice_action_key(command)
        if not key:
            return False
        cooldown = max(
            0.0,
            float(
                getattr(
                    settings,
                    "DANMU_COMMAND_COOLDOWN_SECONDS",
                    settings.VOICE_COMMAND_COOLDOWN_SECONDS,
                )
                or 0.0
            ),
        )
        now = time.time()
        last = self.danmu_action_cache.get(key)
        self.danmu_action_cache[key] = now
        self._prune_expired(self.danmu_action_cache, max(1.0, cooldown * 2))
        if cooldown <= 0:
            return False
        return bool(last and now - last < cooldown)

    def _append_voice_input_log(self, source, text, lang=None, status="captured", note="", command=None):
        action_key = ""
        if isinstance(command, dict):
            action_key = self._voice_action_key(command)
            if not action_key:
                action_key = command.get("action") or ""
        self.voice_input_log.appendleft(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "source": source or "voice",
                "text": str(text or ""),
                "lang": lang or "",
                "status": status,
                "note": note or "",
                "command": action_key,
            }
        )

    def get_recent_voice_inputs(self, limit=80):
        try:
            n = max(1, int(limit))
        except Exception:
            n = 80
        return list(self.voice_input_log)[:n]

    def _pass_voice_wake_word(self, text):
        wake_words = settings.VOICE_COMMAND_WAKE_WORDS
        if not wake_words:
            return True
        lowered = self._normalize_voice_command_text(text)
        normalized = self._normalize_text(lowered)
        text_tokens = [t for t in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lowered).split() if t]

        def _token_similar(a, b):
            a = str(a or "").strip().lower()
            b = str(b or "").strip().lower()
            if (not a) or (not b):
                return False
            if a == b:
                return True
            if abs(len(a) - len(b)) > 2:
                return False
            return SequenceMatcher(None, a, b).ratio() >= 0.8

        for w in wake_words:
            w = (w or "").strip().lower()
            if not w:
                continue
            if w in lowered:
                return True
            w_norm = self._normalize_text(self._normalize_voice_command_text(w))
            if w_norm and w_norm in normalized:
                return True
            wake_tokens = [t for t in re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", w).split() if t]
            if wake_tokens and text_tokens:
                hit = True
                for wt in wake_tokens:
                    if not any(_token_similar(tt, wt) for tt in text_tokens):
                        hit = False
                        break
                if hit:
                    return True
        return False

    def _poll_voice_commands(self):
        """轮询语音识别结果并触发运营动作。"""
        if not self.voice_command_enabled:
            return
        # ASR 对比测试时只保留测试通道，避免与命令轮询争抢同一批转写结果。
        if self.cloud_asr_test_enabled:
            return
        if self.voice.requires_browser_page() and not self.vision.page:
            return

        now = time.time()
        if now - self.last_voice_poll_at < settings.VOICE_COMMAND_POLL_INTERVAL_SECONDS:
            return
        self.last_voice_poll_at = now

        active_language = self._get_active_language()
        fallback_languages = self._get_voice_fallback_languages(active_language)
        started = self.voice.ensure_started(
            language=active_language,
            fallback_languages=fallback_languages,
            silence_restart_seconds=settings.VOICE_COMMAND_SILENCE_RESTART_SECONDS,
        )
        if not started:
            if now - self.last_voice_health_log_at >= settings.VOICE_COMMAND_HEALTH_LOG_INTERVAL_SECONDS:
                state = self.voice.get_state()
                err = state.get("error") if isinstance(state, dict) else None
                logger.warning(f"语音通道不可用，已等待重试。请检查麦克风权限。error={err}")
                self.last_voice_health_log_at = now
            return

        state = self.voice.get_state()
        last_result_at = state.get("lastResultAt") if isinstance(state, dict) else None
        running = bool(state.get("running")) if isinstance(state, dict) else False
        voice_err = state.get("error") if isinstance(state, dict) else None
        last_audio_rms = state.get("lastAudioRms") if isinstance(state, dict) else None
        no_text_count = state.get("noTextCount") if isinstance(state, dict) else None
        device_name = state.get("deviceName") if isinstance(state, dict) else None
        runtime_provider = ""
        if isinstance(state, dict):
            runtime_provider = str(
                state.get("runtimeProvider")
                or state.get("provider")
                or ""
            ).strip()
        provider_note = f"asr={runtime_provider}" if runtime_provider else "asr=unknown"
        if voice_err and now - self.last_voice_health_log_at >= settings.VOICE_COMMAND_HEALTH_LOG_INTERVAL_SECONDS:
            logger.warning(f"语音识别异常: err={voice_err}, rms={last_audio_rms}, no_text_count={no_text_count}, device={device_name}, state={state}")
            self.last_voice_health_log_at = now
        elif (
            running
            and (not last_result_at)
            and isinstance(no_text_count, int)
            and no_text_count >= 5
            and now - self.last_voice_health_log_at >= settings.VOICE_COMMAND_HEALTH_LOG_INTERVAL_SECONDS
        ):
            logger.warning(
                "语音持续无文本: "
                f"no_text_count={no_text_count}, rms={last_audio_rms}, device={device_name}, "
                f"provider={state.get('provider') if isinstance(state, dict) else None}"
            )
            self.last_voice_health_log_at = now
        if isinstance(voice_err, str) and "google_network_error" in voice_err.lower():
            if now - self._google_voice_error_last_at > 120:
                self._google_voice_error_streak = 0
            self._google_voice_error_streak += 1
            self._google_voice_error_last_at = now
            current_provider = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local").lower()
            if (
                current_provider == "google"
                and self._google_voice_error_streak >= 2
                and bool(getattr(settings, "VOICE_ASR_AUTO_SWITCH_ON_TIMEOUT", False))
            ):
                switched = self.set_voice_asr_provider("whisper_local")
                if switched:
                    logger.warning("检测到 Google ASR 持续超时，已自动切换到 whisper_local。")
                    self._append_voice_input_log(
                        source="system",
                        text="",
                        lang=active_language,
                        status="provider_switch",
                        note="google_timeout->whisper_local",
                        command=None,
                    )
                    self._google_voice_error_streak = 0
        else:
            self._google_voice_error_streak = 0
        silence_too_long = False
        if last_result_at:
            try:
                silence_too_long = (
                    now - (float(last_result_at) / 1000.0)
                    > settings.VOICE_COMMAND_SILENCE_RESTART_SECONDS * 2
                )
            except (TypeError, ValueError):
                silence_too_long = False

        # 仅在“未运行 + 长静默 + 超过重启冷却”时强制重启，避免频繁重启拖慢主循环。
        if (
            (not running)
            and silence_too_long
            and now - self.last_voice_forced_restart_at >= settings.VOICE_COMMAND_FORCE_RESTART_MIN_SECONDS
        ):
            restarted = self.voice.start(
                language=active_language,
                fallback_languages=fallback_languages,
                silence_restart_seconds=settings.VOICE_COMMAND_SILENCE_RESTART_SECONDS,
            )
            self.last_voice_forced_restart_at = now
            if restarted:
                logger.info("语音通道长静默后已执行一次强制重启")

        # 用户要求：不使用字幕兜底，避免无效文本干扰指令识别。
        transcripts = self.voice.collect_command_candidates(include_subtitle=False)
        for item in transcripts:
            text = item.get("text", "")
            if not text:
                continue
            source = item.get("source") or "voice"
            lang = item.get("lang") or ""
            command = self._parse_operation_command_text(text)
            if (not self._is_voice_language_match(text, lang, active_language)) and (not command):
                logger.debug(
                    f"语音文本语言不匹配已丢弃: expected={active_language}, got_lang={lang}, text={text}"
                )
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="ignored",
                    note=f"lang_blocked|{provider_note}",
                    command=None,
                )
                continue
            has_wake_word = self._pass_voice_wake_word(text)
            self._append_voice_input_log(
                source=source,
                text=text,
                lang=lang,
                status="captured",
                note=(f"{'wake' if has_wake_word else 'no_wake'}|{provider_note}"),
                command=command,
            )
            if settings.VOICE_STRICT_WAKE_WORD and not has_wake_word:
                logger.debug(f"语音文本未通过唤醒词(严格模式): {text}")
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="ignored",
                    note=f"strict_no_wake|{provider_note}",
                    command=command,
                )
                continue
            if (not has_wake_word) and (not command):
                logger.debug(f"语音文本未命中口令: {text}")
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="ignored",
                    note=f"no_command|{provider_note}",
                    command=command,
                )
                continue
            if self._is_duplicate_voice_command(text):
                logger.debug(f"语音文本被去重忽略: {text}")
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="ignored",
                    note=f"duplicate_text|{provider_note}",
                    command=command,
                )
                continue
            if command and self._is_duplicate_voice_action(command):
                logger.debug(f"语音动作被冷却忽略: {command}")
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="ignored",
                    note=f"duplicate_action|{provider_note}",
                    command=command,
                )
                continue

            logger.info(f"收到口令候选[{source}]: {text}")

            log_entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "user": f"主播语音({source})",
                "text": text,
                "status": "voice_ignored",
                "reply": "",
            }
            self.danmu_log.appendleft(log_entry)

            if command:
                ok = self._execute_operation_command(
                    command,
                    trigger_source=f"voice_{source}",
                    log_entry=log_entry
                )
                receipt_note = str(log_entry.get("action_receipt") or "").strip()
                base_note = "wake" if has_wake_word else "no_wake"
                note = base_note if not receipt_note else f"{base_note}|{receipt_note}"
                self._append_voice_input_log(
                    source=source,
                    text=text,
                    lang=lang,
                    status="action_done" if ok else "action_failed",
                    note=(f"{note}|{provider_note}"),
                    command=command,
                )

    def poll_voice_inputs_when_stopped(self, limit=20):
        """
        主监听停止时的轻量语音轮询（仅识别，不执行动作）。
        用于 ASR 调试时脱离 TikTok 主链路观察识别结果。
        """
        if self.is_running:
            return []
        if not self.voice_command_enabled:
            return []
        if self.cloud_asr_test_enabled:
            return []

        now = time.time()
        if now - self.last_voice_poll_at < settings.VOICE_COMMAND_POLL_INTERVAL_SECONDS:
            return []
        self.last_voice_poll_at = now

        active_language = self._get_active_language()
        fallback_languages = self._get_voice_fallback_languages(active_language)
        started = self.voice.ensure_started(
            language=active_language,
            fallback_languages=fallback_languages,
            silence_restart_seconds=settings.VOICE_COMMAND_SILENCE_RESTART_SECONDS,
        )
        if not started:
            return []

        try:
            n = max(1, int(limit or 20))
        except Exception:
            n = 20

        transcripts = self.voice.collect_command_candidates(include_subtitle=True) or []
        out = []
        for item in transcripts[:n]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            source = str(item.get("source") or "voice")
            lang = str(item.get("lang") or "")
            self._append_voice_input_log(
                source=source,
                text=text,
                lang=lang,
                status="captured",
                note="standalone_asr",
                command=None,
            )
            out.append({"source": source, "text": text, "lang": lang, "ts": item.get("ts")})
        return out

    def _maybe_send_proactive_message(self):
        """在直播间空窗期主动暖场。"""
        if not self.proactive_enabled or not self.is_running:
            return
        if not self.vision.page:
            return

        now = time.time()
        if now - self.last_danmu_time < settings.PROACTIVE_SILENCE_SECONDS:
            return
        if now - self.last_proactive_time < self.next_proactive_interval:
            return
        if not self.operations.can_send_message(log_reason=False):
            logger.info("自动暖场跳过：消息发送门禁未通过")
            self.last_proactive_time = now
            self.next_proactive_interval = random.uniform(
                settings.PROACTIVE_MIN_INTERVAL,
                settings.PROACTIVE_MAX_INTERVAL
            )
            self.danmu_log.appendleft({
                "time": datetime.now().strftime("%H:%M:%S"),
                "user": "系统",
                "text": "[自动暖场]",
                "status": "proactive_guarded",
                "reply": ""
            })
            return

        active_language = self._get_active_language()
        messages = settings.PROACTIVE_MESSAGES_BY_LANGUAGE.get(
            active_language,
            settings.PROACTIVE_MESSAGES_BY_LANGUAGE.get(settings.DEFAULT_REPLY_LANGUAGE, [])
        )
        if not messages:
            return

        message = random.choice(messages)
        message = self._enforce_unified_output_language(
            message,
            language=active_language,
            force_tone=bool(self.tone_template),
        )
        sent = self.operations.send_message(message)
        self.last_proactive_time = now
        self.next_proactive_interval = random.uniform(
            settings.PROACTIVE_MIN_INTERVAL,
            settings.PROACTIVE_MAX_INTERVAL
        )
        if sent:
            self._remember_sent_message(message)

        self.danmu_log.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "user": "系统",
            "text": "[自动暖场]",
            "status": "proactive_sent" if sent else "proactive_failed",
            "reply": message
        })

    def _record_danmu_event(self, user, text, log_entry, llm_candidate, processing_ms):
        try:
            self.analytics.record_danmu_event(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "user": user,
                    "text": text,
                    "status": log_entry.get("status"),
                    "reply": log_entry.get("reply"),
                    "action": log_entry.get("action"),
                    "language": self._get_active_language(),
                    "llm_candidate": bool(llm_candidate),
                    "processing_ms": int(processing_ms),
                }
            )
        except Exception as e:
            logger.warning(f"写入弹幕分析事件失败: {e}")

    def _maybe_generate_reports(self):
        if not settings.ANALYTICS_AUTO_REPORT_ENABLED:
            return
        now = time.time()
        if now - self.last_report_check_at < settings.ANALYTICS_REPORT_CHECK_INTERVAL_SECONDS:
            return
        self.last_report_check_at = now
        try:
            created = self.analytics.maybe_generate_periodic_reports()
            for path in created:
                logger.info(f"自动报表已生成: {path}")
        except Exception as e:
            logger.warning(f"自动报表生成失败: {e}")

    def _mock_shop_file_path(self):
        rel = Path("stress") / "mock_shop" / "mock_tiktok_shop.html"
        candidates = [
            Path(__file__).resolve().parent / rel,
            Path.cwd() / rel,
        ]

        runtime_root = str(os.getenv("LIVE_ASSISTANT_RUNTIME_ROOT", "") or "").strip()
        if runtime_root:
            candidates.append(Path(runtime_root).expanduser() / rel)

        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend(
                [
                    exe_dir / rel,
                    exe_dir / "_internal" / rel,
                ]
            )

        seen = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            if resolved.exists():
                return resolved

        return candidates[0].resolve()

    def _normalize_cloud_test_url(self, url):
        raw = str(url or "").strip()
        if not raw:
            return "https://www.bilibili.com/"
        if raw.startswith(("http://", "https://")):
            return raw
        return f"https://{raw}"

    def get_mock_shop_url(self, view=None):
        mock_file = self._mock_shop_file_path()
        page_view = (view or settings.MOCK_SHOP_DEFAULT_VIEW or "dashboard_live").strip()
        base = mock_file.as_uri()
        return f"{base}?mock_tiktok_shop=1&view={page_view}"

    def _is_current_page_mock_shop(self):
        try:
            page = getattr(self.vision, "page", None)
            if not page:
                return False
            title = (getattr(page, "title", "") or "").lower()
            url = (getattr(page, "url", "") or "").lower()
            return (
                "mock_tiktok_shop" in url
                or "mock_tiktok_shop.html" in url
                or "tiktok shop streamer mock" in title
            )
        except Exception:
            return False

    def _launch_debug_browser_with_url(self, url, user_data_path=None):
        launch_plan = build_chrome_debug_launch_args(
            port=settings.BROWSER_PORT,
            user_data_path=user_data_path or settings.USER_DATA_PATH,
            chrome_executable=settings.CHROME_EXECUTABLE,
            startup_url=url,
        )
        argv_candidates = list(launch_plan.get("argv_candidates") or [])
        if not argv_candidates:
            argv = launch_plan.get("argv") or []
            if argv:
                argv_candidates = [argv]
        if not argv_candidates:
            return False, "launch_args_empty", launch_plan.get("display", "")

        kwargs = {
            "cwd": str(Path(__file__).resolve().parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            # Windows 下 close_fds=True 在部分环境会导致句柄继承/创建失败，改为平台分支。
            "close_fds": os.name != "nt",
        }
        if os.name == "nt":
            flags = 0
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if flags:
                kwargs["creationflags"] = flags
        else:
            # 与调用进程解耦，避免 UI 线程结束影响子进程。
            kwargs["start_new_session"] = True

        last_err = ""
        last_cmd = launch_plan.get("display", "")
        for argv in argv_candidates:
            cmd_argv = list(argv)
            if "--new-window" not in cmd_argv:
                cmd_argv.append("--new-window")
            last_cmd = " ".join(cmd_argv)
            try:
                subprocess.Popen(cmd_argv, **kwargs)
                return True, "", last_cmd
            except Exception as e:
                last_err = str(e)
                continue
        return False, last_err or "launch_failed", last_cmd

    def _listening_pids(self, port):
        pids = set()
        # macOS / Linux
        try:
            proc = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pids.add(int(line))
                except ValueError:
                    pass
        except Exception:
            pass

        # Windows 兜底
        if os.name == "nt" and not pids:
            try:
                ps_cmd = (
                    f"Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
                    "| Select-Object -ExpandProperty OwningProcess"
                )
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
                for line in (proc.stdout or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pids.add(int(line))
                    except ValueError:
                        pass
            except Exception:
                pass

        # Windows 再兜底：netstat 输出在非英文系统会本地化，避免硬编码 LISTENING。
        if os.name == "nt" and not pids:
            try:
                proc = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
                target = f":{int(port)}"
                for raw in (proc.stdout or "").splitlines():
                    parts = raw.split()
                    if len(parts) < 5:
                        continue
                    proto = parts[0]
                    local_addr = parts[1]
                    remote_addr = parts[2] if len(parts) >= 3 else ""
                    state = parts[3] if len(parts) >= 4 else ""
                    pid_s = parts[-1]
                    if not proto.upper().startswith("TCP"):
                        continue
                    if not local_addr.endswith(target):
                        continue
                    state_upper = state.upper()
                    looks_listen = (
                        state_upper in {"LISTENING", "LISTEN"}
                        or ("LISTEN" in state_upper)
                        or str(remote_addr).endswith(":0")
                        or str(remote_addr).endswith(":*")
                        or str(remote_addr) in {"0.0.0.0:0", "[::]:0", "*:*"}
                    )
                    if not looks_listen:
                        continue
                    try:
                        pids.add(int(pid_s))
                    except ValueError:
                        pass
            except Exception:
                pass

        return sorted(pids)

    def _kill_listening_pids(self, port):
        killed = []
        for pid in self._listening_pids(port):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.15)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                killed.append(pid)
            except Exception:
                pass
        return killed

    def _is_devtools_endpoint_ready(self, port=None):
        """检测调试端口是否是可用的 Chrome DevTools。"""
        target_port = int(port or settings.BROWSER_PORT)
        url = f"http://127.0.0.1:{target_port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=0.8) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            payload = json.loads(raw) if raw else {}
            return bool(payload.get("webSocketDebuggerUrl"))
        except Exception:
            return False

    def _ensure_debug_browser_process(self, startup_url=None):
        """
        保证存在可用的 DevTools 端口。
        若 9222 不是可用调试端口，则主动拉起专用 Chrome 调试实例。
        """
        if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
            return True

        # 端口被非 DevTools 进程占用时先清理，避免重复拉起一直失败。
        owners = self._listening_pids(settings.BROWSER_PORT)
        if owners:
            self._kill_listening_pids(settings.BROWSER_PORT)
            time.sleep(0.2)
            if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                return True

        launch_url = startup_url or settings.TIKTOK_LIVE_URL
        ok, err, cmd = self._launch_debug_browser_with_url(launch_url)
        if not ok:
            self.last_start_error = f"launch_debug_browser_failed:{err}"
            self.last_start_detail = cmd
            return False

        for _ in range(8):
            time.sleep(0.6)
            if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                return True
        self.last_start_error = "devtools_not_ready"
        self.last_start_detail = cmd
        return False

    def _ensure_mock_debug_browser(self, mock_url, mock_user_data_path):
        """
        保证 mock 使用独立 profile 且 9222 为可用 DevTools 端口。
        """
        if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
            return True, ""

        # 端口被脏进程占用时先清理
        self._kill_listening_pids(settings.BROWSER_PORT)
        launched, launch_err, launch_cmd = self._launch_debug_browser_with_url(
            mock_url,
            user_data_path=mock_user_data_path,
        )
        if not launched:
            return False, f"mock_launch_failed:{launch_err} | {launch_cmd}"

        for _ in range(10):
            time.sleep(0.45)
            if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                return True, ""
        return False, f"mock_devtools_not_ready | {launch_cmd}"

    def _inject_mock_url_into_debug_browser(self, mock_url):
        """
        将 mock URL 强制注入当前 DevTools 浏览器。
        解决“端口已就绪但当前标签不是 mock 页面”的情况。
        """
        try:
            co = ChromiumOptions().set_local_port(settings.BROWSER_PORT)
            browser = ChromiumPage(co)
            tabs = browser.get_tabs() or []
            target_tab = None

            for tab in tabs:
                title = (getattr(tab, "title", "") or "").lower()
                url = (getattr(tab, "url", "") or "").lower()
                if "mock_tiktok_shop" in url or "tiktok shop streamer mock" in title:
                    self.vision.page = tab
                    return True
                if target_tab is None and ("shop.tiktok.com" in url or "tiktok.com" in url):
                    target_tab = tab

            if target_tab is None:
                target_tab = tabs[0] if tabs else browser

            target_tab.get(mock_url)
            time.sleep(0.25)
            self.vision.page = target_tab
            return self._is_current_page_mock_shop()
        except Exception as e:
            logger.debug(f"注入 mock URL 失败: {e}")
            return False

    def _inject_bilibili_url_into_debug_browser(self, bilibili_url):
        try:
            co = ChromiumOptions().set_local_port(settings.BROWSER_PORT)
            browser = ChromiumPage(co)
            tabs = browser.get_tabs() or []
            target_tab = None
            bilibili_tab = None
            bilibili_media_tab = None
            target_url_lower = str(bilibili_url or "").strip().lower()

            for tab in tabs:
                title = (getattr(tab, "title", "") or "").lower()
                url = (getattr(tab, "url", "") or "").lower()
                if ("bilibili.com" in url) or ("哔哩哔哩" in title) or ("b站" in title):
                    if target_url_lower and target_url_lower in url:
                        bilibili_media_tab = tab
                        continue
                    if bilibili_media_tab is None and any(
                        hint in url for hint in ("live.bilibili.com", "/video/", "/bangumi/play", "/read/cv")
                    ):
                        bilibili_media_tab = tab
                    if bilibili_tab is None:
                        bilibili_tab = tab
                if target_tab is None and ("tiktok.com" in url or "shop.tiktok.com" in url):
                    target_tab = tab

            target_tab = bilibili_media_tab or bilibili_tab or target_tab or (tabs[0] if tabs else browser)

            current_url = str(getattr(target_tab, "url", "") or "").lower()
            keep_existing_media_page = bool(
                target_url_lower in {"", "https://www.bilibili.com", "https://www.bilibili.com/"}
                and any(hint in current_url for hint in ("live.bilibili.com", "/video/", "/bangumi/play"))
            )
            should_navigate = bool(bilibili_url) and (not keep_existing_media_page) and (
                not target_url_lower or target_url_lower not in current_url
            )

            if should_navigate:
                target_tab.get(bilibili_url)
                time.sleep(0.35)
            self.vision.page = target_tab
            current_url = str(getattr(target_tab, "url", "") or "").lower()
            current_title = str(getattr(target_tab, "title", "") or "").lower()
            return ("bilibili.com" in current_url) or ("哔哩哔哩" in current_title) or ("b站" in current_title)
        except Exception as e:
            logger.debug(f"注入 bilibili URL 失败: {e}")
            return False

    def start_cloud_asr_bilibili_test(self, url=None, language=None, provider=None):
        """
        启用“流式 ASR 对比测试”：
        - 打开 bilibili 页面用于播放测试音频
        - 使用浏览器内媒体流抓取（不录屏、不录麦）
        - 将媒体流音频送入当前 ASR Provider（本地/云端均可）
        """
        target_url = self._normalize_cloud_test_url(url)
        active_language = str(language or self._get_active_language() or settings.DEFAULT_REPLY_LANGUAGE)
        test_languages = self._build_cloud_test_languages(active_language)
        fallback_languages = test_languages[1:]
        selected_provider = str(provider or "").strip().lower()

        if not self._cloud_asr_test_prev_provider:
            self._cloud_asr_test_prev_provider = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local")
        if not self._cloud_asr_test_prev_input_mode:
            self._cloud_asr_test_prev_input_mode = str(getattr(self.voice, "input_mode", "python_asr") or "python_asr")
        if self._cloud_asr_test_prev_mic_index is None:
            try:
                mic_cfg = self.voice.get_preferred_microphone() if hasattr(self.voice, "get_preferred_microphone") else {}
                self._cloud_asr_test_prev_mic_index = (mic_cfg or {}).get("deviceIndex")
                self._cloud_asr_test_prev_mic_hint = str((mic_cfg or {}).get("nameHint") or "")
            except Exception:
                self._cloud_asr_test_prev_mic_index = None
                self._cloud_asr_test_prev_mic_hint = ""

        self.cloud_asr_test_enabled = True
        self.cloud_asr_test_url = target_url
        self.cloud_asr_test_log.clear()

        if selected_provider:
            switched = self.set_voice_asr_provider(selected_provider)
            if not switched:
                self.cloud_asr_test_enabled = False
                return {"ok": False, "error": "set_asr_provider_failed", "provider": selected_provider, "url": target_url}

        bilibili_user_data = str((Path(settings.USER_DATA_PATH).expanduser().resolve() / "bilibili_cloud_asr_test"))
        ready, ready_err = self._ensure_mock_debug_browser(target_url, bilibili_user_data)
        if not ready:
            self.cloud_asr_test_enabled = False
            return {"ok": False, "error": ready_err or "debug_browser_not_ready", "url": target_url}

        opened = self._inject_bilibili_url_into_debug_browser(target_url)
        if not opened:
            self.cloud_asr_test_enabled = False
            return {"ok": False, "error": "bilibili_page_open_failed", "url": target_url}

        try:
            self.voice.stop()
        except Exception:
            pass
        try:
            self.voice.input_mode = "tab_audio_asr"
            if hasattr(self.voice, "_sync_capture_mode"):
                self.voice._sync_capture_mode()
        except Exception:
            pass

        if hasattr(self.voice, "_start_tab_audio_stream_capture"):
            started = bool(self.voice._start_tab_audio_stream_capture(list(test_languages or [active_language])))
        else:
            started = False
        if not started:
            info = self.voice.get_start_failure_info() if hasattr(self.voice, "get_start_failure_info") else {}
            reason = str((info or {}).get("reason") or "")
            retryable = [
                "tab_audio_js_no_result",
                "browser_page_context_unavailable",
                "media_element_not_found",
                "capture_stream_unavailable",
            ]
            if any(token in reason for token in retryable):
                logger.info(f"B站播放器流首启失败，执行一次定向重试: reason={reason}")
                try:
                    self._inject_bilibili_url_into_debug_browser(target_url)
                except Exception:
                    pass
                ensure_browser_page = getattr(self.vision, "ensure_browser_page_connection", None)
                if callable(ensure_browser_page):
                    try:
                        ensure_browser_page(force=True, prefer_media_tab=True)
                    except TypeError:
                        ensure_browser_page(force=True)
                    except Exception:
                        pass
                time.sleep(0.25)
                if hasattr(self.voice, "_start_tab_audio_stream_capture"):
                    started = bool(self.voice._start_tab_audio_stream_capture(list(test_languages or [active_language])))
                info = self.voice.get_start_failure_info() if hasattr(self.voice, "get_start_failure_info") else {}
            if not started:
                self.cloud_asr_test_enabled = False
                return {
                    "ok": False,
                    "error": str((info or {}).get("reason") or "voice_start_failed"),
                    "detail": (info or {}).get("diag"),
                    "url": target_url,
                }

        self.last_start_error = ""
        self.last_start_detail = f"cloud_asr_test_ready:{target_url}"
        logger.info(
            f"播放器流 ASR 对比测试已启动: url={target_url}, lang={active_language}, "
            f"test_langs={test_languages}, provider={getattr(settings, 'VOICE_PYTHON_ASR_PROVIDER', '')}"
        )
        return {
            "ok": True,
            "url": target_url,
            "provider": str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "")),
            "mode": "tab_media_stream",
            "language": active_language,
            "fallback_languages": fallback_languages,
            "test_languages": test_languages,
        }

    def stop_cloud_asr_bilibili_test(self, restore_previous=True):
        self.cloud_asr_test_enabled = False
        try:
            if hasattr(self.voice, "_stop_tab_audio_stream_capture"):
                self.voice._stop_tab_audio_stream_capture()
            else:
                self.voice.stop()
        except Exception:
            pass

        restored_provider = None
        restored_mode = None
        if restore_previous:
            prev_provider = str(self._cloud_asr_test_prev_provider or "").strip().lower()
            prev_mode = str(self._cloud_asr_test_prev_input_mode or "").strip().lower()
            if prev_provider:
                self.set_voice_asr_provider(prev_provider)
                restored_provider = prev_provider
            if prev_mode:
                try:
                    self.voice.input_mode = prev_mode
                    if hasattr(self.voice, "_sync_capture_mode"):
                        self.voice._sync_capture_mode()
                    restored_mode = prev_mode
                except Exception:
                    pass

        self._cloud_asr_test_prev_provider = ""
        self._cloud_asr_test_prev_input_mode = ""
        self._cloud_asr_test_prev_mic_index = None
        self._cloud_asr_test_prev_mic_hint = ""
        return {
            "ok": True,
            "restored_provider": restored_provider,
            "restored_mode": restored_mode,
        }

    def poll_cloud_asr_test_transcripts(self, limit=40):
        if not self.cloud_asr_test_enabled:
            return []
        state = self.voice.get_tab_audio_stream_state() if hasattr(self.voice, "get_tab_audio_stream_state") else {}
        runtime_provider = str((state or {}).get("provider") or getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "unknown") or "unknown")
        runtime_type = str((state or {}).get("providerType") or "unknown")
        if hasattr(self.voice, "poll_tab_audio_stream_transcripts"):
            try:
                items = self.voice.poll_tab_audio_stream_transcripts() or []
            except Exception:
                items = []
        else:
            items = []

        for item in items:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            self.cloud_asr_test_log.appendleft(
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "text": text,
                    "lang": str(item.get("lang") or ""),
                    "source": str(item.get("source") or "python_loopback"),
                    "provider": runtime_provider,
                    "provider_type": runtime_type,
                }
            )

        try:
            n = max(1, int(limit or 40))
        except Exception:
            n = 40
        return list(self.cloud_asr_test_log)[:n]

    def get_cloud_asr_test_status(self):
        if hasattr(self.voice, "get_tab_audio_stream_state"):
            state = self.voice.get_tab_audio_stream_state() or {}
        else:
            state = self.voice.get_state() if hasattr(self.voice, "get_state") else {}
        return {
            "enabled": bool(self.cloud_asr_test_enabled),
            "url": self.cloud_asr_test_url,
            "running": bool((state or {}).get("running", False)),
            "error": str((state or {}).get("error") or ""),
            "capture_mode": "tab_media_stream",
            "device_name": "browser_media_stream",
            "provider": str((state or {}).get("provider") or "dashscope_funasr"),
            "provider_type": str((state or {}).get("providerType") or "cloud"),
            "provider_error": str((state or {}).get("providerError") or ""),
            "last_text": str((state or {}).get("lastText") or ""),
        }

    def connect_mock_shop(self, view=None):
        """
        显式连接项目内置 Mock 测试页：
        1) 拉起带调试端口的 Chrome 并打开 mock 页面
        2) 重试连接 VisionAgent，必要时强制导航到 mock URL
        """
        mock_file = self._mock_shop_file_path()
        if not mock_file.exists():
            self.last_start_error = "mock_file_missing"
            self.last_start_detail = str(mock_file)
            logger.error(f"Mock 页面不存在: {mock_file}")
            return False

        mock_url = self.get_mock_shop_url(view=view)
        mock_user_data = str((Path(settings.USER_DATA_PATH).expanduser().resolve() / "mock_debug"))

        # 快路径：当前已在 mock 页，直接成功。
        if self._is_current_page_mock_shop():
            self.last_start_error = ""
            self.last_start_detail = f"mock_connected:{mock_url}"
            return True

        # 快路径：DevTools 已就绪时，先尝试注入，不做固定等待。
        if self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
            injected_fast = self._inject_mock_url_into_debug_browser(mock_url)
            if injected_fast and self._is_current_page_mock_shop():
                self.last_start_error = ""
                self.last_start_detail = f"mock_connected:{mock_url}"
                logger.info(f"已快速连接内置 Mock 测试页: {mock_url}")
                return True

        ready, ready_err = self._ensure_mock_debug_browser(mock_url, mock_user_data)
        if not ready:
            self.last_start_error = ready_err
            self.last_start_detail = mock_url
            logger.warning(f"启动 Mock 浏览器失败: {ready_err}")
            return False

        retries = max(1, int(settings.MOCK_SHOP_CONNECT_RETRIES))
        interval = max(0.2, float(settings.MOCK_SHOP_CONNECT_RETRY_INTERVAL_SECONDS))
        last_err = ""

        for attempt in range(1, retries + 1):
            # 首轮立即尝试，失败后再按间隔重试，减少体感等待。
            if attempt > 1:
                time.sleep(interval)
            if not self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                ready, ready_err = self._ensure_mock_debug_browser(mock_url, mock_user_data)
                if not ready:
                    last_err = ready_err
                    continue
            injected = self._inject_mock_url_into_debug_browser(mock_url)
            if injected and self._is_current_page_mock_shop():
                self.last_start_error = ""
                self.last_start_detail = f"mock_connected:{mock_url}"
                logger.info(f"已注入并连接内置 Mock 测试页: {mock_url}")
                return True
            if not injected:
                last_err = "inject_mock_url_failed"
                # 注入失败时尝试重建调试浏览器，避免卡在脏会话。
                self._kill_listening_pids(settings.BROWSER_PORT)
                self._ensure_mock_debug_browser(mock_url, mock_user_data)
                continue
            try:
                self.vision.connect_browser()
                if self._is_current_page_mock_shop():
                    self.last_start_error = ""
                    self.last_start_detail = f"mock_connected:{mock_url}"
                    logger.info(f"已连接内置 Mock 测试页: {mock_url}")
                    return True

                # 若连到非目标页，强制导航当前标签到 Mock。
                page = getattr(self.vision, "page", None)
                if page:
                    try:
                        page.get(mock_url)
                        time.sleep(0.35)
                        if self._is_current_page_mock_shop():
                            self.last_start_error = ""
                            self.last_start_detail = f"mock_connected:{mock_url}"
                            logger.info(f"已切换到内置 Mock 测试页: {mock_url}")
                            return True
                    except Exception as nav_e:
                        last_err = str(nav_e)
                        logger.debug(f"导航到 Mock 页面失败(尝试{attempt}/{retries}): {nav_e}")
            except Exception as e:
                last_err = str(e)
                emsg = (last_err or "").lower()
                if "browser connection fails" in emsg or "address: 127.0.0.1:9222" in emsg:
                    self._kill_listening_pids(settings.BROWSER_PORT)
                    self._ensure_mock_debug_browser(mock_url, mock_user_data)

        self.last_start_error = f"mock_connect_failed:{last_err or 'unknown'}"
        self.last_start_detail = mock_url
        logger.warning(f"连接内置 Mock 测试页失败: {self.last_start_error}")
        return False

    def connect_browser(self):
        """连接到浏览器"""
        try:
            if self.get_web_info_source_mode() == "screen_ocr":
                ok = bool(self.vision.ensure_connection(force=True))
                if ok:
                    self.last_start_error = ""
                    self.last_start_detail = "screen_capture_ready"
                else:
                    self.last_start_error = "screen_capture_unavailable"
                    self.last_start_detail = getattr(getattr(self.vision, "screen_capture", None), "last_error", "") or ""
                return ok
            # 先确保调试端口可用，避免“端口被占但不是 DevTools”的伪连接失败。
            if not self._is_devtools_endpoint_ready(settings.BROWSER_PORT):
                ready = self._ensure_debug_browser_process(startup_url=settings.TIKTOK_LIVE_URL)
                if not ready:
                    return False
            self.vision.connect_browser()
            self.last_start_error = ""
            self.last_start_detail = "browser_connected"
            return True
        except Exception as e:
            launch_info = build_chrome_debug_commands(
                port=settings.BROWSER_PORT,
                user_data_path=settings.USER_DATA_PATH,
                chrome_executable=settings.CHROME_EXECUTABLE,
            )
            self.last_start_error = str(e)
            self.last_start_detail = launch_info["primary"]
            logger.warning(f"无法连接到浏览器，请确保已启动 Chrome 并开启调试端口 {settings.BROWSER_PORT}")
            logger.warning(f"当前系统: {launch_info['platform_label']}")
            logger.warning(f"命令示例: {launch_info['primary']}")
            if launch_info["alternatives"]:
                logger.warning(f"备选命令: {launch_info['alternatives'][0]}")
            return False

    def _ensure_browser_connected(self):
        """启动阶段浏览器连接保障：先探活，再重试连接。"""
        if self.get_web_info_source_mode() == "screen_ocr":
            ok = bool(self.vision.ensure_connection(force=True))
            if not ok:
                self.last_start_error = "screen_capture_unavailable"
                self.last_start_detail = getattr(getattr(self.vision, "screen_capture", None), "last_error", "") or ""
            return ok

        if self.vision.ensure_connection():
            return True

        retries = max(1, settings.STARTUP_CONNECT_RETRIES)
        interval = max(0.0, settings.STARTUP_CONNECT_RETRY_INTERVAL_SECONDS)
        for attempt in range(1, retries + 1):
            ok = self.connect_browser()
            if ok:
                return True
            if "未找到 TikTok 直播标签页" in (self.last_start_error or ""):
                logger.warning("启动阶段未检测到直播标签页，停止快速重试，等待用户打开直播页")
                break
            if attempt < retries:
                time.sleep(interval)
        return False

    def _ensure_browser_page_context_connected(self, prefer_media_tab=False):
        """
        仅确保可执行 JS 的浏览器上下文可用（不要求 TikTok 直播页）。
        用于 ASR 对比测试等“非直播监听”场景。
        """
        ensure_browser_page = getattr(self.vision, "ensure_browser_page_connection", None)
        if not callable(ensure_browser_page):
            self.last_start_error = "browser_page_context_api_missing"
            self.last_start_detail = "vision.ensure_browser_page_connection"
            return False
        try:
            try:
                ok = bool(ensure_browser_page(force=True, prefer_media_tab=bool(prefer_media_tab)))
            except TypeError:
                ok = bool(ensure_browser_page(force=True))
        except Exception as e:
            self.last_start_error = f"browser_page_context_unavailable:{e}"
            self.last_start_detail = ""
            return False
        if ok:
            self.last_start_error = ""
            self.last_start_detail = "browser_page_context_ready"
            return True
        self.last_start_error = "browser_page_context_unavailable"
        self.last_start_detail = ""
        return False

    def handle_message(self, msg, allow_llm=True):
        """处理单条弹幕消息"""
        started_at = time.time()
        user = msg.get('user', '未知用户')
        text = msg.get('text', '')
        self.last_danmu_time = time.time()
        
        # 记录到内存日志
        log_entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "user": user,
            "text": text,
            "status": "pending",
            "reply": ""
        }
        self.danmu_log.appendleft(log_entry)  # 最新的在最前
        
        logger.info(f"收到弹幕 [{user}]: {text}")
        llm_candidate = False
        try:
            if self._is_self_echo_message(user, text):
                log_entry["status"] = "self_echo_ignored"
                return

            # 0. 先解析主播运营动作口令（优先级最高）
            command = None
            if self.get_web_info_source_mode() == "screen_ocr":
                # screen_ocr 下弹幕源可能混入页面 UI 文案：按配置决定是否要求唤醒词。
                require_wake = bool(
                    getattr(settings, "SCREEN_OCR_DANMU_REQUIRE_WAKE_WORD", True)
                )
                if (not require_wake) or self._pass_voice_wake_word(text):
                    command = self._parse_operation_command(user, text)
                else:
                    log_entry["action_guard"] = "screen_ocr_danmu_no_wake"
            else:
                command = self._parse_operation_command(user, text)
            if command:
                if self._is_duplicate_danmu_action(command):
                    log_entry["status"] = "duplicate_action_skipped"
                    log_entry["action"] = self._voice_action_key(command) or str(
                        command.get("action") or ""
                    )
                    log_entry["action_guard"] = "danmu_action_cooldown"
                    return
                self._execute_operation_command(command, trigger_source="danmu", log_entry=log_entry)
                return

            if self._is_duplicate_message(user, text):
                log_entry["status"] = "duplicate_message_skipped"
                return

            if not self.reply_enabled:
                log_entry["status"] = "reply_disabled"
                return

            active_language = self._get_active_language()
            
            # 1. 场控智能体优先分析 (关键词匹配速度快)
            reply = self.atmosphere.analyze_and_reply(text, language=active_language)
            reply_source = "atmosphere" if reply else ""
            
            llm_candidate = self._is_llm_candidate(text)
            llm_skipped_for_backlog = False

            # 2. 如果场控没有匹配，且是问题类弹幕，再尝试知识智能体
            if not reply and llm_candidate and allow_llm:
                now = time.time()
                if now - self.last_llm_query_at >= settings.LLM_MIN_INTERVAL_SECONDS:
                    self.last_llm_query_at = now
                    reply = self.knowledge.query(
                        text,
                        language=active_language,
                        tone_template=self.tone_template
                    )
                    reply_source = "knowledge" if reply else reply_source
                    if reply:
                        logger.info(f"知识库检索回复: {reply}")
                else:
                    logger.debug("跳过本次 LLM 调用：触发间隔保护")
            elif not reply and llm_candidate and not allow_llm:
                llm_skipped_for_backlog = True
                logger.debug("跳过本条 LLM 调用：高峰期仅处理最新问题")
            
            # 更新内存日志状态
            if reply:
                reply = self._enforce_unified_output_language(
                    reply,
                    language=active_language,
                    force_tone=bool(self.tone_template and reply_source != "knowledge"),
                )
                if len(reply) > settings.LLM_REPLY_MAX_CHARS:
                    reply = reply[:settings.LLM_REPLY_MAX_CHARS]

                if self._is_duplicate_reply(user, reply):
                    log_entry["status"] = "duplicate_reply_skipped"
                    log_entry["reply"] = reply
                    return

                log_entry["status"] = "replied"
                log_entry["reply"] = reply
                
                # 3. 发送回复
                full_reply = f"@{user} {reply}"
                send_ok = self.operations.send_message(full_reply)
                if not send_ok:
                    log_entry["status"] = "send_failed"
                else:
                    self._remember_sent_message(full_reply)
            else:
                if llm_skipped_for_backlog:
                    log_entry["status"] = "llm_skipped_backlog"
                else:
                    log_entry["status"] = "ignored"
        finally:
            processing_ms = int((time.time() - started_at) * 1000)
            self._record_danmu_event(
                user=user,
                text=text,
                log_entry=log_entry,
                llm_candidate=llm_candidate,
                processing_ms=processing_ms,
            )

    def _run_loop(self):
        """后台运行循环"""
        logger.info("AI 助手后台监听循环已启动")
        # 使用手动轮询，便于优雅退出
        while not self._stop_event.is_set():
            try:
                self._poll_voice_commands()
                # B 站播放器流 ASR 对比测试时，不拉取弹幕，避免触发“非 TikTok 页”重连风暴。
                if self.cloud_asr_test_enabled:
                    messages = []
                else:
                    messages = self.vision.get_new_danmu()
                if len(messages) > settings.MAX_MESSAGES_PER_CYCLE:
                    logger.debug(
                        f"弹幕高峰: 本轮抓到 {len(messages)} 条，仅处理最新 {settings.MAX_MESSAGES_PER_CYCLE} 条"
                    )
                    messages = messages[-settings.MAX_MESSAGES_PER_CYCLE:]
                if len(messages) > 1:
                    # 高峰期优先处理最新弹幕，降低体感延迟
                    messages = list(reversed(messages))
                for idx, msg in enumerate(messages):
                    # get_new_danmu 已去重，这里直接处理即可
                    self.handle_message(msg, allow_llm=(idx == 0))

                self._maybe_send_proactive_message()
                self._maybe_generate_reports()

                sleep_seconds = (
                    settings.MAIN_LOOP_BUSY_INTERVAL_SECONDS
                    if messages else settings.MAIN_LOOP_IDLE_INTERVAL_SECONDS
                )
                self._stop_event.wait(max(0.01, sleep_seconds))
            except Exception as e:
                logger.error(f"监听循环出错: {e}")
                self._stop_event.wait(max(0.05, settings.MAIN_LOOP_ERROR_BACKOFF_SECONDS))
        
        logger.info("AI 助手后台监听循环已停止")

    def _prewarm_local_runtime(self):
        """
        轻量本地预热，降低首轮语音/知识链路冷启动耗时。
        """
        try:
            if hasattr(self.voice, "prewarm_local_asr"):
                self.voice.prewarm_local_asr()
        except Exception as e:
            logger.debug(f"语音本地预热失败: {e}")
        try:
            # 触发一次本地检索路径，预热向量检索与分词流程。
            if hasattr(self.knowledge, "_retrieve_context"):
                self.knowledge._retrieve_context("warmup")
        except Exception as e:
            logger.debug(f"知识检索预热失败: {e}")

    def start(self):
        """启动助手"""
        if self.is_running and self._thread and self._thread.is_alive():
            logger.warning("助手已经在运行中")
            return True
        if self.is_starting:
            logger.warning("助手正在启动中，请稍后")
            return False

        with self._start_lock:
            if self.is_running and self._thread and self._thread.is_alive():
                return True

            self.is_starting = True
            self.last_start_error = ""
            self.last_start_detail = ""
            self.last_start_at = time.time()
            try:
                if self.cloud_asr_test_enabled:
                    # ASR 测试模式只需要浏览器可执行 JS 上下文，不强制 TikTok 直播页。
                    if not self._ensure_browser_page_context_connected(prefer_media_tab=True):
                        self.last_start_error = self.last_start_error or "browser_page_context_unavailable"
                        logger.error("ASR 测试模式浏览器上下文不可用，未启动监听")
                        return False
                else:
                    if not self._ensure_browser_connected():
                        self.last_start_error = self.last_start_error or "browser_not_connected"
                        logger.error("浏览器连接失败，未启动监听")
                        return False

                self._stop_event.clear()
                self.is_running = True
                self._thread = threading.Thread(target=self._run_loop, daemon=True)
                self._thread.start()

                wait_s = max(0.0, float(settings.STARTUP_THREAD_READY_TIMEOUT_SECONDS))
                # 启动确认采用短轮询，避免因配置过大导致 UI 长时间停留在“未运行”。
                wait_cap = 2.0
                if wait_s > wait_cap:
                    logger.warning(
                        f"STARTUP_THREAD_READY_TIMEOUT_SECONDS={wait_s} 过大，已按 {wait_cap}s 上限执行快速确认"
                    )
                wait_deadline = time.time() + min(wait_s, wait_cap)
                while time.time() < wait_deadline:
                    if self._thread and self._thread.is_alive():
                        break
                    time.sleep(0.03)
                if not self._thread.is_alive():
                    raise RuntimeError("listener_thread_not_alive")

                # 语音通道改为循环内懒启动，避免阻塞系统启动。
                if self.voice_command_enabled:
                    logger.info("语音监听将在后台循环中自动就绪")

                # 异步预热本地链路，不阻塞主监听启动。
                threading.Thread(target=self._prewarm_local_runtime, daemon=True).start()

                self.last_start_detail = "ok"
                logger.info("系统已启动")
                return True
            except Exception as e:
                self.is_running = False
                self.last_start_error = str(e)
                logger.error(f"系统启动失败: {e}")
                return False
            finally:
                self.is_starting = False

    def stop(self):
        """停止助手"""
        if not self.is_running:
            return
            
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.voice.stop()
        except Exception:
            pass
        self.is_running = False
        self.is_starting = False
        logger.info("系统已停止")

# 为了兼容旧的 main.py 运行方式
def main():
    logger.info("启动 AI 智能直播助手 (Pro版)...")
    assistant = LiveAssistant()
    
    # 连接浏览器
    assistant.connect_browser()
    
    # 定义回调适配器 (因为 vision.listen 需要回调)
    # 但我们的 LiveAssistant 已经重构了循环逻辑。
    # 如果要复用 VisionAgent.listen 的死循环逻辑 (如果不修改 VisionAgent):
    # assistant.vision.listen(assistant.handle_message)
    
    # 既然我们重构了 LiveAssistant 使用线程，这里直接调用 start() 并主线程等待
    if not assistant.start():
        logger.error("启动失败，请检查浏览器连接和标签页。")
        return
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        assistant.stop()

if __name__ == "__main__":
    main()
