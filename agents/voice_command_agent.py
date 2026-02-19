import threading
import time
import re
import math
import struct
from pathlib import Path
from collections import deque
import numpy as np
from utils.logger import logger
import config.settings as settings


class VoiceCommandAgent:
    """
    语音口令监听器（多通道）：
    1) 浏览器 Web Speech API 麦克风识别
    2) 页面字幕文本兜底提取
    """

    def __init__(self, vision_agent):
        self.vision_agent = vision_agent
        self.input_mode = str(getattr(settings, "VOICE_COMMAND_INPUT_MODE", "web_speech") or "web_speech").strip().lower()
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
        self._preferred_mic_device_index = int(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_INDEX", -1))
        self._preferred_mic_name_hint = str(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_NAME_HINT", "") or "").strip()
        self._local_mic_state = {
            "status": "idle",
            "error": None,
            "updatedAt": None,
            "provider": str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local"),
        }
        self._google_backoff_until = 0.0
        self._google_error_count = 0
        self._whisper_model_name = None
        self._whisper_model = None
        self._whisper_prewarm_started = False

    def get_mode(self):
        return self.input_mode

    def _is_python_asr_mode(self):
        return self.input_mode in {"python_asr", "local_python_asr", "python_local", "local"}

    def requires_browser_page(self):
        return not self._is_python_asr_mode()

    def _set_local_mic_state(self, status, error=None):
        self._local_mic_state["status"] = status
        self._local_mic_state["error"] = error
        self._local_mic_state["updatedAt"] = int(time.time() * 1000)

    def _should_report_no_text_error(self):
        """仅在输入音量达到阈值时上报 no_text，避免安静环境误报。"""
        try:
            rms = int(self._local_last_audio_rms or 0)
        except Exception:
            rms = 0
        threshold = max(0, int(getattr(settings, "VOICE_PYTHON_NO_TEXT_WARN_RMS", 120) or 120))
        return rms >= threshold

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
        command_markers = [
            "置顶", "取消置顶", "秒杀", "上架", "链接", "商品", "橱窗",
            "pin", "unpin", "top", "link", "item", "product", "number", "no",
            "flash", "sale", "promotion", "deal", "start", "launch",
            "assistant", "cohost", "streamassistant", "liveassistant",
        ]
        has_command_marker = any(m in lower for m in command_markers)
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
        item = {
            "source": "python_mic",
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
        选择麦克风设备：
        1) 优先使用配置索引 VOICE_PYTHON_MIC_DEVICE_INDEX
        2) 再尝试默认设备
        3) 默认失败时，按名称 hint 或第一个可用设备兜底
        """
        configured_idx = int(self._preferred_mic_device_index)
        if configured_idx < 0:
            configured_idx = int(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_INDEX", -1))
        name_hint = str(self._preferred_mic_name_hint or "").strip().lower()
        if not name_hint:
            name_hint = str(getattr(settings, "VOICE_PYTHON_MIC_DEVICE_NAME_HINT", "") or "").strip().lower()

        names = self._list_local_microphones(sr)

        if configured_idx >= 0:
            try:
                return sr.Microphone(device_index=configured_idx), configured_idx
            except Exception:
                pass

        if not names:
            # 没有枚举到设备时再尝试系统默认设备。
            try:
                return sr.Microphone(), None
            except Exception:
                raise RuntimeError("No Input Device Available")

        if name_hint:
            for idx, name in enumerate(names):
                if name_hint in name.lower():
                    return sr.Microphone(device_index=idx), idx

        # 自动优先选择“真实麦克风”设备，避免系统默认路由到异常输入源。
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

        # 最后兜底：第一个可用设备
        for idx in range(len(names)):
            try:
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
        provider = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local").lower()
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

    def _transcribe_with_local_whisper(self, audio, lang=None):
        whisper_model = str(getattr(settings, "VOICE_WHISPER_MODEL", "tiny") or "tiny")
        whisper_root = str(
            Path(getattr(settings, "VOICE_WHISPER_DOWNLOAD_ROOT", "data/whisper_cache"))
            .expanduser()
            .resolve()
        )
        Path(whisper_root).mkdir(parents=True, exist_ok=True)

        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        if not raw:
            return ""
        pcm = np.frombuffer(raw, dtype=np.int16)
        if pcm.size == 0:
            return ""
        # 过短片段直接跳过，减少空转写。
        if pcm.size < int(16000 * 0.25):
            return ""
        audio_np = (pcm.astype(np.float32) / 32768.0).flatten()

        model = self._load_whisper_model(whisper_model, whisper_root)
        lang_short = (str(lang or "").split("-")[0] or "").lower() or None
        kwargs = {
            "task": "transcribe",
            "fp16": False,
            "condition_on_previous_text": False,
            "temperature": 0.0,
            "no_speech_threshold": 0.65,
            "logprob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
        }
        if lang_short:
            kwargs["language"] = lang_short
        result = model.transcribe(audio_np, **kwargs)
        text = str((result or {}).get("text") or "").strip()
        return text

    def _recognize_with_provider(self, recognizer, audio, langs):
        provider = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local").lower()
        primary_lang = str((langs or [""])[0] or "").lower()
        allow_google_fallback = bool(getattr(settings, "VOICE_ASR_ALLOW_GOOGLE_FALLBACK", False))
        # 统一 provider 链路：
        # - auto: 默认本地优先；LOCAL_FIRST_MODE=false 时英文优先 google（更快但依赖网络）
        # - google: 遇到超时等网络错误时回退 whisper/sphinx
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
        elif provider == "sphinx":
            providers = ["sphinx"]
        else:
            providers = ["whisper_local", "sphinx"] if primary_lang.startswith("zh") else ["google", "whisper_local", "sphinx"]

        # Google 通道网络波动时，短时间内跳过，避免每轮都被 timeout 拖慢。
        if time.time() < float(self._google_backoff_until or 0.0):
            providers = [p for p in providers if p != "google"]
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
                            return text, "en-US", None
                except Exception as e:
                    msg = str(e)
                    if "UnknownValueError" in type(e).__name__:
                        continue
                    provider_errors.append(f"sphinx:{msg}")
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
                        last_err = f"google_network_error:{msg}"
                        break
                    if p == "whisper_local" and (
                        "no module named" in msg.lower() or "recognize_whisper" in msg.lower()
                    ):
                        # 可选能力缺失，不视为当前轮次失败
                        continue
                    provider_errors.append(f"{p}:{msg}")
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
                    return best_text, best_lang, None
        if fallback_candidates:
            _, best_text, best_lang, _ = max(fallback_candidates, key=lambda x: x[0])
            return best_text, best_lang, None
        if provider_errors:
            return None, None, provider_errors[0]
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
        provider_now = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local").lower()
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
                mic, selected_idx = self._select_local_microphone(sr)
                with mic as source:
                    if ambient_s > 0:
                        try:
                            recognizer.adjust_for_ambient_noise(source, duration=ambient_s)
                        except Exception:
                            pass
                    logger.info(
                        "Python ASR recorder params: "
                        f"provider={provider_now}, dyn_energy={recognizer.dynamic_energy_threshold}, "
                        f"energy_threshold={recognizer.energy_threshold}, timeout={timeout_s}, "
                        f"phrase_time_limit={phrase_s}, forced_record={forced_record_seconds}"
                    )
                    self._local_running = True
                    self._local_last_start_at = int(time.time() * 1000)
                    self._set_local_mic_state("granted", None)
                    self._local_mic_state["deviceIndex"] = selected_idx
                    names = self._list_local_microphones(sr)
                    self._local_mic_state["deviceName"] = (
                        names[selected_idx] if (selected_idx is not None and 0 <= selected_idx < len(names)) else None
                    )
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
                            self._local_error = f"listen_error: {e}"
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
            self.last_start_diag = {"mode": self.input_mode, "provider": settings.VOICE_PYTHON_ASR_PROVIDER}
            self.last_start_success_at = time.time()
            logger.info(f"Python ASR 语音监听已启动: provider={settings.VOICE_PYTHON_ASR_PROVIDER}, langs={langs}")
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
                logger.debug(f"VoiceAgent JS 执行失败({ctx_name}): {e}")
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
        if self._is_python_asr_mode():
            expected_langs = self._dedupe_langs(language, fallback_languages)
            if self._local_running and expected_langs == self.last_langs:
                return True
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
            return {
                "supported": True,
                "running": bool(self._local_running),
                "error": self._local_error,
                "lang": (self.last_langs[0] if self.last_langs else self.last_lang),
                "langs": list(self.last_langs or []),
                "lastResultAt": self._local_last_result_at,
                "lastErrorAt": int(time.time() * 1000) if self._local_error else None,
                "lastText": self._local_last_text,
                "lastTextLang": self._local_last_text_lang,
                "ctx": "python_asr",
                "mode": self.input_mode,
                "provider": settings.VOICE_PYTHON_ASR_PROVIDER,
                "deviceIndex": self._local_mic_state.get("deviceIndex"),
                "deviceName": self._local_mic_state.get("deviceName"),
                "lastAudioAt": self._local_last_audio_at,
                "lastAudioRms": self._local_last_audio_rms,
                "noTextCount": self._local_no_text_count,
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
        items.extend(self.poll_transcripts())
        if include_subtitle and (not self._is_python_asr_mode()):
            items.extend(self.poll_subtitle_transcripts())
        items.sort(key=lambda x: x.get("ts") or 0)
        return items

    def get_start_failure_info(self):
        return {
            "reason": self.last_start_reason,
            "errors": list(self.last_start_errors),
            "diag": dict(self.last_start_diag or {}),
        }

    def diagnose_voice_capability(self):
        if self._is_python_asr_mode():
            sr = self._import_speech_recognition()
            return {
                "ctx": "python_asr",
                "mode": self.input_mode,
                "provider": settings.VOICE_PYTHON_ASR_PROVIDER,
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
        if self._is_python_asr_mode():
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
                    "provider": settings.VOICE_PYTHON_ASR_PROVIDER,
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
        if self._is_python_asr_mode():
            state = dict(self._local_mic_state)
            state.setdefault("mode", self.input_mode)
            state.setdefault("provider", settings.VOICE_PYTHON_ASR_PROVIDER)
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
