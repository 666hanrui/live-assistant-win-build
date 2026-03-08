import re
import time

import app_config.settings as settings
from utils.logger import logger


class VoiceCommandAgent:
    """
    Cloud-only voice command agent.
    Runtime path:
    1) browser Web Speech microphone recognition
    2) optional subtitle text fallback extraction
    """

    def __init__(self, vision_agent):
        self.vision_agent = vision_agent
        self.input_mode = "web_speech"
        self.last_lang = None
        self.last_langs = []
        self.enabled_in_page = False
        self.active_ctx_name = "page"
        self._subtitle_seen = {}
        self.last_start_reason = None
        self.last_start_errors = []
        self.last_start_diag = {}
        self.last_start_attempt_at = 0.0
        self.last_start_success_at = 0.0
        self.permission_blocked = False
        self._preferred_mic_device_index = None
        self._preferred_mic_name_hint = ""

    def get_mode(self):
        return "web_speech"

    def requires_browser_page(self):
        return True

    def set_preferred_microphone(self, device_index=None, name_hint=None, restart_if_running=False):
        self._preferred_mic_device_index = device_index
        self._preferred_mic_name_hint = str(name_hint or "").strip()
        return self.get_preferred_microphone()

    def get_preferred_microphone(self):
        return {
            "deviceIndex": self._preferred_mic_device_index,
            "nameHint": self._preferred_mic_name_hint,
        }

    def list_input_devices(self):
        return []

    def probe_local_microphone(self, language="zh-CN", fallback_languages=None, duration_seconds=2.5):
        return {"ok": False, "error": "browser_web_speech_only"}

    def prewarm_local_asr(self, force=False):
        return False

    def get_start_failure_info(self):
        return {
            "reason": self.last_start_reason,
            "errors": list(self.last_start_errors),
            "diag": dict(self.last_start_diag or {}),
        }

    def _run_js(self, script, *args, timeout=None):
        page = getattr(self.vision_agent, "page", None)
        if page is None:
            raise RuntimeError("browser_page_unavailable")
        if timeout is None:
            return page.run_js(script, *args)
        return page.run_js(script, *args, timeout=timeout)

    def _dedupe_langs(self, primary, fallback_languages):
        langs = []
        for lang in [primary] + list(fallback_languages or []):
            norm = str(lang or "").strip()
            if norm and norm not in langs:
                langs.append(norm)
        return langs or ["zh-CN"]

    def _language_family(self, code):
        raw = str(code or "").strip().lower()
        if raw.startswith("zh"):
            return "zh"
        if raw.startswith("en"):
            return "en"
        return ""

    def _is_runtime_lang_compatible(self, text, detected_lang=None):
        raw = str(text or "")
        if not raw:
            return False
        expected = self._language_family((self.last_langs or [self.last_lang])[0] if (self.last_langs or [self.last_lang]) else "")
        detected = self._language_family(detected_lang)
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", raw))
        has_latin = bool(re.search(r"[A-Za-z]", raw))
        if expected == "zh":
            if detected and detected != "zh":
                return False
            return not has_latin
        if expected == "en":
            if detected and detected != "en":
                return False
            return not has_cjk
        return True

    def _is_valid_voice_page(self, diag):
        title = str((diag or {}).get("title") or "").lower()
        url = str((diag or {}).get("url") or "").lower()
        if "tiktok.com" not in url:
            return False
        if "/live" in url:
            return True
        return "live" in title

    def diagnose_voice_capability(self):
        if not self.vision_agent.ensure_connection():
            return {
                "ctx": "page",
                "mode": "web_speech",
                "provider": "browser_web_speech",
                "captureMode": "browser_mic",
                "error": "browser_not_connected",
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
        try:
            data = self._run_js(script, timeout=1.2)
        except Exception as e:
            return {"ctx": "page", "error": str(e), "mode": "web_speech"}
        if not isinstance(data, dict):
            data = {}
        data["ctx"] = "page"
        data["mode"] = "web_speech"
        data["provider"] = "browser_web_speech"
        data["captureMode"] = "browser_mic"
        return data

    def start(self, language="zh-CN", fallback_languages=None, silence_restart_seconds=18):
        self.last_start_attempt_at = time.time()
        langs = self._dedupe_langs(language, fallback_languages)
        if not self.vision_agent.ensure_connection():
            self.enabled_in_page = False
            self.last_start_reason = "browser_not_connected"
            return False

        diag = self.diagnose_voice_capability()
        if not self._is_valid_voice_page(diag):
            self.enabled_in_page = False
            self.last_start_reason = "wrong_page"
            self.last_start_errors = []
            self.last_start_diag = {
                "ctx": "page",
                "title": diag.get("title"),
                "url": diag.get("url"),
            }
            logger.warning(
                f"语音口令监听启动跳过：当前页面不是直播间。title={diag.get('title')} url={diag.get('url')}"
            )
            return False

        silence_ms = max(4000, int(float(silence_restart_seconds or 18) * 1000))
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

          const controller = { recognition, shouldRun: true, langs, idx: 0, silenceMs };
          window.__liveAssistantVoiceController = controller;

          const currentLang = () => controller.langs[controller.idx] || controller.langs[0] || 'zh-CN';
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
            state.mode = 'web_speech';
            state.provider = 'browser_web_speech';
            state.runtimeProvider = 'browser_web_speech';
            state.runtimeProviderType = 'cloud';
            state.captureMode = 'browser_mic';
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
        try:
            result = self._run_js(script, {"langs": langs, "silenceMs": silence_ms}, timeout=2.0)
        except Exception as e:
            result = {"ok": False, "reason": str(e)}
        ok = bool(isinstance(result, dict) and result.get("ok"))
        if ok:
            self.enabled_in_page = True
            self.last_lang = language
            self.last_langs = langs
            self.last_start_success_at = time.time()
            self.last_start_reason = None
            self.last_start_errors = []
            self.last_start_diag = {}
            self.permission_blocked = False
            logger.info(f"语音口令监听已启动: ctx=page, langs={langs}")
            return True

        self.enabled_in_page = False
        reason = str((result or {}).get("reason") or "js_no_result")
        self.last_start_reason = reason
        self.last_start_errors = [reason]
        self.last_start_diag = {
            "ctx": "page",
            "secure": diag.get("secureContext"),
            "sr": diag.get("speechRecognition"),
            "media": diag.get("mediaDevices"),
            "title": diag.get("title"),
            "url": diag.get("url"),
        }
        if any(k in reason.lower() for k in ["not-allowed", "service-not-allowed", "audio-capture"]):
            self.permission_blocked = True
        logger.warning(f"语音口令监听启动失败: reason={reason}, diag={self.last_start_diag}")
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
        try:
            result = self._run_js(script, timeout=1.0)
        except Exception:
            result = {"ok": False}
        self.enabled_in_page = False
        return bool(isinstance(result, dict) and result.get("ok"))

    def ensure_started(self, language="zh-CN", fallback_languages=None, silence_restart_seconds=18):
        if not self.vision_agent.ensure_connection():
            self.enabled_in_page = False
            return False
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
            if cooldown > 0 and self.last_start_attempt_at > 0 and time.time() - self.last_start_attempt_at < cooldown:
                return False
            return self.start(
                language=language,
                fallback_languages=fallback_languages,
                silence_restart_seconds=silence_restart_seconds,
            )
        return True

    def get_state(self):
        if not self.vision_agent.ensure_connection():
            return {
                "supported": False,
                "running": False,
                "error": "browser_not_connected",
                "langs": list(self.last_langs or []),
                "ctx": "page",
                "mode": "web_speech",
                "provider": "browser_web_speech",
                "runtimeProvider": "browser_web_speech",
                "runtimeProviderType": "cloud",
                "captureMode": "browser_mic",
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
            lastErrorAt: st.lastErrorAt || null,
            lastText: st.lastText || '',
            lastTextLang: st.lastTextLang || null,
            noTextCount: st.noTextCount || 0
          };
        })();
        """
        try:
            state = self._run_js(script, timeout=1.0)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["ctx"] = "page"
        state["mode"] = "web_speech"
        state["provider"] = "browser_web_speech"
        state["runtimeProvider"] = "browser_web_speech"
        state["runtimeProviderType"] = "cloud"
        state["captureMode"] = "browser_mic"
        state["deviceName"] = ""
        return state

    def poll_transcripts(self):
        script = """
        return (() => {
          const queue = window.__liveAssistantVoiceQueue || [];
          const items = queue.splice(0, queue.length);
          const state = window.__liveAssistantVoiceState || {};
          if (items.length > 0) {
            const last = items[items.length - 1] || {};
            state.lastText = String(last.text || '');
            state.lastTextLang = last.lang || null;
            state.lastResultAt = last.ts || Date.now();
            state.noTextCount = 0;
          } else {
            state.noTextCount = Number(state.noTextCount || 0) + 1;
          }
          return {items};
        })();
        """
        try:
            data = self._run_js(script, timeout=1.0)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("items") or []
        cleaned = []
        now_ms = int(time.time() * 1000)
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            lang = item.get("lang")
            if not self._is_runtime_lang_compatible(text, detected_lang=lang):
                continue
            cleaned.append(
                {
                    "source": item.get("source") or "mic",
                    "text": text,
                    "confidence": item.get("confidence"),
                    "lang": lang,
                    "ts": item.get("ts") or now_ms,
                }
            )
        return cleaned

    def _normalize(self, text):
        raw = str(text or "").strip().lower()
        return "".join(ch for ch in raw if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"))

    def _prune_subtitle_seen(self, ttl=12):
        now = time.time()
        expired = [k for k, ts in self._subtitle_seen.items() if now - ts > ttl]
        for key in expired:
            self._subtitle_seen.pop(key, None)

    def poll_subtitle_transcripts(self, max_items=4):
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
              if (r.left > window.innerWidth * 0.82) continue;
              nodes.push({ text, ts: Date.now(), source: 'subtitle' });
            }
          }
          return { items: nodes.slice(-maxItems) };
        })(arguments[0] || 4);
        """
        try:
            data = self._run_js(script, int(max_items), timeout=1.0)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        cleaned = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
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
        if include_subtitle:
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

    def request_microphone_permission(self):
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
        try:
            result = self._run_js(script, timeout=2.0)
        except Exception as e:
            result = {"status": "unknown", "error": str(e)}
        if isinstance(result, dict):
            status = str(result.get("status") or "")
            if status == "granted":
                self.permission_blocked = False
            elif status in {"denied", "not-allowed"}:
                self.permission_blocked = True
            return result
        return {"status": "unknown", "error": "js_no_result"}

    def get_microphone_permission_state(self):
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
        try:
            result = self._run_js(script, timeout=1.0)
        except Exception as e:
            result = {"status": "unknown", "error": str(e), "updatedAt": None}
        if isinstance(result, dict):
            diag = self.diagnose_voice_capability()
            result.setdefault("page_title", diag.get("title"))
            result.setdefault("page_url", diag.get("url"))
            result.setdefault("mode", "web_speech")
            result.setdefault("provider", "browser_web_speech")
            result.setdefault("captureMode", "browser_mic")
            return result
        return {"status": "unknown", "error": "js_no_result", "updatedAt": None}
