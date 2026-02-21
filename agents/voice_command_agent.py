import threading
import time
import re
import math
import struct
import io
import wave
import audioop
import base64
import tempfile
from http import HTTPStatus
from pathlib import Path
from collections import deque
import numpy as np
from utils.logger import logger
import app_config.settings as settings


class VoiceCommandAgent:
    """
    语音口令监听器（多通道）：
    1) 浏览器 Web Speech API 麦克风识别
    2) 页面字幕文本兜底提取
    """

    def __init__(self, vision_agent):
        self.vision_agent = vision_agent
        self.input_mode = str(getattr(settings, "VOICE_COMMAND_INPUT_MODE", "web_speech") or "web_speech").strip().lower()
        self._local_capture_mode = self._resolve_capture_mode()
        self.last_lang = None
        self.last_langs = []
        self.enabled_in_page = False
        self.active_ctx_name = None
        self._subtitle_seen = {}
        self.last_start_reason = None
        self.last_start_errors = []
        self.last_start_diag = {}
        self.last_start_attempt_at = 0.0
        self.last_start_success_at = 0.0
        self.permission_blocked = False
        self._local_queue = deque(maxlen=max(20, int(getattr(settings, "VOICE_PYTHON_ASR_QUEUE_MAX", 200))))
        self._local_lock = threading.Lock()
        self._local_stop_event = threading.Event()
        self._local_thread = None
        self._local_running = False
        self._local_error = None
        self._local_last_result_at = None
        self._local_last_text = ""
        self._local_last_text_lang = None
        self._local_last_start_at = None
        self._local_last_audio_at = None
        self._local_last_audio_rms = 0
        self._local_no_text_count = 0
        self._provider_chain_logged = ""
        self._last_provider_base = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local")
        self._last_provider_chain = []
        self._last_provider_attempt = None
        self._last_provider_selected = None
        self._last_provider_error = None
        self._last_provider_at = None
        self._loopback_fallback_warned = False
        if self._local_capture_mode == "loopback":
            self._preferred_mic_device_index = int(getattr(settings, "VOICE_LOOPBACK_DEVICE_INDEX", -1))
            self._preferred_mic_name_hint = str(getattr(settings, "VOICE_LOOPBACK_DEVICE_NAME_HINT", "") or "").strip()
        else:
            self._preferred_mic_device_index = int(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_INDEX", -1))
            self._preferred_mic_name_hint = str(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_NAME_HINT", "") or "").strip()
        self._local_mic_state = {
            "status": "idle",
            "error": None,
            "updatedAt": None,
            "provider": str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local"),
            "captureMode": self._local_capture_mode,
        }
        self._google_backoff_until = 0.0
        self._google_error_count = 0
        self._dashscope_backoff_until = 0.0
        self._dashscope_error_count = 0
        self._whisper_model_name = None
        self._whisper_model = None
        self._whisper_prewarm_started = False
        self._tab_audio_running = False
        self._tab_audio_error = None
        self._tab_audio_last_result_at = None
        self._tab_audio_last_text = ""
        self._tab_audio_last_text_lang = None
        self._tab_audio_last_audio_at = None
        self._tab_audio_last_audio_rms = 0
        self._tab_audio_no_text_count = 0
        self._tab_audio_langs = []
        self._tab_audio_buffer = bytearray()
        self._tab_audio_sample_rate = 16000
        self._tab_audio_chunk_seconds = max(
            2.0,
            float(getattr(settings, "VOICE_TAB_AUDIO_CHUNK_SECONDS", 4.8) or 4.8),
        )
        self._tab_audio_max_chunk_seconds = max(
            self._tab_audio_chunk_seconds,
            float(getattr(settings, "VOICE_TAB_AUDIO_MAX_CHUNK_SECONDS", 9.0) or 9.0),
        )
        self._tab_audio_overlap_seconds = min(
            1.5,
            max(0.0, float(getattr(settings, "VOICE_TAB_AUDIO_CHUNK_OVERLAP_SECONDS", 0.6) or 0.6)),
        )
        self._tab_audio_emit_idle_ms = int(
            max(600.0, float(getattr(settings, "VOICE_TAB_AUDIO_EMIT_IDLE_SECONDS", 1.2) or 1.2) * 1000.0)
        )
        self._tab_audio_emit_max_wait_ms = int(
            max(2500.0, float(getattr(settings, "VOICE_TAB_AUDIO_EMIT_MAX_WAIT_SECONDS", 9.0) or 9.0) * 1000.0)
        )
        self._tab_audio_emit_max_chars = max(
            24,
            int(getattr(settings, "VOICE_TAB_AUDIO_EMIT_MAX_CHARS", 96) or 96),
        )
        self._tab_audio_silence_rms = max(
            20,
            int(getattr(settings, "VOICE_TAB_AUDIO_SILENCE_RMS", 110) or 110),
        )
        self._tab_audio_pending_text = ""
        self._tab_audio_pending_lang = None
        self._tab_audio_pending_started_at = None
        self._tab_audio_pending_updated_at = None
        self._tab_audio_last_chunk_at = None
        self._tab_audio_last_restart_at = 0.0
        self._tab_audio_stall_restart_seconds = max(
            2.0,
            float(getattr(settings, "VOICE_TAB_AUDIO_STALL_RESTART_SECONDS", 4.5) or 4.5),
        )
        self._tab_audio_restart_cooldown_seconds = max(
            0.8,
            float(getattr(settings, "VOICE_TAB_AUDIO_RESTART_COOLDOWN_SECONDS", 2.0) or 2.0),
        )
        self._tab_audio_recognizer = None

    def get_mode(self):
        return self.input_mode

    def _normalize_provider_name(self, provider=None):
        raw = str(
            provider
            if provider is not None
            else getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local")
            or "whisper_local"
        ).strip().lower()
        aliases = {
            "dashscope": "dashscope_funasr",
            "aliyun_funasr": "dashscope_funasr",
            "funasr": "dashscope_funasr",
            "hybrid": "hybrid_local_cloud",
            "local_cloud": "hybrid_local_cloud",
            "cloud_local": "hybrid_local_cloud",
        }
        return aliases.get(raw, raw)

    def _is_dashscope_force_loopback(self, provider=None):
        if not bool(getattr(settings, "VOICE_DASHSCOPE_FORCE_LOOPBACK", True)):
            return False
        return self._normalize_provider_name(provider) == "dashscope_funasr"

    def _is_tab_media_asr_mode(self):
        return self.input_mode in {
            "system_audio_asr",
            "tab_audio_asr",
            "tab_media_asr",
        }

    def _resolve_capture_mode(self, provider=None):
        if self._is_tab_media_asr_mode():
            return "tab_media_stream"
        if self._is_dashscope_force_loopback(provider):
            return "loopback"
        if self.input_mode in {
            "system_loopback_asr",
            "loopback_asr",
            "loopback",
        }:
            return "loopback"
        return "mic"

    def _sync_capture_mode(self, provider=None):
        mode = self._resolve_capture_mode(provider)
        self._local_capture_mode = mode
        self._local_mic_state["captureMode"] = mode
        return mode

    def _is_python_asr_mode(self):
        return self.input_mode in {
            "python_asr",
            "local_python_asr",
            "python_local",
            "local",
            "system_loopback_asr",
            "loopback_asr",
            "system_audio_asr",
            "tab_audio_asr",
            "tab_media_asr",
            "loopback",
        }

    def _is_loopback_asr_mode(self):
        return self._resolve_capture_mode() == "loopback"

    def requires_browser_page(self):
        return not self._is_python_asr_mode()

    def _set_local_mic_state(self, status, error=None):
        self._local_mic_state["status"] = status
        self._local_mic_state["error"] = error
        self._local_mic_state["updatedAt"] = int(time.time() * 1000)

    def _provider_runtime_kind(self, provider_name):
        name = str(provider_name or "").strip().lower()
        if name in {"whisper_local", "sphinx"}:
            return "local"
        if name in {"google", "dashscope_funasr"}:
            return "cloud"
        return "unknown"

    def _should_report_no_text_error(self):
        """仅在输入音量达到阈值时上报 no_text，避免安静环境误报。"""
        runtime_provider = (
            self._last_provider_selected
            or self._last_provider_attempt
            or self._normalize_provider_name()
        )
        # 云端 ASR 通道出现“持续无文本”时，需要明确反馈，避免看起来像“无响应”。
        if self._provider_runtime_kind(runtime_provider) == "cloud":
            return True
        try:
            rms = int(self._local_last_audio_rms or 0)
        except Exception:
            rms = 0
        threshold = max(0, int(getattr(settings, "VOICE_PYTHON_NO_TEXT_WARN_RMS", 120) or 120))
        return rms >= threshold

    def _is_command_like_text(self, text):
        raw = str(text or "").strip()
        if not raw:
            return False
        lower = raw.lower()
        command_markers = [
            "置顶", "取消置顶", "秒杀", "上架", "链接", "商品", "橱窗",
            "pin", "unpin", "top", "link", "item", "product", "number",
            "flash", "sale", "promotion", "deal", "start", "launch",
            "assistant", "cohost", "streamassistant", "liveassistant",
        ]
        has_marker = any(m in lower for m in command_markers)
        has_index = bool(re.search(r"(?:\b\d+\b|[零一二两三四五六七八九十])", raw))
        return bool(has_marker and (has_index or len(raw) <= 80))

    def _is_noise_short_utterance(self, text):
        raw = str(text or "").strip()
        if not raw:
            return True
        if self._is_command_like_text(raw):
            return False
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", raw.lower()).strip()
        tokens = [t for t in compact.split() if t]
        if not tokens:
            return True
        # 过滤英文单词噪声（如 "you"/"uh"），避免长段语音退化成单词刷屏。
        if len(tokens) == 1 and len(tokens[0]) <= 3 and not re.search(r"[\u4e00-\u9fff]", raw):
            return True
        return False

    def _is_low_quality_transcript(self, text, lang=None):
        """
        识别并过滤 ASR 幻听/灌水文本（如重复短语长串），避免污染口令链路。
        返回: (is_bad, reason)
        """
        raw = str(text or "").strip()
        if not raw:
            return True, "empty"

        lower = raw.lower()
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lower).strip()
        tokens = [t for t in compact.split() if t]
        token_count = len(tokens)

        # 明显是口令类文本，直接放行。
        has_command_marker = self._is_command_like_text(raw)
        has_index = bool(re.search(r"(?:\b\d+\b|[零一二两三四五六七八九十])", raw))
        if has_command_marker and (has_index or token_count <= 16):
            return False, ""

        # 规则1：重复短语（例如 "a little bit ... " 循环）
        phrase_repeat = re.search(r"\b([a-z]{1,12}(?:\s+[a-z]{1,12}){1,4})\b(?:\s+\1){3,}", lower)
        if phrase_repeat:
            return True, "repeated_phrase"

        # 规则2：长文本但词汇多样性极低
        if token_count >= 14:
            uniq_ratio = len(set(tokens)) / max(1, token_count)
            max_word_ratio = max(tokens.count(t) for t in set(tokens)) / max(1, token_count)
            if uniq_ratio < 0.40 and max_word_ratio > 0.20:
                return True, "low_diversity_repeat"

            # 规则3：3-gram 高频重复，常见于幻听长句
            if token_count >= 18:
                trigrams = [" ".join(tokens[i:i + 3]) for i in range(0, token_count - 2)]
                if trigrams:
                    max_tri = max(trigrams.count(t) for t in set(trigrams))
                    if max_tri >= 4:
                        return True, "repeated_trigram"

        # 规则4：超长非命令文本默认丢弃（保留命令链路优先）
        if len(raw) >= 180 and (not has_command_marker):
            return True, "too_long_non_command"

        return False, ""

    def _language_family(self, lang_code):
        code = str(lang_code or "").strip().lower()
        if code.startswith("zh"):
            return "zh"
        if code.startswith("en"):
            return "en"
        return ""

    def _is_text_lang_compatible(self, text, expected_lang=None, detected_lang=None):
        expected = self._language_family(expected_lang)
        if not expected:
            return True
        detected = self._language_family(detected_lang)
        raw = str(text or "")
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", raw))
        has_latin = bool(re.search(r"[A-Za-z]", raw))

        if detected and detected != expected:
            return False
        if expected == "en":
            return not has_cjk
        if expected == "zh":
            return not has_latin
        return True

    def _is_runtime_lang_compatible(self, text, detected_lang=None):
        expected = (self.last_langs[0] if self.last_langs else self.last_lang)
        return self._is_text_lang_compatible(text, expected_lang=expected, detected_lang=detected_lang)

    def _local_push_text(self, text, lang=None, confidence=None):
        text = str(text or "").strip()
        if not text:
            return
        source = "python_loopback" if self._is_loopback_asr_mode() else "python_mic"
        item = {
            "source": source,
            "text": text,
            "confidence": confidence,
            "lang": lang,
            "ts": int(time.time() * 1000),
        }
        with self._local_lock:
            self._local_queue.append(item)
        self._local_last_result_at = item["ts"]
        self._local_last_text = text
        self._local_last_text_lang = lang

    def _queue_push_text(self, source, text, lang=None, confidence=None):
        text = str(text or "").strip()
        if not text:
            return
        item = {
            "source": str(source or "voice"),
            "text": text,
            "confidence": confidence,
            "lang": lang,
            "ts": int(time.time() * 1000),
        }
        with self._local_lock:
            self._local_queue.append(item)
        self._local_last_result_at = item["ts"]
        self._local_last_text = text
        self._local_last_text_lang = lang

    def _import_speech_recognition(self):
        try:
            import speech_recognition as sr  # type: ignore
            return sr
        except Exception as e:
            self._local_error = f"speech_recognition_unavailable: {e}"
            return None

    def _list_local_microphones(self, sr):
        try:
            names = sr.Microphone.list_microphone_names()
            if not isinstance(names, list):
                return []
            return [str(n or "").strip() for n in names]
        except Exception:
            return []

    def list_input_devices(self):
        sr = self._import_speech_recognition()
        if sr is None:
            return []
        names = self._list_local_microphones(sr)
        return [{"index": idx, "name": name} for idx, name in enumerate(names)]

    def get_preferred_microphone(self):
        return {
            "deviceIndex": self._preferred_mic_device_index,
            "nameHint": self._preferred_mic_name_hint,
        }

    def set_preferred_microphone(self, device_index=None, name_hint=None, restart_if_running=False):
        self._sync_capture_mode()
        if device_index is None:
            self._preferred_mic_device_index = -1
        else:
            try:
                self._preferred_mic_device_index = int(device_index)
            except Exception:
                self._preferred_mic_device_index = -1
        if name_hint is not None:
            self._preferred_mic_name_hint = str(name_hint or "").strip()
        self._local_mic_state["deviceIndex"] = (self._preferred_mic_device_index if self._preferred_mic_device_index >= 0 else None)
        self._local_mic_state["nameHint"] = self._preferred_mic_name_hint
        self._local_mic_state["captureMode"] = self._local_capture_mode

        if restart_if_running and self._local_running and self._is_python_asr_mode():
            langs = list(self.last_langs or ["zh-CN"])
            self.stop()
            self.start(language=langs[0], fallback_languages=langs[1:])
        return self.get_preferred_microphone()

    def probe_local_microphone(self, language="zh-CN", fallback_languages=None, duration_seconds=2.5):
        """
        录制一段本地麦克风音频并返回探测结果（RMS + 识别文本）。
        用于快速定位“监听 running 但无文本”的输入层问题。
        """
        if self._is_tab_media_asr_mode():
            return {"ok": False, "error": "probe_not_supported_for_tab_media_stream"}
        if not self._is_python_asr_mode():
            return {"ok": False, "error": "not_python_asr_mode"}
        sr = self._import_speech_recognition()
        if sr is None:
            return {"ok": False, "error": "missing_speech_recognition"}

        was_running = bool(self._local_running)
        prev_langs = list(self.last_langs or [])
        if was_running:
            self.stop()
            time.sleep(0.2)

        try:
            self._sync_capture_mode()
            recognizer = sr.Recognizer()
            recognizer.dynamic_energy_threshold = True
            timeout_s = max(1.2, float(duration_seconds))
            phrase_s = max(1.2, float(duration_seconds))
            mic, selected_idx = self._select_local_microphone(sr)
            names = self._list_local_microphones(sr)
            device_name = names[selected_idx] if (selected_idx is not None and 0 <= selected_idx < len(names)) else None
            with mic as source:
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=0.35)
                except Exception:
                    pass
                audio = recognizer.listen(source, timeout=timeout_s, phrase_time_limit=phrase_s)
            rms = self._compute_audio_rms(audio)
            langs = self._dedupe_langs(language, fallback_languages)
            text, lang, err = self._recognize_with_provider(recognizer, audio, langs)
            if text:
                bad, reason = self._is_low_quality_transcript(text, lang=lang)
                if bad:
                    return {
                        "ok": True,
                        "deviceIndex": selected_idx,
                        "deviceName": device_name,
                        "rms": rms,
                        "text": "",
                        "lang": lang,
                        "error": f"filtered_low_quality:{reason}",
                    }
            return {
                "ok": True,
                "deviceIndex": selected_idx,
                "deviceName": device_name,
                "rms": rms,
                "text": text or "",
                "lang": lang,
                "error": err,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if was_running:
                if prev_langs:
                    self.start(language=prev_langs[0], fallback_languages=prev_langs[1:])
                else:
                    self.start(language=language, fallback_languages=fallback_languages)

    def _select_local_microphone(self, sr):
        """
        选择输入设备（麦克风/系统回采）：
        1) 优先使用显式配置索引（mic: VOICE_PYTHON_MIC_DEVICE_INDEX / loopback: VOICE_LOOPBACK_DEVICE_INDEX）
        2) 再尝试默认设备
        3) 默认失败时，按名称 hint 或第一个可用设备兜底
        """
        provider_now = self._normalize_provider_name()
        self._sync_capture_mode(provider_now)
        cloud_force_loopback = self._is_dashscope_force_loopback(provider_now)
        mic_like_tokens = ["microphone", "mic", "麦克风", "内建", "built-in", "builtin", "array"]
        output_like_tokens = ["speaker", "扬声器", "output", "耳机", "headphone", "hdmi", "display audio", "line out"]
        configured_idx = int(self._preferred_mic_device_index)
        if configured_idx < 0:
            if self._is_loopback_asr_mode():
                configured_idx = int(getattr(settings, "VOICE_LOOPBACK_DEVICE_INDEX", -1))
            else:
                configured_idx = int(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_INDEX", -1))
        name_hint_raw = str(self._preferred_mic_name_hint or "").strip().lower()
        if not name_hint_raw:
            if self._is_loopback_asr_mode():
                name_hint_raw = str(getattr(settings, "VOICE_LOOPBACK_DEVICE_NAME_HINT", "") or "").strip().lower()
            else:
                name_hint_raw = str(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_NAME_HINT", "") or "").strip().lower()
        name_hints = [x.strip() for x in re.split(r"[,;|/]+", name_hint_raw) if str(x or "").strip()]

        names = self._list_local_microphones(sr)

        if configured_idx >= 0:
            if (
                cloud_force_loopback
                and isinstance(names, list)
                and 0 <= configured_idx < len(names)
                and (
                    any(tok in str(names[configured_idx] or "").lower() for tok in mic_like_tokens)
                    or any(tok in str(names[configured_idx] or "").lower() for tok in output_like_tokens)
                )
            ):
                configured_idx = -1
            else:
                try:
                    return sr.Microphone(device_index=configured_idx), configured_idx
                except Exception:
                    pass

        if not names:
            # 没有枚举到设备时再尝试系统默认设备。
            if self._is_loopback_asr_mode() and cloud_force_loopback:
                raise RuntimeError("loopback_device_required_for_dashscope_cloud_asr")
            try:
                return sr.Microphone(), None
            except Exception:
                raise RuntimeError("No Input Device Available")

        if name_hints:
            for idx, name in enumerate(names):
                lowered = str(name or "").lower()
                if cloud_force_loopback and any(tok in lowered for tok in output_like_tokens):
                    continue
                if any(h in name.lower() for h in name_hints):
                    return sr.Microphone(device_index=idx), idx

        # 自动优先选择匹配当前采集模式的设备。
        if self._is_loopback_asr_mode():
            preferred_tokens = [
                "loopback", "stereo mix", "立体声混音", "what u hear",
                "blackhole", "vb-audio", "vb cable", "cable output",
                "monitor of", "wave out", "system audio", "系统声音", "soundflower",
            ]
        else:
            preferred_tokens = [
                "microphone", "mic", "麦克风", "内建", "built-in", "builtin",
                "array", "default",
            ]
        for idx, name in enumerate(names):
            n = str(name or "").lower()
            if any(tok in n for tok in preferred_tokens):
                try:
                    return sr.Microphone(device_index=idx), idx
                except Exception:
                    continue

        if self._is_loopback_asr_mode():
            # loopback 模式尽量避免回退到实体麦克风，优先选择“非麦克风命名”的设备。
            for idx, name in enumerate(names):
                n = str(name or "").lower()
                if any(tok in n for tok in mic_like_tokens):
                    continue
                if any(tok in n for tok in output_like_tokens):
                    continue
                try:
                    return sr.Microphone(device_index=idx), idx
                except Exception:
                    continue
            if cloud_force_loopback:
                raise RuntimeError("loopback_device_required_for_dashscope_cloud_asr")

        if self._is_loopback_asr_mode() and cloud_force_loopback:
            raise RuntimeError("loopback_device_required_for_dashscope_cloud_asr")

        # 最后兜底：第一个可用设备
        for idx in range(len(names)):
            try:
                if self._is_loopback_asr_mode() and not self._loopback_fallback_warned:
                    self._loopback_fallback_warned = True
                    logger.warning(
                        "loopback 模式未找到明显回采设备，已回退到首个可用输入设备。"
                        "建议在设置中指定 VOICE_LOOPBACK_DEVICE_INDEX/NAME_HINT。"
                    )
                return sr.Microphone(device_index=idx), idx
            except Exception:
                continue
        raise RuntimeError("No working input device found")

    def _compute_audio_rms(self, audio):
        raw = audio.get_raw_data() if hasattr(audio, "get_raw_data") else b""
        if not raw:
            return 0
        sample_width = int(getattr(audio, "sample_width", 2) or 2)
        if sample_width != 2:
            return 0
        count = len(raw) // 2
        if count <= 0:
            return 0
        try:
            samples = struct.unpack("<{}h".format(count), raw[: count * 2])
            power = sum(s * s for s in samples) / float(count)
            return int(math.sqrt(power))
        except Exception:
            return 0

    def _load_whisper_model(self, model_name, download_root):
        model_name = str(model_name or "tiny").strip()
        if self._whisper_model is not None and self._whisper_model_name == model_name:
            return self._whisper_model
        import whisper  # 延迟导入，避免非 whisper 模式的启动开销

        self._whisper_model = whisper.load_model(model_name, download_root=download_root)
        self._whisper_model_name = model_name
        return self._whisper_model

    def prewarm_local_asr(self, force=False):
        """
        预热本地 ASR 模型，减少首次识别延迟。
        """
        provider = self._normalize_provider_name()
        if not force and provider not in {"whisper_local", "auto"}:
            return {"ok": True, "skipped": "provider_no_whisper"}
        if self._whisper_model is not None and not force:
            return {"ok": True, "status": "already_ready"}
        if self._whisper_prewarm_started and not force:
            return {"ok": True, "status": "prewarming"}
        self._whisper_prewarm_started = True
        whisper_model = str(getattr(settings, "VOICE_WHISPER_MODEL", "tiny") or "tiny")
        whisper_root = str(
            Path(getattr(settings, "VOICE_WHISPER_DOWNLOAD_ROOT", "data/whisper_cache"))
            .expanduser()
            .resolve()
        )
        try:
            Path(whisper_root).mkdir(parents=True, exist_ok=True)
            self._load_whisper_model(whisper_model, whisper_root)
            logger.info(f"Whisper 本地模型预热完成: model={whisper_model}")
            return {"ok": True, "status": "ready", "model": whisper_model}
        except Exception as e:
            logger.warning(f"Whisper 本地模型预热失败: {e}")
            return {"ok": False, "error": str(e), "model": whisper_model}
        finally:
            self._whisper_prewarm_started = False

    def _preprocess_pcm16_for_whisper(self, pcm_bytes, sample_rate=16000):
        raw = bytes(pcm_bytes or b"")
        if not raw:
            return np.array([], dtype=np.float32)
        src_rate = max(8000, int(sample_rate or 16000))
        if src_rate != 16000:
            try:
                raw, _ = audioop.ratecv(raw, 2, 1, src_rate, 16000, None)
            except Exception:
                return np.array([], dtype=np.float32)
        pcm = np.frombuffer(raw, dtype=np.int16)
        if pcm.size == 0:
            return np.array([], dtype=np.float32)

        audio_np = (pcm.astype(np.float32) / 32768.0).flatten()
        if audio_np.size < int(16000 * 0.20):
            return np.array([], dtype=np.float32)

        abs_np = np.abs(audio_np)
        peak = float(np.max(abs_np)) if abs_np.size else 0.0
        if peak < 1e-4:
            return np.array([], dtype=np.float32)

        # 去掉首尾静音，减少 whisper 抓到无效上下文导致只出单词。
        gate = max(0.004, peak * 0.05)
        voiced = np.where(abs_np >= gate)[0]
        if voiced.size > 0:
            pad = int(16000 * 0.12)
            start = max(0, int(voiced[0]) - pad)
            end = min(audio_np.size, int(voiced[-1]) + pad)
            audio_np = audio_np[start:end]
            abs_np = np.abs(audio_np)

        rms = float(np.sqrt(np.mean(np.square(audio_np))) + 1e-8)
        target_rms = 0.10
        gain = target_rms / max(rms, 1e-8)
        gain = min(6.0, max(0.85, gain))
        audio_np = np.clip(audio_np * gain, -0.98, 0.98)

        if audio_np.size < int(16000 * 0.20):
            return np.array([], dtype=np.float32)
        return audio_np.astype(np.float32, copy=False)

    def _transcribe_audio_np_with_local_whisper(self, audio_np, lang=None, no_speech_threshold=None):
        if audio_np is None or int(getattr(audio_np, "size", 0)) == 0:
            return ""
        whisper_model = str(getattr(settings, "VOICE_WHISPER_MODEL", "tiny") or "tiny")
        whisper_root = str(
            Path(getattr(settings, "VOICE_WHISPER_DOWNLOAD_ROOT", "data/whisper_cache"))
            .expanduser()
            .resolve()
        )
        Path(whisper_root).mkdir(parents=True, exist_ok=True)

        model = self._load_whisper_model(whisper_model, whisper_root)
        lang_short = (str(lang or "").split("-")[0] or "").lower() or None
        if no_speech_threshold is None:
            no_speech_threshold = float(getattr(settings, "VOICE_WHISPER_NO_SPEECH_THRESHOLD", 0.50) or 0.50)
        kwargs = {
            "task": "transcribe",
            "fp16": False,
            "condition_on_previous_text": False,
            "temperature": 0.0,
            "no_speech_threshold": float(no_speech_threshold),
            "logprob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
        }
        if lang_short:
            kwargs["language"] = lang_short
        result = model.transcribe(audio_np, **kwargs)
        return str((result or {}).get("text") or "").strip()

    def _transcribe_pcm16_with_local_whisper(self, pcm_bytes, sample_rate=16000, lang=None, no_speech_threshold=None):
        audio_np = self._preprocess_pcm16_for_whisper(pcm_bytes, sample_rate=sample_rate)
        return self._transcribe_audio_np_with_local_whisper(
            audio_np=audio_np,
            lang=lang,
            no_speech_threshold=no_speech_threshold,
        )

    def _transcribe_with_local_whisper(self, audio, lang=None):
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        if not raw:
            return ""
        return self._transcribe_pcm16_with_local_whisper(
            pcm_bytes=raw,
            sample_rate=16000,
            lang=lang,
            no_speech_threshold=float(getattr(settings, "VOICE_WHISPER_NO_SPEECH_THRESHOLD", 0.50) or 0.50),
        )

    def _get_dashscope_api_key(self):
        return str(getattr(settings, "VOICE_DASHSCOPE_API_KEY", "") or "").strip()

    def _resolve_dashscope_language_hints(self, lang=None):
        configured = list(getattr(settings, "VOICE_DASHSCOPE_LANGUAGE_HINTS", []) or [])
        if configured:
            return [str(x or "").strip() for x in configured if str(x or "").strip()]
        code = str(lang or "").strip().lower()
        if not code:
            return []
        if code.startswith("zh"):
            return ["zh"]
        if code.startswith("en"):
            return ["en"]
        if code.startswith("yue"):
            return ["yue"]
        if code.startswith("ja"):
            return ["ja"]
        return []

    def _extract_text_from_dashscope_result(self, result):
        texts = []
        if result is None:
            return ""
        try:
            sentence = result.get_sentence() if hasattr(result, "get_sentence") else None
            if isinstance(sentence, dict):
                t = str(sentence.get("text") or "").strip()
                if t:
                    texts.append(t)
        except Exception:
            pass

        data = result
        if not isinstance(data, dict):
            try:
                if hasattr(result, "to_dict"):
                    data = result.to_dict()
                elif hasattr(result, "__dict__"):
                    data = dict(result.__dict__)
            except Exception:
                data = result

        text_keys = {"text", "transcript", "sentence_text", "result_text"}
        container_keys = {"sentence", "sentences", "output", "payload", "result", "results", "data", "message"}

        def _walk(node, depth=0):
            if depth > 8 or node is None:
                return
            if isinstance(node, str):
                s = node.strip()
                if s:
                    texts.append(s)
                return
            if isinstance(node, (list, tuple)):
                for it in node:
                    _walk(it, depth + 1)
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    key = str(k or "").strip().lower()
                    if key in text_keys and isinstance(v, str):
                        s = v.strip()
                        if s:
                            texts.append(s)
                        continue
                    if key in container_keys or isinstance(v, (dict, list, tuple)):
                        _walk(v, depth + 1)

        _walk(data, depth=0)
        if not texts:
            return ""
        # 优先返回较长文本，通常更接近最终句子。
        texts = [t for t in texts if t and len(t) <= 400]
        if not texts:
            return ""
        return max(texts, key=len)

    def _transcribe_wav_with_dashscope_funasr(self, wav_data, sample_rate=16000, lang=None):
        api_key = self._get_dashscope_api_key()
        if not api_key:
            raise RuntimeError("dashscope_api_key_missing")

        try:
            from dashscope.audio.asr import Recognition  # type: ignore
        except Exception as e:
            raise RuntimeError(f"dashscope_sdk_unavailable:{e}")

        sample_rate = max(8000, int(sample_rate or 16000))
        if not wav_data:
            return ""
        if len(wav_data) < int(sample_rate * 0.20 * 2) + 44:
            return ""

        model = str(getattr(settings, "VOICE_DASHSCOPE_MODEL", "paraformer-realtime-v2") or "paraformer-realtime-v2").strip()
        base_ws = str(getattr(settings, "VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL", "") or "").strip()

        recognition_kwargs = {
            "model": model,
            "format": "wav",
            "sample_rate": sample_rate,
            "api_key": api_key,
        }
        if base_ws:
            recognition_kwargs["base_websocket_api_url"] = base_ws

        call_kwargs = {}
        language_hints = self._resolve_dashscope_language_hints(lang=lang)
        if language_hints:
            call_kwargs["language_hints"] = language_hints
        if bool(getattr(settings, "VOICE_DASHSCOPE_ENABLE_PUNCTUATION", True)):
            call_kwargs["semantic_punctuation_enabled"] = True
        if bool(getattr(settings, "VOICE_DASHSCOPE_DISABLE_ITN", False)):
            call_kwargs["inverse_text_normalization_enabled"] = False

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
                fp.write(wav_data)
                tmp_path = fp.name

            class _NoopRecognitionCallback:
                def on_open(self):
                    return None

                def on_complete(self):
                    return None

                def on_error(self, result):
                    return None

                def on_close(self):
                    return None

                def on_event(self, result):
                    return None

            # 兼容不同 dashscope SDK 构造签名：
            # 1) callback 关键字参数（常见）
            # 2) callback 位置参数（部分版本）
            # 3) 无 callback（历史版本）
            callback_obj = _NoopRecognitionCallback()
            init_errors = []
            recognition = None
            try:
                recognition = Recognition(
                    callback=callback_obj,
                    **recognition_kwargs,
                )
            except TypeError as e_kw:
                init_errors.append(str(e_kw or ""))
                try:
                    model = recognition_kwargs.get("model")
                    fmt = recognition_kwargs.get("format")
                    sample_rate = recognition_kwargs.get("sample_rate")
                    extra_kwargs = {
                        k: v
                        for k, v in recognition_kwargs.items()
                        if k not in {"model", "format", "sample_rate"}
                    }
                    recognition = Recognition(
                        model,
                        callback_obj,
                        fmt,
                        sample_rate,
                        **extra_kwargs,
                    )
                except TypeError as e_pos:
                    init_errors.append(str(e_pos or ""))
                    try:
                        recognition = Recognition(**recognition_kwargs)
                    except TypeError as e_plain:
                        init_errors.append(str(e_plain or ""))
                        msg = " | ".join([m for m in init_errors if m]).strip() or "unknown"
                        raise RuntimeError(f"dashscope_init_error:{msg}") from e_plain

            result = recognition.call(tmp_path, **call_kwargs)
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

        status_code = getattr(result, "status_code", None)
        if status_code is not None:
            try:
                code = int(status_code)
            except Exception:
                code = status_code
            if code != int(HTTPStatus.OK):
                message = str(
                    getattr(result, "message", "")
                    or getattr(result, "error_message", "")
                    or getattr(result, "error", "")
                    or ""
                ).strip()
                raise RuntimeError(f"dashscope_http_{code}:{message or 'request_failed'}")

        return str(self._extract_text_from_dashscope_result(result) or "").strip()

    def _transcribe_with_dashscope_funasr(self, audio, lang=None):
        sample_rate = max(8000, int(getattr(settings, "VOICE_DASHSCOPE_SAMPLE_RATE", 16000) or 16000))
        wav_data = audio.get_wav_data(convert_rate=sample_rate, convert_width=2)
        return self._transcribe_wav_with_dashscope_funasr(
            wav_data=wav_data,
            sample_rate=sample_rate,
            lang=lang,
        )

    def _build_wav_from_pcm16(self, pcm_bytes, sample_rate=16000, channels=1):
        pcm_bytes = bytes(pcm_bytes or b"")
        if not pcm_bytes:
            return b""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(max(1, int(channels or 1)))
            wf.setsampwidth(2)
            wf.setframerate(max(8000, int(sample_rate or 16000)))
            wf.writeframes(pcm_bytes)
        return buf.getvalue()

    def _start_tab_audio_stream_capture(self, langs):
        ensure_browser_page = getattr(self.vision_agent, "ensure_browser_page_connection", None)
        if callable(ensure_browser_page):
            try:
                # 显式启动播放器流 ASR 时强制探活，避免被直播页重连冷却阻断。
                browser_ready = bool(ensure_browser_page(force=True, prefer_media_tab=True))
            except TypeError:
                browser_ready = bool(ensure_browser_page(force=True))
        else:
            browser_ready = bool(self.vision_agent.ensure_connection())
        if not browser_ready:
            self._tab_audio_running = False
            self._tab_audio_error = "browser_not_connected"
            return False

        self._tab_audio_running = False
        self._tab_audio_error = None
        self._tab_audio_last_text = ""
        self._tab_audio_last_text_lang = None
        self._tab_audio_last_audio_at = None
        self._tab_audio_last_audio_rms = 0
        self._tab_audio_no_text_count = 0
        self._tab_audio_langs = list(langs or ["zh-CN"])
        self.last_langs = list(self._tab_audio_langs)
        self.last_lang = (self.last_langs[0] if self.last_langs else self.last_lang)
        self._tab_audio_buffer = bytearray()
        self._tab_audio_pending_text = ""
        self._tab_audio_pending_lang = None
        self._tab_audio_pending_started_at = None
        self._tab_audio_pending_updated_at = None
        self._tab_audio_last_chunk_at = None

        script = """
        return ((cfg) => {
          cfg = cfg || {};
          const targetRate = Number(cfg.targetRate || 16000);
          const state = (window.__liveAssistantTabAudioState = window.__liveAssistantTabAudioState || {});
          const queue = (window.__liveAssistantTabAudioQueue = window.__liveAssistantTabAudioQueue || []);

          const setErr = (e, details) => {
            state.error = String(e || 'unknown');
            state.running = false;
            state.lastErrorAt = Date.now();
            return { ok: false, reason: state.error, details: details || {} };
          };

          try {
            const prev = window.__liveAssistantTabAudioController;
            if (prev && typeof prev.stop === 'function') {
              try { prev.stop(); } catch (e) {}
            }
          } catch (e) {}

          const medias = Array.from(document.querySelectorAll('video, audio'));
          if (!medias.length) return setErr('media_element_not_found');
          const sorted = medias
            .map((m) => {
              const score =
                (m.paused ? 0 : 60) +
                ((m.currentTime || 0) > 0 ? 20 : 0) +
                ((m.readyState || 0) >= 2 ? 10 : 0) +
                ((m.muted ? 0 : 1) * 10);
              return { m, score };
            })
            .sort((a, b) => b.score - a.score);
          const captureMediaStream = (media) => {
            try {
              if (typeof media.captureStream === 'function') return media.captureStream();
              if (typeof media.mozCaptureStream === 'function') return media.mozCaptureStream();
              return null;
            } catch (e) {
              return null;
            }
          };

          let media = null;
          let stream = null;
          let noTrackCount = 0;
          for (const item of sorted) {
            const candidate = item && item.m;
            if (!candidate) continue;
            const candidateStream = captureMediaStream(candidate);
            if (!candidateStream) continue;
            const audioTracks = (candidateStream.getAudioTracks && candidateStream.getAudioTracks()) || [];
            if (audioTracks.length > 0) {
              media = candidate;
              stream = candidateStream;
              break;
            }
            noTrackCount += 1;
          }
          if (!media) {
            media = sorted.length ? (sorted[0] && sorted[0].m) : null;
          }
          if (!media) {
            return setErr('media_element_not_found');
          }
          if (!stream) {
            // 某些页面/打包环境下 captureStream 拿不到音轨，回退 media element source。
            try {
              if (media.paused && typeof media.play === 'function') {
                media.play().catch(() => {});
              }
            } catch (e) {}
          }

          const AudioCtx = window.AudioContext || window.webkitAudioContext;
          if (!AudioCtx) return setErr('audio_context_unavailable');

          const ctx = new AudioCtx({ sampleRate: targetRate });
          let src = null;
          let captureDriver = '';
          try {
            if (stream) {
              src = ctx.createMediaStreamSource(stream);
              captureDriver = 'capture_stream';
            } else {
              src = ctx.createMediaElementSource(media);
              captureDriver = 'media_element_source';
            }
          } catch (e) {
            return setErr(
              'create_audio_source_failed:' + String(e),
              {
                mediaCandidateCount: sorted.length,
                mediaNoTrackCount: noTrackCount,
                mediaTag: (media && media.tagName) || '',
                mediaSrc: (media && (media.currentSrc || media.src)) || '',
              }
            );
          }
          const proc = ctx.createScriptProcessor(4096, 2, 1);
          const mute = ctx.createGain();
          mute.gain.value = 0.0;
          const passthrough = ctx.createGain();
          passthrough.gain.value = 1.0;

          proc.onaudioprocess = (ev) => {
            try {
              const channels = Math.max(1, Number(ev.inputBuffer.numberOfChannels || 1));
              const ch0 = ev.inputBuffer.getChannelData(0);
              if (!ch0 || !ch0.length) return;
              const len = ch0.length;
              let rmsAcc = 0;
              const out = new Int16Array(len);
              for (let i = 0; i < len; i++) {
                let s = 0;
                for (let c = 0; c < channels; c++) {
                  try {
                    const ch = ev.inputBuffer.getChannelData(c);
                    s += (ch && i < ch.length) ? ch[i] : 0;
                  } catch (e) {}
                }
                s = s / channels;
                if (s > 1) s = 1;
                if (s < -1) s = -1;
                out[i] = s < 0 ? s * 32768 : s * 32767;
                rmsAcc += s * s;
              }
              let bin = '';
              const bytes = new Uint8Array(out.buffer);
              const chunk = 0x8000;
              for (let i = 0; i < bytes.length; i += chunk) {
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
              }
              const b64 = btoa(bin);
              const rms = Math.sqrt(rmsAcc / Math.max(1, len)) * 32768;
              queue.push({
                ts: Date.now(),
                sampleRate: Number(ev.inputBuffer.sampleRate || targetRate || 16000),
                channels: Number(ev.inputBuffer.numberOfChannels || 1),
                rms: Math.round(rms),
                pcmB64: b64,
              });
              if (queue.length > 120) queue.splice(0, queue.length - 120);
              state.lastAudioAt = Date.now();
              state.lastAudioRms = Math.round(rms);
            } catch (e) {
              state.error = 'processor_error:' + String(e);
              state.lastErrorAt = Date.now();
            }
          };

          src.connect(proc);
          proc.connect(mute);
          mute.connect(ctx.destination);
          // media element source 需要显式回连 destination，否则页面声音会被“吃掉”。
          if (captureDriver === 'media_element_source') {
            try {
              src.connect(passthrough);
              passthrough.connect(ctx.destination);
            } catch (e) {}
          }
          if (ctx.state === 'suspended' && typeof ctx.resume === 'function') {
            ctx.resume().catch(() => {});
          }
          const keepAliveTimer = setInterval(() => {
            try {
              if (ctx.state === 'suspended' && typeof ctx.resume === 'function') {
                ctx.resume().catch(() => {});
              }
            } catch (e) {}
          }, 1500);

          const controller = {
            media,
            stream,
            ctx,
            src,
            proc,
            mute,
            passthrough,
            keepAliveTimer,
            stop: () => {
              try { clearInterval(keepAliveTimer); } catch (e) {}
              try { proc.disconnect(); } catch (e) {}
              try { src.disconnect(); } catch (e) {}
              try { mute.disconnect(); } catch (e) {}
              try { passthrough.disconnect(); } catch (e) {}
              try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
              try { ctx.close(); } catch (e) {}
            },
          };
          window.__liveAssistantTabAudioController = controller;
          state.running = true;
          state.error = null;
          state.startedAt = Date.now();
          state.lastErrorAt = null;
          state.captureDriver = captureDriver;
          state.mediaCandidateCount = sorted.length;
          state.mediaNoTrackCount = noTrackCount;
          state.mediaTag = (media.tagName || '').toLowerCase();
          state.mediaSrc = media.currentSrc || media.src || '';
          return {
            ok: true,
            mediaTag: state.mediaTag,
            mediaSrc: state.mediaSrc,
            captureDriver,
            mediaCandidateCount: sorted.length,
            mediaNoTrackCount: noTrackCount,
          };
        })(arguments[0] || {});
        """

        ctx_name, result, errors = self._run_js_with_reconnect(
            script,
            {"targetRate": 16000},
            prefer_ctx_name=self.active_ctx_name,
        )
        if result is None:
            ensure_browser_page = getattr(self.vision_agent, "ensure_browser_page_connection", None)
            if callable(ensure_browser_page):
                try:
                    try:
                        ensure_browser_page(force=True, prefer_media_tab=True)
                    except TypeError:
                        ensure_browser_page(force=True)
                except Exception:
                    pass
            ctx_name, result, errors2 = self._run_js_with_reconnect(
                script,
                {"targetRate": 16000},
                prefer_ctx_name=self.active_ctx_name,
            )
            errors = list(errors or []) + list(errors2 or [])
        ok = bool(isinstance(result, dict) and result.get("ok"))
        if ok:
            self.active_ctx_name = ctx_name or self.active_ctx_name
            self._tab_audio_running = True
            self._tab_audio_error = None
            self._tab_audio_last_restart_at = time.time()
            return True
        self._tab_audio_running = False
        if isinstance(result, dict):
            reason = result.get("reason") or "tab_audio_start_failed"
        else:
            reason = "browser_page_context_unavailable" if not ctx_name else "tab_audio_js_no_result"
        self._tab_audio_error = str(reason or "tab_audio_start_failed")
        self.last_start_reason = self._tab_audio_error
        self.last_start_errors = list(errors or [])[:5]
        detail = {}
        if isinstance(result, dict):
            detail = {
                "details": result.get("details") if isinstance(result.get("details"), dict) else {},
                "captureDriver": result.get("captureDriver"),
                "mediaTag": result.get("mediaTag"),
                "mediaSrc": result.get("mediaSrc"),
                "mediaCandidateCount": result.get("mediaCandidateCount"),
                "mediaNoTrackCount": result.get("mediaNoTrackCount"),
            }
        self.last_start_diag = {"ctx": ctx_name, **detail}
        logger.warning(
            f"TabAudio ASR 启动失败: reason={self._tab_audio_error}, ctx={ctx_name}, "
            f"detail={detail}, errors={self.last_start_errors[:2]}"
        )
        return False

    def _stop_tab_audio_stream_capture(self):
        script = """
        return (() => {
          const st = window.__liveAssistantTabAudioState || (window.__liveAssistantTabAudioState = {});
          const ctl = window.__liveAssistantTabAudioController;
          try {
            if (ctl && typeof ctl.stop === 'function') ctl.stop();
          } catch (e) {}
          window.__liveAssistantTabAudioController = null;
          st.running = false;
          return { ok: true };
        })();
        """
        _, result = self._run_js_in_contexts(script, prefer_ctx_name=self.active_ctx_name)
        self._tab_audio_running = False
        self._tab_audio_buffer = bytearray()
        self._tab_audio_pending_text = ""
        self._tab_audio_pending_lang = None
        self._tab_audio_pending_started_at = None
        self._tab_audio_pending_updated_at = None
        self._tab_audio_last_chunk_at = None
        return bool(isinstance(result, dict) and result.get("ok"))

    def _poll_tab_audio_chunks(self, max_items=40):
        script = """
        return ((limit) => {
          const queue = window.__liveAssistantTabAudioQueue || [];
          const st = window.__liveAssistantTabAudioState || {};
          const n = Math.max(1, Number(limit || 40));
          const items = queue.splice(0, Math.min(n, queue.length));
          return { items, state: st };
        })(arguments[0] || 40);
        """
        _, result = self._run_js_in_contexts(
            script,
            int(max(1, max_items)),
            prefer_ctx_name=self.active_ctx_name,
        )
        if not isinstance(result, dict):
            ctx_name, result2, errors = self._run_js_with_reconnect(
                script,
                int(max(1, max_items)),
                prefer_ctx_name=self.active_ctx_name,
            )
            if ctx_name:
                self.active_ctx_name = ctx_name
            if isinstance(result2, dict):
                result = result2
            elif errors:
                logger.debug(f"TabAudio 轮询失败，JS上下文不可用: {errors[:2]}")
        if not isinstance(result, dict):
            return [], {}
        return list(result.get("items") or []), dict(result.get("state") or {})

    def _maybe_restart_tab_audio_stream_capture(self, reason=""):
        now = time.time()
        cooldown = float(self._tab_audio_restart_cooldown_seconds)
        if "media_stream_no_audio_track" in str(self._tab_audio_error or ""):
            # 页面未开始播放前，音轨通常为空；放慢重启节奏，避免日志风暴。
            cooldown = max(cooldown, 8.0)
        if now - float(self._tab_audio_last_restart_at or 0.0) < cooldown:
            return False
        self._tab_audio_last_restart_at = now
        langs = list(self._tab_audio_langs or self.last_langs or ["zh-CN"])
        ok = self._start_tab_audio_stream_capture(langs)
        if ok:
            logger.info(f"TabAudio ASR 已自动重启: reason={reason or 'unknown'}")
        else:
            logger.warning(f"TabAudio ASR 自动重启失败: reason={reason or 'unknown'}, err={self._tab_audio_error}")
        return ok

    def _transcribe_tab_audio_pcm_with_provider(self, pcm_bytes, sample_rate, langs=None):
        pcm_bytes = bytes(pcm_bytes or b"")
        if not pcm_bytes:
            return None, None, None
        lang_candidates = list(langs or self._tab_audio_langs or ["zh-CN"])
        provider = self._normalize_provider_name()
        if provider == "whisper_local":
            try:
                threshold = float(getattr(settings, "VOICE_TAB_AUDIO_WHISPER_NO_SPEECH_THRESHOLD", 0.35) or 0.35)
                text_candidates = []
                max_langs = max(1, int(getattr(settings, "VOICE_WHISPER_MAX_LANGS", 1) or 1))
                for lang in lang_candidates[:max_langs]:
                    text = self._transcribe_pcm16_with_local_whisper(
                        pcm_bytes=pcm_bytes,
                        sample_rate=sample_rate,
                        lang=lang,
                        no_speech_threshold=threshold,
                    )
                    if not text:
                        continue
                    if not self._is_text_lang_compatible(text, expected_lang=lang, detected_lang=lang):
                        continue
                    score = (3.0 if self._is_command_like_text(text) else 0.0) + min(len(text), 120) / 120.0
                    text_candidates.append((score, text, lang))
                if not text_candidates:
                    auto_text = self._transcribe_pcm16_with_local_whisper(
                        pcm_bytes=pcm_bytes,
                        sample_rate=sample_rate,
                        lang=None,
                        no_speech_threshold=threshold,
                    )
                    if auto_text:
                        expected_lang = (lang_candidates[0] if lang_candidates else None)
                        if self._is_text_lang_compatible(auto_text, expected_lang=expected_lang, detected_lang=None):
                            score = (3.0 if self._is_command_like_text(auto_text) else 0.0) + min(len(auto_text), 120) / 120.0
                            text_candidates.append((score, auto_text, expected_lang))
                if text_candidates:
                    _, text, lang = max(text_candidates, key=lambda x: x[0])
                    self._last_provider_selected = "whisper_local"
                    self._last_provider_error = None
                    self._last_provider_at = int(time.time() * 1000)
                    return text, (lang or (lang_candidates[0] if lang_candidates else None)), None
                self._last_provider_selected = "whisper_local"
                self._last_provider_error = None
                self._last_provider_at = int(time.time() * 1000)
                return None, (lang_candidates[0] if lang_candidates else None), None
            except Exception as e:
                self._last_provider_selected = None
                self._last_provider_error = str(e)
                self._last_provider_at = int(time.time() * 1000)
                return None, None, str(e)

        sr = self._import_speech_recognition()
        if sr is None:
            if provider != "dashscope_funasr":
                return None, None, "speech_recognition_unavailable_for_selected_provider"
            try:
                wav_data = self._build_wav_from_pcm16(
                    pcm_bytes=pcm_bytes,
                    sample_rate=sample_rate,
                    channels=1,
                )
                text = self._transcribe_wav_with_dashscope_funasr(
                    wav_data=wav_data,
                    sample_rate=sample_rate,
                    lang=(lang_candidates[0] if lang_candidates else None),
                )
                self._last_provider_selected = "dashscope_funasr"
                self._last_provider_error = None
                self._last_provider_at = int(time.time() * 1000)
                return text, (lang_candidates[0] if lang_candidates else None), None
            except Exception as e:
                self._last_provider_selected = None
                self._last_provider_error = str(e)
                self._last_provider_at = int(time.time() * 1000)
                return None, None, str(e)

        try:
            if self._tab_audio_recognizer is None:
                self._tab_audio_recognizer = sr.Recognizer()
            audio = sr.AudioData(pcm_bytes, int(sample_rate), 2)
            text, lang, err = self._recognize_with_provider(
                self._tab_audio_recognizer,
                audio,
                lang_candidates,
            )
            return text, (lang or (lang_candidates[0] if lang_candidates else None)), err
        except Exception as e:
            return None, None, str(e)

    def _merge_tab_audio_text(self, base_text, new_text):
        base = str(base_text or "").strip()
        incoming = str(new_text or "").strip()
        if not base:
            return incoming
        if not incoming:
            return base
        if incoming in base:
            return base
        if base in incoming:
            return incoming

        base_lower = base.lower()
        incoming_lower = incoming.lower()
        max_overlap = min(len(base_lower), len(incoming_lower), 64)
        overlap = 0
        for n in range(max_overlap, 2, -1):
            if base_lower[-n:] == incoming_lower[:n]:
                overlap = n
                break

        if overlap > 0:
            merged = base + incoming[overlap:]
        else:
            need_space = bool(
                re.search(r"[a-z0-9]$", base, re.IGNORECASE)
                and re.search(r"^[a-z0-9]", incoming, re.IGNORECASE)
            )
            merged = f"{base}{' ' if need_space else ''}{incoming}"
        return re.sub(r"\s+", " ", merged).strip()

    def _append_tab_audio_fragment(self, text, lang=None):
        frag = str(text or "").strip()
        if not frag:
            return
        now_ms = int(time.time() * 1000)
        self._tab_audio_pending_text = self._merge_tab_audio_text(self._tab_audio_pending_text, frag)
        if not self._tab_audio_pending_lang:
            self._tab_audio_pending_lang = lang
        if self._tab_audio_pending_started_at is None:
            self._tab_audio_pending_started_at = now_ms
        self._tab_audio_pending_updated_at = now_ms

    def _should_flush_tab_audio_pending_text(self, now_ms):
        text = str(self._tab_audio_pending_text or "").strip()
        if not text:
            return False
        if re.search(r"[。！？!?；;…]$", text):
            return True
        if len(text) >= self._tab_audio_emit_max_chars:
            return True

        started_at = int(self._tab_audio_pending_started_at or now_ms)
        if now_ms - started_at >= self._tab_audio_emit_max_wait_ms:
            return True

        updated_at = int(self._tab_audio_pending_updated_at or started_at)
        idle_ms = now_ms - updated_at
        try:
            rms = int(self._tab_audio_last_audio_rms or 0)
        except Exception:
            rms = 0
        if idle_ms >= self._tab_audio_emit_idle_ms and rms <= self._tab_audio_silence_rms:
            return True
        return False

    def _flush_tab_audio_pending_text(self):
        text = str(self._tab_audio_pending_text or "").strip()
        if not text:
            self._tab_audio_pending_lang = None
            self._tab_audio_pending_started_at = None
            self._tab_audio_pending_updated_at = None
            return None
        if len(text) < 3:
            self._tab_audio_pending_text = ""
            self._tab_audio_pending_lang = None
            self._tab_audio_pending_started_at = None
            self._tab_audio_pending_updated_at = None
            return None
        if self._is_noise_short_utterance(text):
            self._tab_audio_pending_text = ""
            self._tab_audio_pending_lang = None
            self._tab_audio_pending_started_at = None
            self._tab_audio_pending_updated_at = None
            return None

        lang = self._tab_audio_pending_lang or (self._tab_audio_langs[0] if self._tab_audio_langs else None)
        ts = int(time.time() * 1000)
        self._queue_push_text(source="tab_audio_stream", text=text, lang=lang)
        self._tab_audio_last_result_at = ts
        self._tab_audio_last_text = text
        self._tab_audio_last_text_lang = lang
        item = {
            "source": "tab_audio_stream",
            "text": text,
            "lang": lang,
            "confidence": None,
            "ts": ts,
        }
        self._tab_audio_pending_text = ""
        self._tab_audio_pending_lang = None
        self._tab_audio_pending_started_at = None
        self._tab_audio_pending_updated_at = None
        return item

    def poll_tab_audio_stream_transcripts(self):
        if (not self._tab_audio_running) and (not self._tab_audio_pending_text):
            self._maybe_restart_tab_audio_stream_capture(reason="not_running")
            if (not self._tab_audio_running) and (not self._tab_audio_pending_text):
                return []

        chunks, js_state = self._poll_tab_audio_chunks(max_items=60)
        if isinstance(js_state, dict):
            self._tab_audio_error = str(js_state.get("error") or self._tab_audio_error or "")
            self._tab_audio_last_audio_at = js_state.get("lastAudioAt") or self._tab_audio_last_audio_at
            try:
                self._tab_audio_last_audio_rms = int(js_state.get("lastAudioRms") or self._tab_audio_last_audio_rms or 0)
            except Exception:
                pass
            self._tab_audio_running = bool(js_state.get("running", self._tab_audio_running))

        if chunks:
            self._tab_audio_last_chunk_at = int(time.time() * 1000)

        for ch in chunks:
            if not isinstance(ch, dict):
                continue
            pcm_b64 = str(ch.get("pcmB64") or "")
            if not pcm_b64:
                continue
            try:
                pcm = base64.b64decode(pcm_b64.encode("ascii"), validate=False)
            except Exception:
                continue
            if not pcm:
                continue
            self._tab_audio_buffer.extend(pcm)
            try:
                self._tab_audio_sample_rate = max(8000, int(ch.get("sampleRate") or self._tab_audio_sample_rate or 16000))
            except Exception:
                self._tab_audio_sample_rate = 16000
            self._tab_audio_last_audio_at = int(ch.get("ts") or int(time.time() * 1000))
            try:
                self._tab_audio_last_audio_rms = int(ch.get("rms") or self._tab_audio_last_audio_rms or 0)
            except Exception:
                pass

        out_items = []
        min_bytes = max(3200, int(self._tab_audio_sample_rate * self._tab_audio_chunk_seconds * 2))
        max_bytes = max(min_bytes, int(self._tab_audio_sample_rate * self._tab_audio_max_chunk_seconds * 2))
        overlap_bytes = max(0, int(self._tab_audio_sample_rate * self._tab_audio_overlap_seconds * 2))
        filter_low_quality = bool(getattr(settings, "VOICE_TAB_AUDIO_FILTER_LOW_QUALITY", False))
        asr_calls = 0
        while len(self._tab_audio_buffer) >= min_bytes and asr_calls < 2:
            asr_calls += 1
            consume = min(len(self._tab_audio_buffer), max_bytes)
            pcm_bytes = bytes(self._tab_audio_buffer[:consume])
            self._tab_audio_buffer = bytearray(self._tab_audio_buffer[consume:])
            if overlap_bytes > 0 and len(pcm_bytes) > overlap_bytes:
                # 保留少量尾音作为下一轮上下文，降低“截断词”概率。
                self._tab_audio_buffer = bytearray(pcm_bytes[-overlap_bytes:]) + self._tab_audio_buffer
            try:
                text, lang, err = self._transcribe_tab_audio_pcm_with_provider(
                    pcm_bytes=pcm_bytes,
                    sample_rate=self._tab_audio_sample_rate,
                    langs=self._tab_audio_langs,
                )
                if text:
                    if filter_low_quality:
                        bad, reason = self._is_low_quality_transcript(text, lang=lang)
                        if bad:
                            self._tab_audio_no_text_count += 1
                            continue
                    self._append_tab_audio_fragment(text=text, lang=lang)
                    self._tab_audio_no_text_count = 0
                    self._tab_audio_error = None
                else:
                    if err:
                        self._tab_audio_error = f"asr_error: {err}"
                        self._last_provider_error = str(err)
                        self._last_provider_at = int(time.time() * 1000)
                    self._tab_audio_no_text_count += 1
            except Exception as e:
                self._tab_audio_error = f"asr_error: {e}"
                self._last_provider_selected = None
                self._last_provider_error = str(e)
                self._last_provider_at = int(time.time() * 1000)
                self._tab_audio_no_text_count += 1

        now_ms = int(time.time() * 1000)
        last_audio_at = int(self._tab_audio_last_audio_at or 0)
        last_chunk_at = int(self._tab_audio_last_chunk_at or 0)
        stall_ms = int(self._tab_audio_stall_restart_seconds * 1000.0)
        stalled = False
        if self._tab_audio_running:
            if last_chunk_at > 0 and (now_ms - last_chunk_at) > stall_ms:
                stalled = True
            elif last_audio_at > 0 and (now_ms - last_audio_at) > max(stall_ms, 6000):
                stalled = True
        if stalled:
            self._tab_audio_running = False
            self._tab_audio_error = "tab_audio_stalled"
            self._maybe_restart_tab_audio_stream_capture(reason="stalled")

        if self._should_flush_tab_audio_pending_text(now_ms):
            merged_item = self._flush_tab_audio_pending_text()
            if merged_item:
                out_items.append(merged_item)
        if (not self._tab_audio_running) and self._tab_audio_pending_text:
            merged_item = self._flush_tab_audio_pending_text()
            if merged_item:
                out_items.append(merged_item)
        return out_items

    def get_tab_audio_stream_state(self):
        return {
            "running": bool(self._tab_audio_running),
            "error": self._tab_audio_error,
            "lastResultAt": self._tab_audio_last_result_at,
            "lastText": self._tab_audio_last_text,
            "lastTextLang": self._tab_audio_last_text_lang,
            "lastAudioAt": self._tab_audio_last_audio_at,
            "lastAudioRms": self._tab_audio_last_audio_rms,
            "noTextCount": self._tab_audio_no_text_count,
            "sampleRate": self._tab_audio_sample_rate,
            "pendingText": str(self._tab_audio_pending_text or ""),
            "provider": str(self._last_provider_selected or self._normalize_provider_name()),
            "providerType": str(self._provider_runtime_kind(self._last_provider_selected or self._normalize_provider_name())),
            "providerError": self._last_provider_error,
        }

    def _recognize_with_provider(self, recognizer, audio, langs):
        provider = self._normalize_provider_name()
        primary_lang = str((langs or [""])[0] or "").lower()
        allow_google_fallback = bool(getattr(settings, "VOICE_ASR_ALLOW_GOOGLE_FALLBACK", False))
        dashscope_available = bool(self._get_dashscope_api_key())
        self._last_provider_base = provider
        self._last_provider_chain = []
        self._last_provider_attempt = None
        self._last_provider_selected = None
        self._last_provider_error = None
        self._last_provider_at = int(time.time() * 1000)
        # 统一 provider 链路（显式选择制）：
        # - 不再自动把 dashscope 当“备用链路”混入。
        # - 你选择哪个 provider，就按该链路执行。
        providers = [provider]
        if provider == "auto":
            local_first = bool(getattr(settings, "LOCAL_FIRST_MODE", False))
            if primary_lang.startswith("zh"):
                providers = ["whisper_local", "sphinx"]
                if allow_google_fallback:
                    providers.insert(1, "google")
            else:
                providers = ["whisper_local", "sphinx"] if local_first else ["google", "whisper_local", "sphinx"]
                if allow_google_fallback and "google" not in providers:
                    providers.append("google")
        elif provider == "google":
            providers = ["google", "whisper_local", "sphinx"] if allow_google_fallback else ["google"]
        elif provider == "whisper_local":
            providers = ["whisper_local", "sphinx"] if allow_google_fallback else ["whisper_local"]
            if allow_google_fallback and "google" not in providers:
                providers.insert(1, "google")
        elif provider == "dashscope_funasr":
            if allow_google_fallback:
                providers = ["dashscope_funasr", "google", "whisper_local", "sphinx"]
            else:
                providers = ["dashscope_funasr"]
        elif provider == "hybrid_local_cloud":
            providers = ["whisper_local", "dashscope_funasr", "sphinx"]
            if allow_google_fallback:
                providers.insert(2, "google")
        elif provider == "sphinx":
            providers = ["sphinx"]
        else:
            providers = ["whisper_local", "sphinx"] if primary_lang.startswith("zh") else ["google", "whisper_local", "sphinx"]
        if (not dashscope_available) and provider != "dashscope_funasr":
            providers = [p for p in providers if p != "dashscope_funasr"]

        deduped = []
        seen = set()
        for item in providers:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        providers = deduped
        self._last_provider_chain = list(providers)

        # Google 通道网络波动时：仅在非显式 google 模式下临时跳过，避免拖慢链路。
        if provider != "google" and time.time() < float(self._google_backoff_until or 0.0):
            providers = [p for p in providers if p != "google"]
            if not providers:
                providers = ["whisper_local", "sphinx"]
        # DashScope 通道网络波动时：仅在非显式 dashscope 模式下临时跳过。
        if provider != "dashscope_funasr" and time.time() < float(self._dashscope_backoff_until or 0.0):
            providers = [p for p in providers if p != "dashscope_funasr"]
            if not providers:
                providers = ["whisper_local", "sphinx"]
        chain_key = f"{provider}|{','.join(providers)}|{','.join(langs or [])}"
        if chain_key != self._provider_chain_logged:
            self._provider_chain_logged = chain_key
            logger.info(f"ASR provider chain: base={provider}, resolved={providers}, langs={langs}")

        def _is_google_network_error(msg):
            lower = str(msg or "").lower()
            keywords = [
                "timed out",
                "time-out",
                "gateway time-out",
                "gateway timeout",
                "connection",
                "temporarily unavailable",
                "service unavailable",
                "remote end closed connection",
            ]
            return any(k in lower for k in keywords)

        def _is_dashscope_network_error(msg):
            lower = str(msg or "").lower()
            keywords = [
                "timed out",
                "time-out",
                "timeout",
                "connection",
                "temporarily unavailable",
                "service unavailable",
                "network",
                "ssl",
                "connection reset",
                "remote end closed connection",
            ]
            return any(k in lower for k in keywords)

        def _allow_sphinx_for_langs(lang_list):
            # PocketSphinx 对英文效果较好；中文场景禁用，避免把中文误识别成英文乱码。
            return any(str(lang or "").lower().startswith("en") for lang in (lang_list or []))

        def _command_score(text):
            raw = str(text or "")
            if not raw:
                return -1
            lower = raw.lower()
            norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lower)
            score = 0
            command_words = [
                "置顶", "取消置顶", "秒杀", "上架", "链接", "商品", "橱窗",
                "pin", "unpin", "top", "link", "item", "product",
                "flash", "sale", "promotion", "deal", "launch", "lounge", "lanch", "lunch",
                "start", "cohost", "assistant", "streamassistant", "liveassistant",
            ]
            if any(w in lower for w in command_words):
                score += 3
            if re.search(r"[0-9]+|[零一二两三四五六七八九十]", raw):
                score += 2
            wake_words = list(getattr(settings, "VOICE_COMMAND_WAKE_WORDS", []) or [])
            wake_hit = False
            for w in wake_words:
                w = str(w or "").strip().lower()
                if not w:
                    continue
                w_norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", w)
                if w in lower or (w_norm and w_norm in norm):
                    wake_hit = True
                    break
            if wake_hit:
                score += 2
            if re.search(r"[\u4e00-\u9fff]", raw):
                score += 1
            score += min(len(raw.strip()), 32) / 32.0
            return score

        whisper_lang_limit = max(1, int(getattr(settings, "VOICE_WHISPER_MAX_LANGS", 1) or 1))
        provider_errors = []
        fallback_candidates = []
        for p in providers:
            self._last_provider_attempt = p
            self._last_provider_at = int(time.time() * 1000)
            if p == "sphinx":
                if not _allow_sphinx_for_langs(langs):
                    continue
                try:
                    text = recognizer.recognize_sphinx(audio)
                    if text:
                        expected_lang = (langs[0] if langs else "en-US")
                        if not self._is_text_lang_compatible(text, expected_lang=expected_lang, detected_lang="en-US"):
                            continue
                        score = _command_score(text)
                        fallback_candidates.append((score, text, "en-US", p))
                        if score >= 3.0:
                            self._last_provider_selected = p
                            self._last_provider_error = None
                            self._last_provider_at = int(time.time() * 1000)
                            return text, "en-US", None
                except Exception as e:
                    msg = str(e)
                    if "UnknownValueError" in type(e).__name__:
                        continue
                    provider_errors.append(f"sphinx:{msg}")
                    self._last_provider_error = f"sphinx:{msg}"
                    self._last_provider_at = int(time.time() * 1000)
                    last_err = msg
                continue

            recognized = []
            langs_for_provider = list(langs or [])
            if p == "whisper_local":
                # 为提升中英混说命中率，whisper 至少跑 2 个语言候选（若可用）。
                limit = max(whisper_lang_limit, 2 if len(langs_for_provider) > 1 else 1)
                langs_for_provider = langs_for_provider[:limit]
            for lang in langs_for_provider:
                try:
                    if p == "google":
                        text = recognizer.recognize_google(audio, language=lang)
                        if text:
                            if not self._is_text_lang_compatible(text, expected_lang=lang, detected_lang=lang):
                                continue
                            self._google_error_count = 0
                            self._google_backoff_until = 0.0
                            recognized.append((text, lang))
                        continue
                    if p == "whisper_local":
                        text = self._transcribe_with_local_whisper(audio, lang=lang)
                        if text:
                            if not self._is_text_lang_compatible(text, expected_lang=lang, detected_lang=lang):
                                continue
                            recognized.append((text, lang))
                        continue
                    if p == "dashscope_funasr":
                        text = self._transcribe_with_dashscope_funasr(audio, lang=lang)
                        if text:
                            if not self._is_text_lang_compatible(text, expected_lang=lang, detected_lang=lang):
                                continue
                            self._dashscope_error_count = 0
                            self._dashscope_backoff_until = 0.0
                            recognized.append((text, lang))
                        continue
                except Exception as e:
                    msg = str(e)
                    if "UnknownValueError" in type(e).__name__:
                        continue
                    # google 网络错误、whisper 依赖缺失等，尝试下一个 provider/lang
                    if p == "google" and _is_google_network_error(msg):
                        self._google_error_count = min(self._google_error_count + 1, 12)
                        cooldown = min(180, 20 * self._google_error_count)
                        self._google_backoff_until = time.time() + cooldown
                        provider_errors.append(f"google_network_error:{msg}")
                        self._last_provider_error = f"google_network_error:{msg}"
                        self._last_provider_at = int(time.time() * 1000)
                        last_err = f"google_network_error:{msg}"
                        break
                    if p == "dashscope_funasr" and _is_dashscope_network_error(msg):
                        self._dashscope_error_count = min(self._dashscope_error_count + 1, 12)
                        cooldown = min(180, 20 * self._dashscope_error_count)
                        self._dashscope_backoff_until = time.time() + cooldown
                        provider_errors.append(f"dashscope_network_error:{msg}")
                        self._last_provider_error = f"dashscope_network_error:{msg}"
                        self._last_provider_at = int(time.time() * 1000)
                        last_err = f"dashscope_network_error:{msg}"
                        break
                    if p == "whisper_local" and (
                        "no module named" in msg.lower() or "recognize_whisper" in msg.lower()
                    ):
                        # 可选能力缺失，不视为当前轮次失败
                        continue
                    provider_errors.append(f"{p}:{msg}")
                    self._last_provider_error = f"{p}:{msg}"
                    self._last_provider_at = int(time.time() * 1000)
                    last_err = msg
                    continue
            if (not recognized) and p == "whisper_local":
                # Whisper 自动语言检测仅做一次，避免多语言链路重复耗时。
                try:
                    auto_text = self._transcribe_with_local_whisper(audio, lang=None)
                    if auto_text:
                        expected_lang = (langs[0] if langs else None)
                        if self._is_text_lang_compatible(auto_text, expected_lang=expected_lang, detected_lang=None):
                            recognized.append((auto_text, expected_lang))
                except Exception as e:
                    msg = str(e)
                    provider_errors.append(f"whisper_local_auto:{msg}")

            if recognized:
                best_text, best_lang = max(recognized, key=lambda item: _command_score(item[0]))
                best_score = _command_score(best_text)
                fallback_candidates.append((best_score, best_text, best_lang, p))
                # 命令分足够高时立即返回，降低执行延迟；否则继续尝试下一个 provider。
                if best_score >= 3.0:
                    self._last_provider_selected = p
                    self._last_provider_error = None
                    self._last_provider_at = int(time.time() * 1000)
                    return best_text, best_lang, None
        if fallback_candidates:
            _, best_text, best_lang, best_provider = max(fallback_candidates, key=lambda x: x[0])
            self._last_provider_selected = best_provider
            self._last_provider_error = None
            self._last_provider_at = int(time.time() * 1000)
            return best_text, best_lang, None
        if provider_errors:
            self._last_provider_selected = None
            self._last_provider_at = int(time.time() * 1000)
            return None, None, provider_errors[0]
        self._last_provider_selected = None
        self._last_provider_at = int(time.time() * 1000)
        return None, None, locals().get("last_err")

    def _start_python_asr_worker(self, langs):
        sr = self._import_speech_recognition()
        if sr is None:
            self.last_start_reason = "python_asr_dependency_missing"
            self.last_start_diag = {"mode": self.input_mode}
            self._set_local_mic_state("unsupported", "missing_speech_recognition")
            logger.warning("Python ASR 启动失败：缺少 speech_recognition 依赖")
            return False

        if self._local_thread and self._local_thread.is_alive():
            return True

        self._local_stop_event.clear()
        self._local_error = None
        self._local_last_text = ""
        self._local_last_text_lang = None

        timeout_s = max(0.5, float(getattr(settings, "VOICE_PYTHON_LISTEN_TIMEOUT_SECONDS", 2.5)))
        phrase_s = max(0.6, float(getattr(settings, "VOICE_PYTHON_PHRASE_TIME_LIMIT_SECONDS", 4.0)))
        ambient_s = max(0.0, float(getattr(settings, "VOICE_PYTHON_AMBIENT_ADJUST_SECONDS", 0.25)))
        provider_now = self._normalize_provider_name()
        self._sync_capture_mode(provider_now)
        self._local_mic_state["provider"] = provider_now
        dyn_energy = bool(getattr(settings, "VOICE_PYTHON_DYNAMIC_ENERGY", True))
        energy_threshold = int(getattr(settings, "VOICE_PYTHON_ENERGY_THRESHOLD", 280))
        # whisper_local 更依赖“稳定收音”，动态阈值在噪声环境下容易漂移过高导致漏检。
        if provider_now == "whisper_local":
            dyn_energy = False
            energy_threshold = max(70, min(energy_threshold, 180))
        forced_record_seconds = min(5.5, max(2.2, phrase_s))

        def _worker():
            recognizer = sr.Recognizer()
            recognizer.dynamic_energy_threshold = dyn_energy
            if energy_threshold > 0:
                recognizer.energy_threshold = energy_threshold

            try:
                self._local_running = True
                self._local_last_start_at = int(time.time() * 1000)
                while not self._local_stop_event.is_set():
                    mic, selected_idx = self._select_local_microphone(sr)
                    with mic as source:
                        if ambient_s > 0:
                            try:
                                recognizer.adjust_for_ambient_noise(source, duration=ambient_s)
                            except Exception:
                                pass
                        logger.info(
                            "Python ASR recorder params: "
                            f"capture_mode={self._local_capture_mode}, provider={provider_now}, "
                            f"dyn_energy={recognizer.dynamic_energy_threshold}, "
                            f"energy_threshold={recognizer.energy_threshold}, timeout={timeout_s}, "
                            f"phrase_time_limit={phrase_s}, forced_record={forced_record_seconds}"
                        )
                        self._set_local_mic_state("granted", None)
                        self._local_mic_state["deviceIndex"] = selected_idx
                        names = self._list_local_microphones(sr)
                        self._local_mic_state["deviceName"] = (
                            names[selected_idx] if (selected_idx is not None and 0 <= selected_idx < len(names)) else None
                        )
                        self._local_mic_state["captureMode"] = self._local_capture_mode
                        while not self._local_stop_event.is_set():
                            try:
                                # 连续无文本时，改为固定窗口录音，避免只抓到噪声片段。
                                if self._local_no_text_count >= 3:
                                    audio = recognizer.record(source, duration=forced_record_seconds)
                                else:
                                    audio = recognizer.listen(
                                        source,
                                        timeout=timeout_s,
                                        phrase_time_limit=phrase_s,
                                    )
                                self._local_last_audio_at = int(time.time() * 1000)
                                self._local_last_audio_rms = self._compute_audio_rms(audio)
                            except sr.WaitTimeoutError:
                                continue
                            except Exception as e:
                                msg = str(e or "")
                                self._local_error = f"listen_error: {msg}"
                                if "audio source must be entered before listening" in msg.lower():
                                    logger.warning("检测到音频源上下文失效，正在重建输入流。")
                                    time.sleep(0.2)
                                    break
                                time.sleep(0.15)
                                continue

                            text, lang, err = self._recognize_with_provider(recognizer, audio, langs)
                            if text:
                                bad, reason = self._is_low_quality_transcript(text, lang=lang)
                                if bad:
                                    self._local_no_text_count += 1
                                    if self._local_no_text_count >= 3:
                                        if self._should_report_no_text_error():
                                            self._local_error = (
                                                f"no_text:rms={self._local_last_audio_rms},provider="
                                                f"{getattr(settings, 'VOICE_PYTHON_ASR_PROVIDER', 'unknown')},"
                                                f"filtered={reason}"
                                            )
                                        else:
                                            self._local_error = None
                                    logger.debug(f"Python ASR filtered low-quality text: reason={reason}, text={text[:120]}")
                                    continue
                                self._local_push_text(text, lang=lang)
                                self._local_error = None
                                self._local_no_text_count = 0
                            elif err:
                                self._local_error = f"asr_error: {err}"
                                self._local_no_text_count += 1
                            else:
                                # 避免静默失败：持续有音频但无文本时，显式标记 no_text。
                                self._local_no_text_count += 1
                                if self._local_no_text_count >= 3:
                                    if self._should_report_no_text_error():
                                        self._local_error = (
                                            f"no_text:rms={self._local_last_audio_rms},provider="
                                            f"{getattr(settings, 'VOICE_PYTHON_ASR_PROVIDER', 'unknown')}"
                                        )
                                    else:
                                        self._local_error = None
            except Exception as e:
                msg = str(e)
                self._local_error = f"mic_error: {msg}"
                lower = msg.lower()
                if "permission" in lower or "not permitted" in lower or "denied" in lower:
                    self._set_local_mic_state("denied", msg)
                elif "pyaudio" in lower:
                    self._set_local_mic_state("unsupported", "missing_pyaudio")
                else:
                    self._set_local_mic_state("error", msg)
                logger.warning(f"Python ASR worker exited with mic error: {msg}")
            finally:
                self._local_running = False

        self._local_thread = threading.Thread(target=_worker, daemon=True)
        self._local_thread.start()

        # 后台预热 whisper，降低首条命令的识别延迟（不阻塞启动）。
        if provider_now in {"whisper_local", "auto"}:
            threading.Thread(target=self.prewarm_local_asr, daemon=True).start()

        time.sleep(0.08)
        ok = bool(self._local_thread and self._local_thread.is_alive())
        if ok:
            self.last_lang = langs[0] if langs else self.last_lang
            self.last_langs = list(langs or [])
            self.last_start_reason = None
            self.last_start_errors = []
            self.last_start_diag = {
                "mode": self.input_mode,
                "provider": provider_now,
                "captureMode": self._local_capture_mode,
            }
            self.last_start_success_at = time.time()
            logger.info(
                "Python ASR 语音监听已启动: "
                f"capture_mode={self._local_capture_mode}, provider={provider_now}, langs={langs}"
            )
            return True
        self.last_start_reason = self._local_error or "python_asr_start_failed"
        return False

    def _iter_contexts(self, prefer_ctx_name=None):
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

        if prefer_ctx_name:
            contexts.sort(key=lambda x: 0 if x[0] == prefer_ctx_name else 1)
        return contexts

    def _run_js_in_contexts(self, script, *args, prefer_ctx_name=None, require_truthy=False, return_errors=False):
        errors = []
        for ctx_name, ctx in self._iter_contexts(prefer_ctx_name=prefer_ctx_name):
            try:
                result = ctx.run_js(script, *args)
                if require_truthy and not result:
                    continue
                if return_errors:
                    return ctx_name, result, errors
                return ctx_name, result
            except Exception as e:
                logger.debug(f"VoiceAgent JS 执行失败({ctx_name}): {type(e).__name__}: {repr(e)}")
                errors.append(f"{ctx_name}:{type(e).__name__}:{str(e)[:160]}")
                continue
        if return_errors:
            return None, None, errors
        return None, None

    def _run_js_with_reconnect(self, script, *args, prefer_ctx_name=None):
        """
        JS 执行鲁棒封装：
        1) 先在当前上下文执行
        2) 若无结果，强制重连后重试一次
        """
        ctx_name, result, errors = self._run_js_in_contexts(
            script,
            *args,
            prefer_ctx_name=prefer_ctx_name,
            return_errors=True,
        )
        if result is not None:
            return ctx_name, result, errors

        try:
            self.vision_agent.ensure_connection(force=True)
        except Exception:
            pass

        ctx2, result2, errors2 = self._run_js_in_contexts(
            script,
            *args,
            prefer_ctx_name=prefer_ctx_name,
            return_errors=True,
        )
        return ctx2, result2, errors + errors2

    def _is_valid_voice_page(self, diag):
        url = str((diag or {}).get("url") or "").lower()
        title = str((diag or {}).get("title") or "").lower()
        if "tiktok.com" not in url:
            return False
        if "/@".lower() in url and "/live" in url:
            return True
        if "正在直播" in title and "tiktok" in title:
            return True
        return False

    def _dedupe_langs(self, primary, fallback_languages):
        langs = []
        for lang in [primary] + list(fallback_languages or []):
            if not lang:
                continue
            lang = str(lang).strip()
            if not lang or lang in langs:
                continue
            langs.append(lang)
        return langs or ["zh-CN"]

    def start(self, language="zh-CN", fallback_languages=None, silence_restart_seconds=18):
        """在当前页面注入并启动语音识别。"""
        self.last_start_attempt_at = time.time()
        langs = self._dedupe_langs(language, fallback_languages)
        if self._is_tab_media_asr_mode():
            return self._start_tab_audio_stream_capture(langs)
        if self._is_python_asr_mode():
            return self._start_python_asr_worker(langs)

        if not self.vision_agent.ensure_connection():
            return False
        diag = self.diagnose_voice_capability()
        if not self._is_valid_voice_page(diag):
            self.enabled_in_page = False
            self.last_start_reason = "wrong_page"
            self.last_start_errors = []
            self.last_start_diag = {
                "ctx": diag.get("ctx"),
                "title": diag.get("title"),
                "url": diag.get("url"),
            }
            logger.warning(f"语音口令监听启动跳过：当前页面不是直播间。title={diag.get('title')} url={diag.get('url')}")
            return False

        silence_ms = max(4000, int(float(silence_restart_seconds) * 1000))

        script = """
        return ((cfg) => {
          cfg = cfg || {};
          const langs = Array.isArray(cfg.langs) ? cfg.langs.filter(Boolean) : ['zh-CN'];
          const silenceMs = Number(cfg.silenceMs || 18000);
          const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

          window.__liveAssistantVoiceQueue = window.__liveAssistantVoiceQueue || [];
          window.__liveAssistantVoiceState = window.__liveAssistantVoiceState || {};
          const queue = window.__liveAssistantVoiceQueue;
          const state = window.__liveAssistantVoiceState;

          if (!SR) {
            state.supported = false;
            state.error = 'SpeechRecognitionUnsupported';
            return { ok: false, reason: 'unsupported' };
          }

          const prev = window.__liveAssistantVoiceController;
          if (prev && prev.recognition) {
            try {
              prev.shouldRun = false;
              prev.recognition.onstart = null;
              prev.recognition.onresult = null;
              prev.recognition.onerror = null;
              prev.recognition.onend = null;
              prev.recognition.stop();
            } catch (e) {}
          }

          const recognition = new SR();
          recognition.continuous = true;
          recognition.interimResults = false;
          recognition.maxAlternatives = 1;

          const controller = {
            recognition,
            shouldRun: true,
            langs,
            idx: 0,
            silenceMs
          };
          window.__liveAssistantVoiceController = controller;

          const currentLang = () => {
            const lang = controller.langs[controller.idx] || controller.langs[0] || 'zh-CN';
            return lang;
          };

          const startOnce = () => {
            recognition.lang = currentLang();
            state.lang = recognition.lang;
            state.langs = controller.langs;
            recognition.start();
          };

          recognition.onstart = () => {
            state.supported = true;
            state.running = true;
            state.error = null;
            state.startedAt = Date.now();
            state.lastStartAt = Date.now();
            state.lang = recognition.lang;
          };

          recognition.onresult = (event) => {
            const now = Date.now();
            for (let i = event.resultIndex; i < event.results.length; i++) {
              const r = event.results[i];
              if (!r || !r.isFinal) continue;
              const alt = r[0] || {};
              const text = String(alt.transcript || '').trim();
              if (!text) continue;
              queue.push({
                source: 'mic',
                text,
                confidence: typeof alt.confidence === 'number' ? alt.confidence : null,
                lang: recognition.lang,
                ts: now
              });
            }
            state.lastResultAt = now;
            if (queue.length > 120) queue.splice(0, queue.length - 120);
          };

          recognition.onerror = (ev) => {
            const err = (ev && ev.error) || 'unknown';
            state.error = err;
            state.lastErrorAt = Date.now();

            if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(err)) {
              controller.shouldRun = false;
              return;
            }

            if (['no-speech', 'language-not-supported'].includes(err) && controller.langs.length > 1) {
              controller.idx = (controller.idx + 1) % controller.langs.length;
            }
          };

          recognition.onend = () => {
            state.running = false;
            state.lastEndAt = Date.now();
            if (!controller.shouldRun) return;

            const now = Date.now();
            const lastResultAt = Number(state.lastResultAt || 0);
            if (controller.langs.length > 1 && now - lastResultAt > controller.silenceMs) {
              controller.idx = (controller.idx + 1) % controller.langs.length;
            }

            setTimeout(() => {
              try {
                startOnce();
              } catch (e) {
                state.error = String(e);
                state.lastErrorAt = Date.now();
              }
            }, 120);
          };

          try {
            startOnce();
            return { ok: true, lang: currentLang(), langs: controller.langs, silenceMs };
          } catch (e) {
            state.error = String(e);
            state.lastErrorAt = Date.now();
            return { ok: false, reason: String(e) };
          }
        })(arguments[0] || {});
        """

        ctx_name, result, errors = self._run_js_in_contexts(
            script,
            {"langs": langs, "silenceMs": silence_ms},
            prefer_ctx_name=self.active_ctx_name,
            return_errors=True,
        )

        ok = bool(result and isinstance(result, dict) and result.get("ok"))
        if ok:
            self.enabled_in_page = True
            self.active_ctx_name = ctx_name
            self.last_lang = language
            self.last_langs = langs
            self.last_start_success_at = time.time()
            self.last_start_reason = None
            self.last_start_errors = []
            self.last_start_diag = {}
            self.permission_blocked = False
            logger.info(f"语音口令监听已启动: ctx={ctx_name}, langs={langs}")
            return True

        self.enabled_in_page = False
        reason = result.get("reason") if isinstance(result, dict) else "js_no_result"
        diag = self.diagnose_voice_capability()
        diag_brief = {
            "ctx": diag.get("ctx"),
            "secure": diag.get("secureContext"),
            "sr": diag.get("speechRecognition"),
            "media": diag.get("mediaDevices"),
            "title": diag.get("title"),
            "url": diag.get("url"),
        }
        self.last_start_reason = reason
        self.last_start_errors = errors[:5]
        self.last_start_diag = diag_brief
        reason_lower = str(reason or "").lower()
        if any(k in reason_lower for k in ["not-allowed", "service-not-allowed", "audio-capture"]):
            self.permission_blocked = True
        logger.warning(
            f"语音口令监听启动失败: reason={reason}, ctx={ctx_name}, diag={diag_brief}, ctx_errors={self.last_start_errors[:2]}"
        )
        return False

    def _compute_retry_cooldown_seconds(self):
        reason = str(self.last_start_reason or "").lower()
        if not reason:
            return 0.0
        if any(k in reason for k in ["not-allowed", "service-not-allowed", "audio-capture", "needs_in_tab_click"]):
            return 12.0
        if any(k in reason for k in ["wrong_page", "unsupported_context", "unsupported"]):
            return 6.0
        return 2.0

    def stop(self):
        """停止页面内语音识别。"""
        if self._tab_audio_running:
            self._stop_tab_audio_stream_capture()
            self._tab_audio_running = False
            self._tab_audio_buffer = bytearray()
            self._tab_audio_pending_text = ""
            self._tab_audio_pending_lang = None
            self._tab_audio_pending_started_at = None
            self._tab_audio_pending_updated_at = None
            return True
        if self._is_python_asr_mode():
            self._local_stop_event.set()
            if self._local_thread and self._local_thread.is_alive():
                self._local_thread.join(timeout=1.2)
            self._local_running = False
            return True

        script = """
        return (() => {
          const controller = window.__liveAssistantVoiceController;
          if (!controller || !controller.recognition) return {ok:true, reason:'not-running'};
          controller.shouldRun = false;
          try { controller.recognition.stop(); } catch (e) {}
          const state = window.__liveAssistantVoiceState || {};
          state.running = false;
          return {ok:true};
        })();
        """
        _, result = self._run_js_in_contexts(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        self.enabled_in_page = False
        return bool(result and isinstance(result, dict) and result.get("ok"))

    def ensure_started(self, language="zh-CN", fallback_languages=None, silence_restart_seconds=18):
        """确保语音识别已开启且语言配置正确。"""
        if self._is_tab_media_asr_mode():
            expected_langs = self._dedupe_langs(language, fallback_languages)
            if self._tab_audio_running and expected_langs == self.last_langs:
                return True
            return self.start(
                language=language,
                fallback_languages=fallback_languages,
                silence_restart_seconds=silence_restart_seconds,
            )
        if self._is_python_asr_mode():
            expected_langs = self._dedupe_langs(language, fallback_languages)
            expected_provider = self._normalize_provider_name()
            if self._tab_audio_running:
                self.last_lang = expected_langs[0] if expected_langs else language
                self.last_langs = expected_langs
                return True
            current_provider = str(self._local_mic_state.get("provider") or "").strip().lower()
            provider_changed = bool(current_provider and current_provider != expected_provider)
            if self._local_running and expected_langs == self.last_langs and not provider_changed:
                return True
            if self._local_running:
                self.stop()
                time.sleep(0.08)
            return self.start(
                language=language,
                fallback_languages=fallback_languages,
                silence_restart_seconds=silence_restart_seconds,
            )

        if not self.vision_agent.ensure_connection():
            self.enabled_in_page = False
            return False

        # 如果曾被浏览器明确拒绝，暂停自动重试，避免反复触发 not-allowed。
        # 仅当手动权限申请成功后再恢复自动启动。
        if self.permission_blocked:
            perm = self.get_microphone_permission_state()
            if str((perm or {}).get("status") or "") == "granted":
                self.permission_blocked = False
            else:
                return False

        expected_langs = self._dedupe_langs(language, fallback_languages)
        state = self.get_state()
        running = bool(state.get("running"))
        current_langs = state.get("langs") if isinstance(state.get("langs"), list) else []

        if (not running) or (expected_langs != current_langs):
            cooldown = self._compute_retry_cooldown_seconds()
            if (
                cooldown > 0
                and self.last_start_attempt_at > 0
                and time.time() - self.last_start_attempt_at < cooldown
            ):
                return False
            return self.start(
                language=language,
                fallback_languages=fallback_languages,
                silence_restart_seconds=silence_restart_seconds,
            )
        return True

    def get_state(self):
        if self._is_python_asr_mode():
            configured_provider = self._normalize_provider_name()
            self._sync_capture_mode(configured_provider)
            tab_active = bool(self._tab_audio_running)
            if self._is_tab_media_asr_mode():
                tab_active = True
            runtime_provider = self._last_provider_selected or self._last_provider_attempt or configured_provider
            cloud_only = bool(tab_active) or self._is_dashscope_force_loopback(configured_provider)
            device_name = (
                "browser_media_stream"
                if tab_active
                else str(self._local_mic_state.get("deviceName") or "")
            )
            loopback_likely_mic = bool(
                (not tab_active)
                and self._is_loopback_asr_mode()
                and device_name
                and any(tok in device_name.lower() for tok in ["microphone", "mic", "麦克风", "built-in", "array"])
            )
            last_result_at = self._tab_audio_last_result_at if tab_active else self._local_last_result_at
            last_text = self._tab_audio_last_text if tab_active else self._local_last_text
            last_text_lang = self._tab_audio_last_text_lang if tab_active else self._local_last_text_lang
            last_audio_at = self._tab_audio_last_audio_at if tab_active else self._local_last_audio_at
            last_audio_rms = self._tab_audio_last_audio_rms if tab_active else self._local_last_audio_rms
            no_text_count = self._tab_audio_no_text_count if tab_active else self._local_no_text_count
            runtime_error = self._last_provider_error or (self._tab_audio_error if tab_active else self._local_error)
            return {
                "supported": True,
                "running": bool(self._local_running or tab_active),
                "error": runtime_error,
                "lang": (self.last_langs[0] if self.last_langs else self.last_lang),
                "langs": list(self.last_langs or []),
                "lastResultAt": last_result_at,
                "lastErrorAt": int(time.time() * 1000) if runtime_error else None,
                "lastText": last_text,
                "lastTextLang": last_text_lang,
                "ctx": "python_asr",
                "mode": self.input_mode,
                "provider": configured_provider,
                "runtimeProvider": runtime_provider,
                "runtimeProviderType": self._provider_runtime_kind(runtime_provider),
                "runtimeProviderChain": list(self._last_provider_chain or []),
                "runtimeProviderError": runtime_error,
                "runtimeProviderAt": self._last_provider_at,
                "usingCloudAsr": self._provider_runtime_kind(runtime_provider) == "cloud",
                "cloudOnly": bool(cloud_only),
                "loopbackLikelyMic": loopback_likely_mic,
                "deviceIndex": self._local_mic_state.get("deviceIndex"),
                "deviceName": device_name,
                "captureMode": ("tab_media_stream" if tab_active else self._local_capture_mode),
                "source": ("tab_audio_stream" if tab_active else ("python_loopback" if self._is_loopback_asr_mode() else "python_mic")),
                "lastAudioAt": last_audio_at,
                "lastAudioRms": last_audio_rms,
                "noTextCount": no_text_count,
            }

        script = """
        return (() => {
          const st = window.__liveAssistantVoiceState || {};
          return {
            supported: !!st.supported,
            running: !!st.running,
            error: st.error || null,
            lang: st.lang || null,
            langs: Array.isArray(st.langs) ? st.langs : [],
            lastResultAt: st.lastResultAt || null,
            lastErrorAt: st.lastErrorAt || null
          };
        })();
        """
        ctx_name, state = self._run_js_in_contexts(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        if ctx_name:
            self.active_ctx_name = ctx_name
        if not isinstance(state, dict):
            return {}
        state["ctx"] = ctx_name
        return state

    def poll_transcripts(self):
        """拉取并清空页面内已识别语音文本队列。"""
        if self._is_python_asr_mode():
            with self._local_lock:
                items = list(self._local_queue)
                self._local_queue.clear()
            return items

        script = """
        return (() => {
          const queue = window.__liveAssistantVoiceQueue || [];
          const items = queue.splice(0, queue.length);
          return {items};
        })();
        """
        ctx_name, data = self._run_js_in_contexts(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        if ctx_name:
            self.active_ctx_name = ctx_name

        if not isinstance(data, dict):
            return []
        items = data.get("items") or []
        cleaned = []
        now_ms = int(time.time() * 1000)
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            item_lang = item.get("lang")
            if not self._is_runtime_lang_compatible(text, detected_lang=item_lang):
                continue
            cleaned.append(
                {
                    "source": item.get("source") or "mic",
                    "text": text,
                    "confidence": item.get("confidence"),
                    "lang": item_lang,
                    "ts": item.get("ts") or now_ms,
                }
            )
        return cleaned

    def _normalize(self, text):
        text = (text or "").strip().lower()
        return "".join(ch for ch in text if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"))

    def _prune_subtitle_seen(self, ttl=12):
        now = time.time()
        expired = [k for k, ts in self._subtitle_seen.items() if now - ts > ttl]
        for k in expired:
            self._subtitle_seen.pop(k, None)

    def poll_subtitle_transcripts(self, max_items=4):
        """从页面可能的字幕区域提取文本（兜底通道）。"""
        script = """
        return ((maxItems) => {
          maxItems = Number(maxItems || 4);
          const selectors = [
            '[data-e2e*="caption"]',
            '[data-e2e*="subtitle"]',
            '[class*="caption"]',
            '[class*="subtitle"]',
            '[aria-live="polite"]',
            '[aria-live="assertive"]'
          ];
          const isVisible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width < 20 || r.height < 10) return false;
            const s = window.getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
          };
          const clean = (t) => String(t || '').replace(/\\s+/g, ' ').trim();
          const badWords = ['观众', '已关注主播', 'joined', 'sent', 'followed', 'gift', '点赞', '分享'];
          const nodes = [];
          const seen = new Set();
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
              if (seen.has(el)) continue;
              seen.add(el);
              if (!isVisible(el)) continue;
              const r = el.getBoundingClientRect();
              const text = clean(el.innerText || el.textContent || '');
              if (!text || text.length < 2 || text.length > 120) continue;
              const lower = text.toLowerCase();
              if (badWords.some(w => lower.includes(w))) continue;
              // 更偏向视频区域，尽量排除右侧聊天栏
              if (r.left > window.innerWidth * 0.82) continue;
              nodes.push({ text, ts: Date.now(), source: 'subtitle' });
            }
          }
          return { items: nodes.slice(-maxItems) };
        })(arguments[0] || 4);
        """
        _, data = self._run_js_in_contexts(
            script,
            int(max_items),
            prefer_ctx_name=self.active_ctx_name,
        )
        if not isinstance(data, dict):
            return []

        items = data.get("items") or []
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            norm = self._normalize(text)
            if not norm:
                continue
            if not self._is_runtime_lang_compatible(text, detected_lang=item.get("lang")):
                continue

            now = time.time()
            last = self._subtitle_seen.get(norm)
            self._subtitle_seen[norm] = now
            if last and now - last < 6:
                continue

            cleaned.append(
                {
                    "source": "subtitle",
                    "text": text,
                    "confidence": None,
                    "lang": None,
                    "ts": item.get("ts") or int(now * 1000),
                }
            )
        self._prune_subtitle_seen()
        return cleaned

    def collect_command_candidates(self, include_subtitle=True):
        items = []
        if self._tab_audio_running:
            items.extend(self.poll_tab_audio_stream_transcripts())
        items.extend(self.poll_transcripts())
        if include_subtitle and (not self._is_python_asr_mode()):
            items.extend(self.poll_subtitle_transcripts())
        items.sort(key=lambda x: x.get("ts") or 0)
        deduped = []
        last_key = ""
        last_ts = 0
        for item in items:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            source = str(item.get("source") or "")
            lang = str(item.get("lang") or "")
            try:
                ts = int(item.get("ts") or 0)
            except Exception:
                ts = 0
            norm = self._normalize(text)
            key = f"{source}|{lang}|{norm}"
            if key and key == last_key and (ts - last_ts) <= 1200:
                continue
            deduped.append(item)
            last_key = key
            last_ts = ts
        return deduped

    def get_start_failure_info(self):
        return {
            "reason": self.last_start_reason,
            "errors": list(self.last_start_errors),
            "diag": dict(self.last_start_diag or {}),
        }

    def diagnose_voice_capability(self):
        if self._is_tab_media_asr_mode():
            if not self.vision_agent.ensure_connection():
                return {
                    "ctx": "tab_media_asr",
                    "mode": self.input_mode,
                    "provider": self._normalize_provider_name(),
                    "captureMode": "tab_media_stream",
                    "secureContext": False,
                    "title": "",
                    "url": "",
                    "mediaElements": 0,
                    "audioTrackReady": False,
                    "error": "browser_not_connected",
                }
            script = """
            return (() => {
              const medias = Array.from(document.querySelectorAll('video,audio'));
              let audioTrackReady = false;
              for (const m of medias) {
                try {
                  let s = null;
                  if (typeof m.captureStream === 'function') s = m.captureStream();
                  else if (typeof m.mozCaptureStream === 'function') s = m.mozCaptureStream();
                  if (s && s.getAudioTracks && s.getAudioTracks().length > 0) {
                    audioTrackReady = true;
                    break;
                  }
                } catch (e) {}
              }
              return {
                title: document.title || '',
                url: location.href || '',
                secureContext: !!window.isSecureContext,
                mediaElements: medias.length,
                audioTrackReady,
              };
            })();
            """
            ctx_name, data = self._run_js_in_contexts(
                script,
                prefer_ctx_name=self.active_ctx_name,
            )
            if ctx_name:
                self.active_ctx_name = ctx_name
            if not isinstance(data, dict):
                data = {}
            data.update(
                {
                    "ctx": ctx_name or "tab_media_asr",
                    "mode": self.input_mode,
                    "provider": self._normalize_provider_name(),
                    "captureMode": "tab_media_stream",
                    "cloudOnly": False,
                }
            )
            return data
        if self._is_python_asr_mode():
            provider_now = self._normalize_provider_name()
            self._sync_capture_mode(provider_now)
            sr = self._import_speech_recognition()
            return {
                "ctx": "python_asr",
                "mode": self.input_mode,
                "provider": provider_now,
                "captureMode": self._local_capture_mode,
                "cloudOnly": bool(self._is_dashscope_force_loopback(provider_now)),
                "speechRecognition": bool(sr),
                "mediaDevices": bool(sr),
                "secureContext": True,
                "title": "",
                "url": "",
                "visibility": "python-process",
                "userAgent": "python",
            }

        script = """
        return (() => {
          const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
          return {
            title: document.title || '',
            url: location.href || '',
            secureContext: !!window.isSecureContext,
            speechRecognition: !!SR,
            mediaDevices: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
            permissionsApi: !!(navigator.permissions && navigator.permissions.query),
            visibility: document.visibilityState || '',
            userAgent: navigator.userAgent || ''
          };
        })();
        """
        ctx_name, data = self._run_js_in_contexts(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        if ctx_name:
            self.active_ctx_name = ctx_name
        if not isinstance(data, dict):
            return {"ctx": ctx_name}
        data["ctx"] = ctx_name
        return data

    def request_microphone_permission(self):
        """
        触发浏览器麦克风权限申请（会弹出授权窗口）。
        返回当前状态：requesting/granted/denied/unsupported。
        """
        if self._is_tab_media_asr_mode():
            if not self.vision_agent.ensure_connection():
                return {"status": "no_page", "error": "browser_not_connected", "mode": self.input_mode}
            diag = self.diagnose_voice_capability() or {}
            if not self._is_valid_voice_page(diag):
                return {
                    "status": "wrong_page",
                    "error": "not_in_tiktok_live_room",
                    "mode": self.input_mode,
                    "page_title": diag.get("title"),
                    "page_url": diag.get("url"),
                }
            return {
                "status": "granted",
                "error": None,
                "mode": self.input_mode,
                "provider": self._normalize_provider_name(),
                "captureMode": "tab_media_stream",
                "cloudOnly": False,
            }
        if self._is_python_asr_mode():
            provider_now = self._normalize_provider_name()
            self._sync_capture_mode(provider_now)
            sr = self._import_speech_recognition()
            if sr is None:
                self._set_local_mic_state("unsupported", "missing_speech_recognition")
                return {
                    "status": "unsupported",
                    "error": "missing_speech_recognition",
                    "mode": self.input_mode,
                }
            try:
                recognizer = sr.Recognizer()
                mic, selected_idx = self._select_local_microphone(sr)
                with mic as source:
                    ambient_s = max(0.0, float(getattr(settings, "VOICE_PYTHON_AMBIENT_ADJUST_SECONDS", 0.25)))
                    if ambient_s > 0:
                        try:
                            recognizer.adjust_for_ambient_noise(source, duration=ambient_s)
                        except Exception:
                            pass
                names = self._list_local_microphones(sr)
                device_name = names[selected_idx] if (selected_idx is not None and 0 <= selected_idx < len(names)) else None
                self.permission_blocked = False
                self._set_local_mic_state("granted", None)
                self._local_mic_state["deviceIndex"] = selected_idx
                self._local_mic_state["deviceName"] = device_name
                return {
                    "status": "granted",
                    "error": None,
                    "mode": self.input_mode,
                    "provider": provider_now,
                    "captureMode": self._local_capture_mode,
                    "cloudOnly": bool(self._is_dashscope_force_loopback(provider_now)),
                    "deviceIndex": selected_idx,
                    "deviceName": device_name,
                }
            except Exception as e:
                msg = str(e)
                lower = msg.lower()
                if "pyaudio" in lower:
                    self._set_local_mic_state("unsupported", "missing_pyaudio")
                    return {
                        "status": "unsupported",
                        "error": "missing_pyaudio",
                        "mode": self.input_mode,
                    }
                if "permission" in lower or "not permitted" in lower or "denied" in lower:
                    self.permission_blocked = True
                    self._set_local_mic_state("denied", msg)
                    return {
                        "status": "denied",
                        "error": msg,
                        "mode": self.input_mode,
                    }
                self._set_local_mic_state("error", msg)
                return {
                    "status": "error",
                    "error": msg,
                    "mode": self.input_mode,
                }

        if not self.vision_agent.ensure_connection():
            return {"status": "no_page", "error": "browser_not_connected"}

        diag = self.diagnose_voice_capability()
        if not self._is_valid_voice_page(diag):
            return {
                "status": "wrong_page",
                "error": "not_in_tiktok_live_room",
                "page_title": diag.get("title"),
                "page_url": diag.get("url"),
            }
        if not diag.get("secureContext"):
            return {
                "status": "unsupported_context",
                "error": "insecure_context",
                "page_title": diag.get("title"),
                "page_url": diag.get("url"),
            }
        if not diag.get("mediaDevices"):
            return {
                "status": "unsupported",
                "error": "getUserMediaUnavailable",
                "page_title": diag.get("title"),
                "page_url": diag.get("url"),
            }

        script = """
        return (() => {
          window.__liveAssistantMicPerm = window.__liveAssistantMicPerm || {
            status: 'idle',
            error: null,
            updatedAt: Date.now()
          };
          const state = window.__liveAssistantMicPerm;
          const startAt = Date.now();

          const setState = (status, error=null) => {
            state.status = status;
            state.error = error;
            state.updatedAt = Date.now();
            return state;
          };

          const doRequest = (fromGesture=false) => {
            setState('requesting', null);
            return navigator.mediaDevices.getUserMedia({ audio: true })
              .then((stream) => {
                setState('granted', null);
                try { stream.getTracks().forEach(t => t.stop()); } catch (e) {}
                return state;
              })
              .catch((err) => {
                const errName = String((err && (err.name || err.message)) || err || 'unknown');
                const fastReject = (Date.now() - startAt) < 800;
                const hasPermApi = !!(navigator.permissions && navigator.permissions.query);
                if (hasPermApi) {
                  return navigator.permissions.query({ name: 'microphone' }).then((perm) => {
                    const pState = String((perm && perm.state) || '');
                    state.permissionState = pState;
                    // 站点已被明确拒绝：不要误报“点按钮重试”
                    if (pState === 'denied') {
                      setState('denied', errName);
                      return state;
                    }
                    if (errName.includes('NotAllowedError') && fastReject && !fromGesture) {
                      setState('needs_in_tab_click', errName);
                    } else {
                      setState('denied', errName);
                    }
                    return state;
                  }).catch(() => {
                    if (errName.includes('NotAllowedError') && fastReject && !fromGesture) {
                      setState('needs_in_tab_click', errName);
                    } else {
                      setState('denied', errName);
                    }
                    return state;
                  });
                }
                if (errName.includes('NotAllowedError') && fastReject && !fromGesture) {
                  setState('needs_in_tab_click', errName);
                } else {
                  setState('denied', errName);
                }
                return state;
              });
          };

          if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
            return setState('unsupported', 'getUserMediaUnavailable');
          }

          // 在直播页注入一个“用户手势按钮”，用于真实触发权限弹窗。
          const btnId = '__liveAssistantMicBtn';
          let btn = document.getElementById(btnId);
          if (!btn) {
            btn = document.createElement('button');
            btn.id = btnId;
            btn.innerText = 'Enable Mic';
            btn.style.position = 'fixed';
            btn.style.zIndex = '2147483647';
            btn.style.top = '16px';
            btn.style.right = '16px';
            btn.style.padding = '8px 12px';
            btn.style.borderRadius = '8px';
            btn.style.border = '1px solid #ddd';
            btn.style.background = '#111';
            btn.style.color = '#fff';
            btn.style.cursor = 'pointer';
            btn.onclick = () => { doRequest(true); };
            document.body.appendChild(btn);
          }

          doRequest(false);
          state.helperButton = true;
          state.helperButtonText = 'Enable Mic';
          return state;
        })();
        """
        _, result, errors = self._run_js_with_reconnect(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        if isinstance(result, dict):
            status = str(result.get("status") or "")
            if status == "granted":
                self.permission_blocked = False
            elif status in ("denied", "not-allowed"):
                self.permission_blocked = True
            return result
        page = self.vision_agent.page
        page_title = ""
        page_url = ""
        try:
            page_title = page.title if page else ""
            page_url = page.url if page else ""
        except Exception:
            pass
        return {
            "status": "unknown",
            "error": "js_no_result",
            "details": errors[:3],
            "diag": {
                "ctx": diag.get("ctx"),
                "secure": diag.get("secureContext"),
                "sr": diag.get("speechRecognition"),
                "media": diag.get("mediaDevices"),
            },
            "page_title": page_title,
            "page_url": page_url,
        }

    def get_microphone_permission_state(self):
        """读取最近一次麦克风权限申请状态。"""
        if self._is_tab_media_asr_mode():
            diag = self.diagnose_voice_capability() or {}
            return {
                "status": "granted" if self._is_valid_voice_page(diag) else "wrong_page",
                "error": None if self._is_valid_voice_page(diag) else "not_in_tiktok_live_room",
                "mode": self.input_mode,
                "provider": self._normalize_provider_name(),
                "captureMode": "tab_media_stream",
                "cloudOnly": False,
                "page_title": diag.get("title"),
                "page_url": diag.get("url"),
                "updatedAt": int(time.time() * 1000),
            }
        if self._is_python_asr_mode():
            provider_now = self._normalize_provider_name()
            self._sync_capture_mode(provider_now)
            state = dict(self._local_mic_state)
            state.setdefault("mode", self.input_mode)
            state.setdefault("provider", provider_now)
            state.setdefault("captureMode", self._local_capture_mode)
            state.setdefault("cloudOnly", bool(self._is_dashscope_force_loopback(provider_now)))
            if self._local_running:
                state["status"] = "granted"
                state["error"] = None
            if "deviceIndex" not in state:
                state["deviceIndex"] = (self._preferred_mic_device_index if self._preferred_mic_device_index >= 0 else None)
            state.setdefault("nameHint", self._preferred_mic_name_hint)
            return state

        if not self.vision_agent.ensure_connection():
            return {"status": "no_page", "error": "browser_not_connected", "updatedAt": None}

        script = """
        return (() => {
          const st = window.__liveAssistantMicPerm || null;
          if (!st) return { status: 'idle', error: null, updatedAt: null };
          return {
            status: st.status || 'idle',
            error: st.error || null,
            updatedAt: st.updatedAt || null,
            permissionState: st.permissionState || null,
            helperButton: !!st.helperButton,
            helperButtonText: st.helperButtonText || null
          };
        })();
        """
        _, result, errors = self._run_js_with_reconnect(
            script,
            prefer_ctx_name=self.active_ctx_name,
        )
        if isinstance(result, dict):
            diag = self.diagnose_voice_capability()
            result.setdefault("page_title", diag.get("title"))
            result.setdefault("page_url", diag.get("url"))
            return result
        page = self.vision_agent.page
        page_title = ""
        page_url = ""
        try:
            page_title = page.title if page else ""
            page_url = page.url if page else ""
        except Exception:
            pass
        return {
            "status": "unknown",
            "error": "js_no_result",
            "updatedAt": None,
            "details": errors[:3],
            "diag": self.diagnose_voice_capability(),
            "page_title": page_title,
            "page_url": page_url,
        }
