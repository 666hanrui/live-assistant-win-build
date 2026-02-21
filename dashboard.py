import streamlit as st
import time
from datetime import datetime, date, timedelta
import subprocess
import sys
import importlib
import json
import pandas as pd
from main import LiveAssistant
import app_config.settings as settings
import os
import shutil
from pathlib import Path
from utils.vision_utils import find_button_on_screen
from utils.platform_utils import (
    build_chrome_debug_commands,
    get_microphone_permission_guide,
    get_python_asr_install_guide,
)

# 设置页面配置
st.set_page_config(
    page_title="AI 直播助手控制台",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _script_python():
    if getattr(sys, "frozen", False):
        return None
    return sys.executable


def _resolve_env_file_path():
    candidates = []
    env_override = os.getenv("LIVE_ASSISTANT_ENV", "").strip()
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(_app_base_dir() / ".env")

    seen = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return candidates[0]


def _save_env_values(updates):
    env_path = _resolve_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    normalized = {}
    for key, value in (updates or {}).items():
        k = str(key or "").strip()
        if not k:
            continue
        normalized[k] = str(value or "").strip()

    if not normalized:
        return env_path

    output = []
    seen_keys = set()
    for line in lines:
        stripped = line.strip()
        if (not stripped) or stripped.startswith("#") or ("=" not in stripped):
            output.append(line)
            continue
        cur_key = stripped.split("=", 1)[0].strip()
        if cur_key in normalized and cur_key not in seen_keys:
            output.append(f"{cur_key}={normalized[cur_key]}")
            seen_keys.add(cur_key)
        else:
            output.append(line)

    for k, v in normalized.items():
        if k not in seen_keys:
            output.append(f"{k}={v}")

    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return env_path


def _reload_runtime_settings():
    importlib.reload(settings)


def _reset_assistant_for_rerun():
    assistant = st.session_state.get("assistant")
    if assistant and getattr(assistant, "is_running", False):
        assistant.stop()
    if "assistant" in st.session_state:
        del st.session_state.assistant
    st.session_state.logs = []
    st.cache_resource.clear()

# 使用 Session State 管理单例助手实例
if 'assistant' not in st.session_state:
    st.session_state.assistant = LiveAssistant()
if 'logs' not in st.session_state:
    st.session_state.logs = []
if 'self_test_results' not in st.session_state:
    st.session_state.self_test_results = None
if 'report_generation_result' not in st.session_state:
    st.session_state.report_generation_result = None
if 'voice_stress_result' not in st.session_state:
    st.session_state.voice_stress_result = None
if 'full_regression_result' not in st.session_state:
    st.session_state.full_regression_result = None
if "show_user_guide" not in st.session_state:
    st.session_state.show_user_guide = False
if "cloud_asr_bili_test_enabled_ui" not in st.session_state:
    st.session_state.cloud_asr_bili_test_enabled_ui = False
if "cloud_asr_bili_test_prev_ui" not in st.session_state:
    st.session_state.cloud_asr_bili_test_prev_ui = False
if "cloud_asr_bili_test_force_reset_pending" not in st.session_state:
    st.session_state.cloud_asr_bili_test_force_reset_pending = False
if "cloud_asr_bili_test_last_error" not in st.session_state:
    st.session_state.cloud_asr_bili_test_last_error = ""
if "cloud_asr_bili_test_url_ui" not in st.session_state:
    st.session_state.cloud_asr_bili_test_url_ui = "https://www.bilibili.com/"
if "cloud_asr_bili_test_provider_ui" not in st.session_state:
    st.session_state.cloud_asr_bili_test_provider_ui = "follow_current"

# 兼容旧版会话对象（旧对象可能没有 get_unified_language 方法）
def _get_unified_language(assistant):
    if hasattr(assistant, "get_unified_language"):
        return assistant.get_unified_language()
    lang = getattr(assistant, "reply_language", settings.DEFAULT_REPLY_LANGUAGE)
    valid_languages = set(settings.REPLY_LANGUAGES.values())
    return lang if lang in valid_languages else settings.DEFAULT_REPLY_LANGUAGE


def _get_voice_command_enabled(assistant):
    return bool(getattr(assistant, "voice_command_enabled", settings.VOICE_COMMAND_ENABLED))


def _set_voice_command_enabled(assistant, enabled):
    if hasattr(assistant, "set_voice_command_enabled"):
        assistant.set_voice_command_enabled(enabled)
        return
    # 兼容旧对象：补齐属性，避免 UI 直接崩溃
    assistant.voice_command_enabled = bool(enabled)
    if hasattr(assistant, "_save_runtime_state"):
        assistant._save_runtime_state()


def _get_reply_enabled(assistant):
    return bool(getattr(assistant, "reply_enabled", True))


def _set_reply_enabled(assistant, enabled):
    if hasattr(assistant, "set_reply_enabled"):
        assistant.set_reply_enabled(enabled)
        return
    assistant.reply_enabled = bool(enabled)
    if hasattr(assistant, "_save_runtime_state"):
        assistant._save_runtime_state()


def _get_proactive_enabled(assistant):
    return bool(getattr(assistant, "proactive_enabled", settings.PROACTIVE_ENABLED))


def _set_proactive_enabled(assistant, enabled):
    if hasattr(assistant, "set_proactive_enabled"):
        assistant.set_proactive_enabled(enabled)
        return
    assistant.proactive_enabled = bool(enabled)
    if hasattr(assistant, "_save_runtime_state"):
        assistant._save_runtime_state()


def _get_voice_state(assistant):
    try:
        if hasattr(assistant, "voice") and hasattr(assistant.voice, "get_state"):
            state = assistant.voice.get_state()
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {}


def _get_voice_mode(assistant):
    try:
        voice = getattr(assistant, "voice", None)
        if voice and hasattr(voice, "get_mode"):
            return str(voice.get_mode() or "")
    except Exception:
        pass
    return str(getattr(settings, "VOICE_COMMAND_INPUT_MODE", "web_speech") or "web_speech")


def _is_local_voice_mode(mode):
    value = str(mode or "").strip().lower()
    return value in {
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


def _is_tab_media_voice_mode(mode):
    value = str(mode or "").strip().lower()
    return value in {
        "system_audio_asr",
        "tab_audio_asr",
        "tab_media_asr",
    }


def _is_loopback_voice_mode(mode):
    value = str(mode or "").strip().lower()
    return value in {
        "system_loopback_asr",
        "loopback_asr",
        "loopback",
    }


def _get_operation_execution_mode(assistant):
    try:
        if hasattr(assistant, "get_operation_execution_mode"):
            mode = str(assistant.get_operation_execution_mode() or "").strip().lower()
            if mode:
                return mode
    except Exception:
        pass
    return "ocr_vision"


def _set_operation_execution_mode(assistant, mode):
    value = str(mode or "").strip().lower()
    if value not in {"dom", "ocr_vision"}:
        value = "ocr_vision"
    try:
        if hasattr(assistant, "set_operation_execution_mode"):
            assistant.set_operation_execution_mode(value)
            return value
    except Exception:
        pass
    assistant.operation_execution_mode = value
    if hasattr(getattr(assistant, "operations", None), "set_execution_mode"):
        try:
            assistant.operations.set_execution_mode(value)
        except Exception:
            pass
    if hasattr(assistant, "_save_runtime_state"):
        assistant._save_runtime_state()
    return value


def _get_operation_mode_status(assistant):
    try:
        if hasattr(assistant, "get_operation_mode_status"):
            data = assistant.get_operation_mode_status()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"mode": _get_operation_execution_mode(assistant)}


def _get_human_like_settings(assistant):
    try:
        if hasattr(assistant, "get_human_like_settings"):
            data = assistant.get_human_like_settings()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    op = _get_operation_mode_status(assistant)
    data = op.get("human_like_settings")
    return data if isinstance(data, dict) else {}


def _set_human_like_settings(assistant, config):
    payload = dict(config or {})
    try:
        if hasattr(assistant, "set_human_like_settings"):
            assistant.set_human_like_settings(payload)
            return True
    except Exception:
        return False
    return False


def _get_human_like_stats(assistant):
    try:
        if hasattr(assistant, "get_human_like_stats"):
            data = assistant.get_human_like_stats()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    op = _get_operation_mode_status(assistant)
    data = op.get("human_like_stats")
    return data if isinstance(data, dict) else {}


def _get_web_info_source_mode(assistant):
    try:
        if hasattr(assistant, "get_web_info_source_mode"):
            mode = str(assistant.get_web_info_source_mode() or "").strip().lower()
            if mode:
                return mode
    except Exception:
        pass
    try:
        vision = getattr(assistant, "vision", None)
        if vision and hasattr(vision, "get_info_source_mode"):
            mode = str(vision.get_info_source_mode() or "").strip().lower()
            if mode:
                return mode
    except Exception:
        pass
    return "ocr_only"


def _set_web_info_source_mode(assistant, mode):
    value = str(mode or "").strip().lower()
    if value not in {"dom", "ocr_hybrid", "ocr_only", "screen_ocr"}:
        value = "ocr_only"
    try:
        if hasattr(assistant, "set_web_info_source_mode"):
            assistant.set_web_info_source_mode(value)
            return value
    except Exception:
        pass
    try:
        vision = getattr(assistant, "vision", None)
        if vision and hasattr(vision, "set_info_source_mode"):
            vision.set_info_source_mode(value)
    except Exception:
        pass
    if hasattr(assistant, "_save_runtime_state"):
        assistant._save_runtime_state()
    return value


def _get_web_info_source_status(assistant):
    try:
        if hasattr(assistant, "get_web_info_source_status"):
            data = assistant.get_web_info_source_status()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    try:
        vision = getattr(assistant, "vision", None)
        if vision and hasattr(vision, "get_info_source_status"):
            data = vision.get_info_source_status()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"mode": _get_web_info_source_mode(assistant)}


def _get_recent_voice_inputs(assistant, limit=80):
    try:
        if hasattr(assistant, "get_recent_voice_inputs"):
            data = assistant.get_recent_voice_inputs(limit=limit)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _get_voice_diag(assistant):
    try:
        voice = getattr(assistant, "voice", None)
        if voice and hasattr(voice, "diagnose_voice_capability"):
            diag = voice.diagnose_voice_capability()
            if isinstance(diag, dict):
                return diag
    except Exception:
        pass
    return {}


def _infer_browser_name(voice_diag=None, launch_cmd=""):
    ua = str((voice_diag or {}).get("userAgent") or "").lower()
    cmd = str(launch_cmd or "").lower()
    if "edg/" in ua or "msedge" in ua or "edge" in cmd:
        return "Edge"
    if "firefox" in ua:
        return "Firefox"
    if "chrome/" in ua or "google chrome" in cmd:
        return "Chrome"
    return "浏览器"


def _browser_mic_settings_hint(browser_name):
    if browser_name == "Edge":
        return "edge://settings/content/microphone"
    if browser_name == "Chrome":
        return "chrome://settings/content/microphone"
    return ""


def _get_startup_state(assistant):
    try:
        if hasattr(assistant, "get_startup_state"):
            state = assistant.get_startup_state()
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {
        "is_running": bool(getattr(assistant, "is_running", False)),
        "is_starting": bool(getattr(assistant, "is_starting", False)),
        "last_start_error": "",
        "last_start_detail": "",
        "last_start_at": 0.0,
    }


def _request_mic_permission(assistant):
    try:
        voice = getattr(assistant, "voice", None)
        need_page = True
        if voice and hasattr(voice, "requires_browser_page"):
            need_page = bool(voice.requires_browser_page())
        if need_page and (not getattr(getattr(assistant, "vision", None), "page", None)):
            try:
                assistant.connect_browser()
            except Exception:
                pass
        if voice and hasattr(voice, "request_microphone_permission"):
            first = voice.request_microphone_permission()
            status = str((first or {}).get("status") or "")
            # 授权弹窗是异步流程，短轮询拿到最终态，避免 UI 长期显示 unknown/requesting。
            if status in ("requesting", "idle"):
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    time.sleep(0.25)
                    cur = voice.get_microphone_permission_state()
                    cur_status = str((cur or {}).get("status") or "")
                    if cur_status not in ("requesting", "idle", ""):
                        return cur
                return voice.get_microphone_permission_state()
            return first
    except Exception as e:
        return {"status": "error", "error": str(e)}
    return {"status": "unsupported", "error": "voice_agent_unavailable"}


def _get_mic_permission_state(assistant):
    try:
        voice = getattr(assistant, "voice", None)
        if voice and hasattr(voice, "get_microphone_permission_state"):
            return voice.get_microphone_permission_state()
    except Exception:
        pass
    return {"status": "unknown", "error": None, "updatedAt": None}


def _list_python_mic_devices(assistant):
    try:
        voice = getattr(assistant, "voice", None)
        if voice and hasattr(voice, "list_input_devices"):
            devices = voice.list_input_devices()
            if isinstance(devices, list):
                return devices
    except Exception:
        pass
    return []


def _get_python_mic_selected_index(assistant):
    try:
        if hasattr(assistant, "get_voice_mic_device"):
            cfg = assistant.get_voice_mic_device() or {}
            idx = cfg.get("deviceIndex")
            if idx is None:
                return -1
            return int(idx)
    except Exception:
        pass
    return -1


def _set_python_mic_selected_index(assistant, device_index):
    try:
        if hasattr(assistant, "set_voice_mic_device"):
            assistant.set_voice_mic_device(device_index=device_index)
            return True
    except Exception:
        return False
    return False


def _probe_python_mic(assistant, duration_seconds=2.5):
    try:
        if hasattr(assistant, "probe_voice_microphone"):
            return assistant.probe_voice_microphone(duration_seconds=duration_seconds)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "probe_not_supported"}


def _get_status_snapshot(assistant):
    voice_state = _get_voice_state(assistant)
    startup_state = _get_startup_state(assistant)
    op_status = _get_operation_mode_status(assistant)
    human_like_cfg = _get_human_like_settings(assistant)
    human_like_stats = _get_human_like_stats(assistant)
    web_info_status = _get_web_info_source_status(assistant)
    web_mode = _get_web_info_source_mode(assistant)
    if web_mode == "screen_ocr":
        try:
            browser_connected = bool(getattr(getattr(assistant, "vision", None), "ensure_connection", lambda *_: False)())
        except Exception:
            browser_connected = False
    else:
        browser_connected = bool(getattr(getattr(assistant, "vision", None), "page", None))
    if (not browser_connected) and web_mode != "screen_ocr":
        now = time.time()
        last_probe = float(st.session_state.get("_browser_rebind_probe_at", 0.0) or 0.0)
        running_or_starting = bool(startup_state.get("is_running") or startup_state.get("is_starting"))
        # 未启动时只做“静态探测”，不做主动重连，避免日志显示“已连接”却仍是“已停止”的错位感。
        if hasattr(assistant, "get_browser_connected"):
            try:
                browser_connected = bool(assistant.get_browser_connected(allow_rebind=False))
            except TypeError:
                browser_connected = bool(assistant.get_browser_connected())
            except Exception:
                browser_connected = False
        # 仅在运行中/启动中允许自愈重连，且做限流。
        if (not browser_connected) and running_or_starting and (now - last_probe >= 2.5):
            st.session_state["_browser_rebind_probe_at"] = now
            try:
                if hasattr(assistant, "get_browser_connected"):
                    try:
                        browser_connected = bool(assistant.get_browser_connected(allow_rebind=True))
                    except TypeError:
                        browser_connected = bool(assistant.get_browser_connected())
            except Exception:
                browser_connected = False
    llm_online = bool(getattr(getattr(assistant, "knowledge", None), "has_llm", False))
    human_trace_list = human_like_stats.get("recent_action_traces") if isinstance(human_like_stats.get("recent_action_traces"), list) else []
    human_trace_df = _build_human_trace_dataframe(human_trace_list)
    human_trace_summary = _calc_human_trace_summary(human_trace_df, recent_window=20)
    return {
        "running": bool(startup_state.get("is_running")),
        "starting": bool(startup_state.get("is_starting")),
        "start_error": startup_state.get("last_start_error") or "none",
        "start_detail": startup_state.get("last_start_detail") or "",
        "browser_connected": browser_connected,
        "llm_online": llm_online,
        "voice_enabled": _get_voice_command_enabled(assistant),
        "reply_enabled": _get_reply_enabled(assistant),
        "proactive_enabled": _get_proactive_enabled(assistant),
        "voice_running": bool(voice_state.get("running")),
        "voice_error": voice_state.get("error") or "none",
        "voice_provider": voice_state.get("provider") or "",
        "voice_runtime_provider": voice_state.get("runtimeProvider") or "",
        "voice_runtime_provider_type": voice_state.get("runtimeProviderType") or "",
        "voice_runtime_chain": voice_state.get("runtimeProviderChain") if isinstance(voice_state.get("runtimeProviderChain"), list) else [],
        "voice_runtime_error": voice_state.get("runtimeProviderError") or "",
        "voice_loopback_likely_mic": bool(voice_state.get("loopbackLikelyMic", False)),
        "voice_capture_mode": voice_state.get("captureMode") or "",
        "voice_langs": voice_state.get("langs") if isinstance(voice_state.get("langs"), list) else [],
        "voice_last_text": (voice_state.get("lastText") or "").strip(),
        "voice_last_text_lang": voice_state.get("lastTextLang") or "",
        "voice_last_result_at": voice_state.get("lastResultAt") or None,
        "voice_last_audio_rms": voice_state.get("lastAudioRms") or 0,
        "voice_last_audio_at": voice_state.get("lastAudioAt") or None,
        "voice_no_text_count": voice_state.get("noTextCount") or 0,
        "voice_device_name": voice_state.get("deviceName") or "",
        "language": _get_unified_language(assistant),
        "operation_mode": _get_operation_execution_mode(assistant),
        "operation_ocr_available": bool(op_status.get("ocr_available", False)),
        "operation_ocr_provider": op_status.get("ocr_provider") or "",
        "operation_ocr_error": op_status.get("ocr_last_error") or "",
        "operation_ocr_ms": op_status.get("ocr_last_ms") or 0,
        "operation_ocr_text": op_status.get("ocr_last_text") or "",
        "operation_ocr_lines": op_status.get("ocr_last_lines") or 0,
        "operation_ocr_source": op_status.get("ocr_last_source") or "",
        "operation_last_click_driver": op_status.get("last_click_driver") or "",
        "operation_last_click_point": op_status.get("last_click_point") if isinstance(op_status.get("last_click_point"), dict) else {},
        "operation_last_click_error": op_status.get("last_click_error") or "",
        "operation_fixed_row_enabled": bool(op_status.get("ocr_pin_fixed_row_click_enabled", False)),
        "operation_fixed_row_force": bool(op_status.get("pin_unpin_force_fixed_row_click", False)),
        "operation_fixed_row_require_index": bool(op_status.get("pin_unpin_require_link_index", False)),
        "operation_fixed_row_panel_x_ratio": float(op_status.get("ocr_pin_fixed_row_click_panel_x_ratio") or 0.0),
        "operation_fixed_row_y_ratio": float(op_status.get("ocr_pin_fixed_row_click_offset_y_ratio") or 0.0),
        "operation_fixed_row_calib_log_enabled": bool(op_status.get("ocr_pin_fixed_row_calibration_log_enabled", False)),
        "operation_fixed_row_last": op_status.get("last_fixed_row_click") if isinstance(op_status.get("last_fixed_row_click"), dict) else {},
        "operation_dom_fallback_enabled": bool(op_status.get("dom_fallback_enabled", False)),
        "operation_force_full_physical_chain": bool(op_status.get("force_full_physical_chain", False)),
        "human_like_enabled": bool(human_like_cfg.get("enabled", True)),
        "human_like_delay_min_ms": int(float(human_like_cfg.get("delay_min_seconds", 0.0) or 0.0) * 1000),
        "human_like_delay_max_ms": int(float(human_like_cfg.get("delay_max_seconds", 0.0) or 0.0) * 1000),
        "human_like_post_delay_min_ms": int(float(human_like_cfg.get("post_delay_min_seconds", 0.0) or 0.0) * 1000),
        "human_like_post_delay_max_ms": int(float(human_like_cfg.get("post_delay_max_seconds", 0.0) or 0.0) * 1000),
        "human_like_click_jitter_px": float(human_like_cfg.get("click_jitter_px", 0.0) or 0.0),
        "human_like_ocr_physical_click_enabled": bool(human_like_cfg.get("ocr_physical_click_enabled", True)),
        "human_like_ocr_vision_allow_dom_fallback": bool(human_like_cfg.get("ocr_vision_allow_dom_fallback", False)),
        "human_like_force_full_physical_chain": bool(human_like_cfg.get("force_full_physical_chain", False)),
        "human_like_keyboard_fallback_enabled": bool(human_like_cfg.get("keyboard_fallback_enabled", False)),
        "human_like_message_keyboard_only_enabled": bool(human_like_cfg.get("message_keyboard_only_enabled", True)),
        "human_like_typing_min_ms": int(float(human_like_cfg.get("typing_min_interval_seconds", 0.0) or 0.0) * 1000),
        "human_like_typing_max_ms": int(float(human_like_cfg.get("typing_max_interval_seconds", 0.0) or 0.0) * 1000),
        "human_like_delay_total_ms": int(human_like_stats.get("delay_total_ms") or 0),
        "human_like_delay_calls": int(human_like_stats.get("delay_calls") or 0),
        "human_like_last_delay_ms": int(human_like_stats.get("last_delay_ms") or 0),
        "human_like_last_delay_reason": str(human_like_stats.get("last_delay_reason") or ""),
        "human_like_last_action_trace": human_like_stats.get("last_action_trace") if isinstance(human_like_stats.get("last_action_trace"), dict) else {},
        "human_like_recent_action_traces": human_trace_list,
        "human_like_recent_window": int(human_trace_summary.get("window") or 0),
        "human_like_recent_sample_count": int(human_trace_summary.get("sample_count") or 0),
        "human_like_recent_success_pct": int(human_trace_summary.get("success_pct") or 0),
        "human_like_recent_p95_ms": int(human_trace_summary.get("p95_ms") or 0),
        "human_like_recent_avg_ms": int(human_trace_summary.get("avg_ms") or 0),
        "human_like_recent_avg_delay_ms": int(human_trace_summary.get("avg_delay_ms") or 0),
        "web_info_mode": web_mode,
        "web_ocr_available": bool(web_info_status.get("ocr_available", False)),
        "web_ocr_page_type": web_info_status.get("ocr_page_type") or "",
        "web_ocr_error": web_info_status.get("ocr_error") or "",
        "web_ocr_ms": web_info_status.get("ocr_ms") or 0,
        "web_ocr_lines": web_info_status.get("ocr_line_count") or 0,
        "web_ocr_chat_count": web_info_status.get("ocr_chat_count") or 0,
        "web_ocr_live": bool(web_info_status.get("ocr_live")),
        "web_ocr_live_phase": web_info_status.get("ocr_live_phase") or "",
        "web_ocr_live_confidence": float(web_info_status.get("ocr_live_confidence") or 0.0),
        "web_ocr_live_has_timer": bool(web_info_status.get("ocr_live_has_timer")),
        "web_ocr_scene_tags": web_info_status.get("ocr_scene_tags") if isinstance(web_info_status.get("ocr_scene_tags"), list) else [],
        "web_ocr_block_count": web_info_status.get("ocr_block_count") or 0,
        "web_capture_backend": web_info_status.get("capture_backend") or "",
        "web_capture_error": web_info_status.get("capture_error") or "",
        "web_capture_ms": web_info_status.get("capture_ms") or 0,
    }


def _get_mock_shop_url(assistant):
    try:
        if hasattr(assistant, "get_mock_shop_url"):
            return assistant.get_mock_shop_url()
    except Exception:
        pass
    mock_file = (_app_base_dir() / "stress" / "mock_shop" / "mock_tiktok_shop.html").resolve()
    return f"{mock_file.as_uri()}?mock_tiktok_shop=1&view=dashboard_live"


def _get_cloud_asr_test_status(assistant):
    try:
        if hasattr(assistant, "get_cloud_asr_test_status"):
            data = assistant.get_cloud_asr_test_status()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"enabled": False}


def _poll_cloud_asr_test_transcripts(assistant, limit=20):
    try:
        if hasattr(assistant, "poll_cloud_asr_test_transcripts"):
            data = assistant.poll_cloud_asr_test_transcripts(limit=limit)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _short_text(text, limit=220):
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: max(20, limit - 3)] + "..."


def _build_human_trace_dataframe(traces):
    rows = list(traces or [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if "ok" not in df.columns:
        df["ok"] = False
    df["ok"] = df["ok"].fillna(False).astype(bool)

    if "elapsed_ms" not in df.columns:
        df["elapsed_ms"] = 0
    if "delay_ms" not in df.columns:
        df["delay_ms"] = 0
    df["elapsed_ms"] = pd.to_numeric(df["elapsed_ms"], errors="coerce").fillna(0).astype(int)
    df["delay_ms"] = pd.to_numeric(df["delay_ms"], errors="coerce").fillna(0).astype(int)

    if "end_at" in df.columns:
        df["time"] = pd.to_datetime(df["end_at"], unit="s", errors="coerce")
    elif "start_at" in df.columns:
        df["time"] = pd.to_datetime(df["start_at"], unit="s", errors="coerce")
    else:
        df["time"] = pd.NaT

    if df["time"].isna().all():
        df["time"] = pd.date_range(
            end=pd.Timestamp.now(),
            periods=len(df),
            freq="200ms",
        )
    return df.sort_values("time")


def _calc_human_trace_summary(trace_df, recent_window=20):
    if trace_df is None or trace_df.empty:
        return {
            "window": int(recent_window),
            "sample_count": 0,
            "success_pct": 0,
            "p95_ms": 0,
            "avg_ms": 0,
            "avg_delay_ms": 0,
        }

    n = min(max(1, int(recent_window or 20)), len(trace_df))
    recent_df = trace_df.tail(n)
    return {
        "window": int(n),
        "sample_count": int(len(recent_df)),
        "success_pct": int(round(float(recent_df["ok"].mean()) * 100)),
        "p95_ms": int(float(recent_df["elapsed_ms"].quantile(0.95))),
        "avg_ms": int(round(float(recent_df["elapsed_ms"].mean()))),
        "avg_delay_ms": int(round(float(recent_df["delay_ms"].mean()))),
    }


def _human_like_preset(name):
    presets = {
        "保守（低风险）": {
            "enabled": True,
            "delay_min_ms": 70,
            "delay_max_ms": 280,
            "post_delay_min_ms": 60,
            "post_delay_max_ms": 240,
            "jitter_px": 1.4,
            "ocr_physical_click_enabled": True,
            "keyboard_fallback_enabled": True,
            "typing_min_ms": 20,
            "typing_max_ms": 62,
            "force_full_physical_chain": False,
        },
        "平衡（推荐）": {
            "enabled": True,
            "delay_min_ms": 45,
            "delay_max_ms": 190,
            "post_delay_min_ms": 35,
            "post_delay_max_ms": 155,
            "jitter_px": 1.8,
            "ocr_physical_click_enabled": True,
            "keyboard_fallback_enabled": True,
            "typing_min_ms": 18,
            "typing_max_ms": 55,
            "force_full_physical_chain": False,
        },
        "激进（速度优先）": {
            "enabled": True,
            "delay_min_ms": 12,
            "delay_max_ms": 90,
            "post_delay_min_ms": 10,
            "post_delay_max_ms": 82,
            "jitter_px": 1.1,
            "ocr_physical_click_enabled": True,
            "keyboard_fallback_enabled": False,
            "typing_min_ms": 12,
            "typing_max_ms": 34,
            "force_full_physical_chain": False,
        },
        "强制全链路物理键鼠": {
            "enabled": True,
            "delay_min_ms": 65,
            "delay_max_ms": 240,
            "post_delay_min_ms": 55,
            "post_delay_max_ms": 210,
            "jitter_px": 1.6,
            "ocr_physical_click_enabled": True,
            "keyboard_fallback_enabled": True,
            "typing_min_ms": 20,
            "typing_max_ms": 64,
            "force_full_physical_chain": True,
        },
    }
    return dict(presets.get(name) or presets["平衡（推荐）"])


def _apply_human_preset_to_ui(name):
    cfg = _human_like_preset(name)
    st.session_state.human_like_enabled_ui = bool(cfg.get("enabled", True))
    st.session_state.human_like_delay_min_ms_ui = int(cfg.get("delay_min_ms", 45))
    st.session_state.human_like_delay_max_ms_ui = int(cfg.get("delay_max_ms", 190))
    st.session_state.human_like_post_delay_min_ms_ui = int(cfg.get("post_delay_min_ms", 35))
    st.session_state.human_like_post_delay_max_ms_ui = int(cfg.get("post_delay_max_ms", 155))
    st.session_state.human_like_click_jitter_px_ui = float(cfg.get("jitter_px", 1.8))
    st.session_state.ocr_physical_click_enabled_ui = bool(cfg.get("ocr_physical_click_enabled", True))
    st.session_state.os_keyboard_fallback_enabled_ui = bool(cfg.get("keyboard_fallback_enabled", True))
    st.session_state.os_keyboard_typing_min_ms_ui = int(cfg.get("typing_min_ms", 18))
    st.session_state.os_keyboard_typing_max_ms_ui = int(cfg.get("typing_max_ms", 55))
    st.session_state.force_full_physical_chain_ui = bool(cfg.get("force_full_physical_chain", False))


def _build_human_like_payload_from_ui():
    delay_min_ms = int(st.session_state.get("human_like_delay_min_ms_ui") or 0)
    delay_max_ms = int(st.session_state.get("human_like_delay_max_ms_ui") or 0)
    if delay_max_ms < delay_min_ms:
        delay_max_ms = delay_min_ms

    post_min_ms = int(st.session_state.get("human_like_post_delay_min_ms_ui") or 0)
    post_max_ms = int(st.session_state.get("human_like_post_delay_max_ms_ui") or 0)
    if post_max_ms < post_min_ms:
        post_max_ms = post_min_ms

    typing_min_ms = int(st.session_state.get("os_keyboard_typing_min_ms_ui") or 1)
    typing_max_ms = int(st.session_state.get("os_keyboard_typing_max_ms_ui") or 1)
    if typing_max_ms < typing_min_ms:
        typing_max_ms = typing_min_ms

    force_full_physical_chain = bool(st.session_state.get("force_full_physical_chain_ui", False))
    return {
        "enabled": bool(st.session_state.get("human_like_enabled_ui", True)),
        "delay_min_seconds": delay_min_ms / 1000.0,
        "delay_max_seconds": delay_max_ms / 1000.0,
        "post_delay_min_seconds": post_min_ms / 1000.0,
        "post_delay_max_seconds": post_max_ms / 1000.0,
        "click_jitter_px": float(st.session_state.get("human_like_click_jitter_px_ui") or 0.0),
        "ocr_physical_click_enabled": bool(st.session_state.get("ocr_physical_click_enabled_ui", True)) or force_full_physical_chain,
        "keyboard_fallback_enabled": bool(st.session_state.get("os_keyboard_fallback_enabled_ui", False)),
        "typing_min_interval_seconds": max(0.001, typing_min_ms / 1000.0),
        "typing_max_interval_seconds": max(0.001, typing_max_ms / 1000.0),
        "message_keyboard_only_enabled": True if force_full_physical_chain else bool(st.session_state.get("message_keyboard_only_enabled_ui", True)),
        "ocr_vision_allow_dom_fallback": False if force_full_physical_chain else bool(st.session_state.get("ocr_vision_allow_dom_fallback_ui", False)),
        "force_full_physical_chain": force_full_physical_chain,
        "pin_click_test_confirm_popup": bool(st.session_state.get("pin_click_test_confirm_popup_ui", False)),
    }


def _run_local_cmd(cmd, timeout=300):
    try:
        p = subprocess.run(
            cmd,
            cwd=str(_app_base_dir()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        return {
            "ok": p.returncode == 0,
            "code": p.returncode,
            "stdout": out[-4000:] if out else "",
            "stderr": err[-4000:] if err else "",
            "cmd": " ".join(cmd),
        }
    except Exception as e:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": str(e),
            "cmd": " ".join(cmd),
        }


def _load_user_guide_markdown():
    guide_path = _app_base_dir() / "docs" / "USER_GUIDE.md"
    if guide_path.exists():
        try:
            return guide_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"使用说明书读取失败：{e}"
    return "未找到使用说明书文件：docs/USER_GUIDE.md"


def _find_powershell():
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def _run_local_voice_stress_pipeline(profile="quick", rounds=1, gap_seconds=2):
    py = _script_python()
    if not py:
        return {
            "ok": False,
            "steps": [{
                "step": "precheck",
                "ok": False,
                "code": 2,
                "cmd": "python scripts/voice_stress_pack.py",
                "stdout": "",
                "stderr": "frozen_runtime_no_python: EXE 模式下不支持本地脚本压测，请在源码环境运行。",
            }],
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    base = _app_base_dir()
    steps = [("offline", [py, str(base / "scripts" / "voice_stress_pack.py"), "offline", "--profile", profile, "--json"], 180)]

    if sys.platform == "darwin":
        steps.extend([
            ("generate_audio", [py, str(base / "scripts" / "voice_audio_runner.py"), "generate-mac", "--profile", profile], 240),
            (
                "play_audio",
                [
                    py,
                    str(base / "scripts" / "voice_audio_runner.py"),
                    "play-mac",
                    "--profile",
                    profile,
                    "--rounds",
                    str(max(1, int(rounds))),
                    "--gap-seconds",
                    str(max(0, int(gap_seconds))),
                ],
                600,
            ),
        ])
    elif os.name == "nt":
        powershell = _find_powershell()
        if not powershell:
            return {
                "ok": False,
                "steps": [{
                    "step": "precheck",
                    "ok": False,
                    "code": 2,
                    "cmd": "powershell -ExecutionPolicy Bypass ...",
                    "stdout": "",
                    "stderr": "powershell_not_found",
                }],
                "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        win_voice_root = base / "stress" / "voice"
        steps.extend([
            (
                "generate_audio",
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(win_voice_root / "windows_tts_generate.ps1"),
                    "-Profile",
                    profile,
                ],
                260,
            ),
            (
                "play_audio",
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(win_voice_root / "windows_playback_loop.ps1"),
                    "-Rounds",
                    str(max(1, int(rounds))),
                    "-GapSeconds",
                    str(max(0, int(gap_seconds))),
                ],
                600,
            ),
        ])
    else:
        return {
            "ok": False,
            "steps": [{
                "step": "precheck",
                "ok": False,
                "code": 2,
                "cmd": f"platform={sys.platform}",
                "stdout": "",
                "stderr": "unsupported_platform_for_one_click_voice_stress",
            }],
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    steps.append(("log_scan", [py, str(base / "scripts" / "voice_stress_pack.py"), "log-scan", "--minutes", "30"], 120))
    results = []
    all_ok = True
    for name, cmd, timeout in steps:
        res = _run_local_cmd(cmd, timeout=timeout)
        res["step"] = name
        results.append(res)
        if not res["ok"]:
            all_ok = False
            break
    return {"ok": all_ok, "steps": results, "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _run_global_feature_regression(profile="full"):
    py = _script_python()
    if not py:
        return {
            "ok": False,
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "step": {
                "ok": False,
                "code": 2,
                "cmd": "python scripts/global_feature_test.py --profile full",
                "stdout": "",
                "stderr": "frozen_runtime_no_python: EXE 模式下不支持脚本回归，请在源码环境运行。",
            },
            "report_json": "",
            "report_md": "",
            "report": {},
        }

    base = _app_base_dir()
    cmd = [py, str(base / "scripts" / "global_feature_test.py"), "--profile", str(profile or "full")]
    timeout = 1200 if str(profile or "full") == "full" else 420
    step = _run_local_cmd(cmd, timeout=timeout)

    report_json = ""
    report_md = ""
    merged = "\n".join([str(step.get("stdout") or ""), str(step.get("stderr") or "")])
    for line in merged.splitlines():
        if line.startswith("global_feature_report_json="):
            report_json = line.split("=", 1)[1].strip()
        elif line.startswith("global_feature_report_md="):
            report_md = line.split("=", 1)[1].strip()

    report_payload = {}
    if report_json:
        try:
            p = Path(report_json)
            if p.exists():
                report_payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            report_payload = {}

    overall_ok = bool(step.get("ok"))
    if isinstance(report_payload, dict) and report_payload:
        overall_ok = bool(report_payload.get("overall_ok"))

    return {
        "ok": overall_ok,
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "step": step,
        "report_json": report_json,
        "report_md": report_md,
        "report": report_payload,
    }


def _run_system_self_check(assistant):
    checks = []

    def add(name, ok, detail):
        checks.append(
            {
                "检查项": name,
                "结果": "PASS" if ok else "FAIL",
                "详情": detail,
            }
        )

    # 1) 环境与配置
    local_first_enabled = bool(getattr(settings, "LOCAL_FIRST_MODE", False))
    add("Local First Mode", local_first_enabled, "enabled" if local_first_enabled else "disabled")

    llm_remote_enabled = bool(getattr(settings, "LLM_REMOTE_ENABLED", True))
    has_api_key = bool(settings.LLM_API_KEY)
    if llm_remote_enabled:
        add("LLM API Key", has_api_key, "已配置" if has_api_key else "未配置")
    else:
        add("LLM API Key", True, "远端 LLM 已禁用，不依赖 API Key")

    if llm_remote_enabled:
        add("LLM Remote Switch", has_api_key, "enabled（需 API Key）")
    else:
        add("LLM Remote Switch", True, "disabled（local-only）")

    llm_online = bool(getattr(getattr(assistant, "knowledge", None), "has_llm", False))
    if llm_remote_enabled:
        add("LLM Runtime", llm_online, "在线" if llm_online else "离线")
    else:
        add("LLM Runtime", True, "按配置禁用远端 LLM")

    embedding_local_only = bool(getattr(settings, "EMBEDDING_LOCAL_FILES_ONLY", False))
    embedding_online_fallback = bool(getattr(settings, "EMBEDDING_ENABLE_ONLINE_FALLBACK", False))
    add(
        "Embedding Offline",
        embedding_local_only and (not embedding_online_fallback),
        f"local_only={embedding_local_only}, online_fallback={embedding_online_fallback}",
    )

    web_info_mode = _get_web_info_source_mode(assistant)

    # 2) 主信息源连接状态
    if web_info_mode == "screen_ocr":
        try:
            browser_ok = bool(getattr(getattr(assistant, "vision", None), "ensure_connection", lambda *_: False)())
        except Exception:
            browser_ok = False
        browser_detail = "屏幕采集就绪" if browser_ok else "屏幕采集不可用"
        add("Screen Source", browser_ok, browser_detail)
    else:
        browser_ok = bool(getattr(getattr(assistant, "vision", None), "page", None))
        browser_detail = "已连接" if browser_ok else "未连接"
        if not browser_ok:
            try:
                browser_ok = bool(assistant.connect_browser())
                browser_detail = "已自动重连" if browser_ok else "重连失败"
            except Exception as e:
                browser_detail = f"重连异常: {e}"
                browser_ok = False
        add("Browser", browser_ok, browser_detail)

    # 3) 语言与语音状态
    lang = _get_unified_language(assistant)
    lang_ok = lang in set(settings.REPLY_LANGUAGES.values())
    add("Unified Language", lang_ok, lang)
    op_mode = _get_operation_execution_mode(assistant)
    add("Execution Mode", op_mode in {"dom", "ocr_vision"}, op_mode)
    add("Web Info Mode", web_info_mode in {"dom", "ocr_hybrid", "ocr_only", "screen_ocr"}, web_info_mode)
    op_status = _get_operation_mode_status(assistant)
    web_info_status = _get_web_info_source_status(assistant)
    if op_mode == "ocr_vision":
        add(
            "OCR Runtime",
            bool(op_status.get("ocr_available")),
            f"provider={op_status.get('ocr_provider') or '-'}, err={op_status.get('ocr_last_error') or 'none'}",
        )
    if web_info_mode in {"ocr_hybrid", "ocr_only", "screen_ocr"}:
        add(
            "Web OCR Runtime",
            bool(web_info_status.get("ocr_available")),
            (
                f"page={web_info_status.get('ocr_page_type') or '-'}, "
                f"ms={web_info_status.get('ocr_ms') or 0}, "
                f"err={web_info_status.get('ocr_error') or 'none'}, "
                f"cap={web_info_status.get('capture_backend') or '-'}"
            ),
        )

    voice_enabled = _get_voice_command_enabled(assistant)
    voice_state = _get_voice_state(assistant)
    voice_mode = _get_voice_mode(assistant)
    voice_provider = str(
        voice_state.get("provider")
        or getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local")
        or "whisper_local"
    ).lower()
    voice_google_fallback = bool(getattr(settings, "VOICE_ASR_ALLOW_GOOGLE_FALLBACK", False))
    voice_dashscope_ready = bool(str(getattr(settings, "VOICE_DASHSCOPE_API_KEY", "") or "").strip())
    voice_chain_ok = (
        _is_local_voice_mode(voice_mode)
        and voice_provider in {"whisper_local", "sphinx", "auto", "google", "dashscope_funasr", "hybrid_local_cloud"}
        and (
            voice_provider not in {"dashscope_funasr", "hybrid_local_cloud"}
            or voice_dashscope_ready
        )
    )
    add(
        "Voice ASR Chain",
        voice_chain_ok,
        f"mode={voice_mode}, provider={voice_provider}, google_fallback={voice_google_fallback}, dashscope_ready={voice_dashscope_ready}",
    )

    if voice_enabled:
        voice_running = bool(voice_state.get("running"))
        voice_err = voice_state.get("error") or "none"
        add("Voice Listener", voice_running and voice_err == "none", f"running={voice_running}, err={voice_err}")
    else:
        add("Voice Listener", True, "已关闭（按配置）")

    # 3.1) 语音输入链路诊断（尤其用于 VM 场景）
    if _is_local_voice_mode(voice_mode):
        capture_mode = str(voice_state.get("captureMode") or "").strip().lower()
        source = str(voice_state.get("source") or "").strip().lower()
        tab_mode = _is_tab_media_voice_mode(voice_mode) or capture_mode == "tab_media_stream"
        if tab_mode:
            tab_ok = capture_mode == "tab_media_stream" and source == "tab_audio_stream"
            add("Tab Stream Route", tab_ok, f"mode={voice_mode}, capture={capture_mode or '-'}, source={source or '-'}")
        else:
            devices = _list_python_mic_devices(assistant)
            device_count = len(devices)
            device_preview = ",".join(f"{d.get('index')}:{d.get('name')}" for d in devices[:6]) if devices else "none"
            vm_audio_hint = (
                "若在 VMware/远程桌面：请在虚拟机设置启用声卡并透传麦克风，Windows 隐私里允许桌面应用访问麦克风。"
                if os.name == "nt"
                else "若在虚拟机：请确认宿主机麦克风已透传到来宾系统。"
            )
            devices_ok = device_count > 0
            devices_detail = f"count={device_count}, devices={device_preview}"
            if not devices_ok:
                devices_detail += f" | hint={vm_audio_hint}"
            add("Audio Input Devices", devices_ok, devices_detail)

            perm = _request_mic_permission(assistant)
            perm_status = str((perm or {}).get("status") or "").strip().lower() or "unknown"
            perm_ok = perm_status == "granted"
            perm_detail = f"status={perm_status}, err={(perm or {}).get('error') or 'none'}"
            if not perm_ok:
                perm_detail += f" | hint={vm_audio_hint}"
            add("Voice Permission", perm_ok, perm_detail)

            if perm_ok:
                probe = _probe_python_mic(assistant, duration_seconds=2.0)
                probe_ok = bool((probe or {}).get("ok"))
                probe_detail = (
                    f"ok={probe_ok}, rms={(probe or {}).get('rms')}, "
                    f"text={str((probe or {}).get('text') or '')[:48]}, err={(probe or {}).get('error') or 'none'}"
                )
                add("Voice Probe", probe_ok, probe_detail)

            if _is_loopback_voice_mode(voice_mode) or capture_mode == "loopback":
                loopback_ok = capture_mode == "loopback" and source == "python_loopback"
                add("Loopback Route", loopback_ok, f"mode={voice_mode}, capture={capture_mode or '-'}, source={source or '-'}")
    else:
        perm_state = _get_mic_permission_state(assistant)
        perm_status = str((perm_state or {}).get("status") or "").strip().lower() or "unknown"
        add("Browser Mic Permission", perm_status in {"granted", "requesting", "idle"}, f"status={perm_status}, err={(perm_state or {}).get('error') or 'none'}")

    # 4) 口令解析（中英文）
    parser_ok = hasattr(assistant, "_parse_operation_command_text")
    parser_detail = "方法不存在"
    if parser_ok:
        cases = [
            ("助播 将3号链接置顶一下", ("pin_product", 3)),
            ("助播 将3号链接取消置顶", ("unpin_product", 3)),
            ("assistant pin link three", ("pin_product", 3)),
            ("assistant unpin link three", ("unpin_product", 3)),
            ("cohost top item fifth", ("pin_product", 5)),
            ("assistant launch flash promotion now", ("start_flash_sale", None)),
            ("助播 结束秒杀活动", ("stop_flash_sale", None)),
            ("assistant stop flash sale now", ("stop_flash_sale", None)),
            ("pin link 3 please", ("pin_product", 3)),
            ("unpin link 3 please", ("unpin_product", 3)),
            ("start flash sale now", ("start_flash_sale", None)),
        ]
        failed = []
        for text, expected in cases:
            result = assistant._parse_operation_command_text(text)
            got = (result.get("action"), result.get("link_index")) if result else None
            if got != expected:
                failed.append(f"{text} -> {got}")
        parser_ok = len(failed) == 0
        parser_detail = "中英文样例通过" if parser_ok else "; ".join(failed[:2])
    add("Command Parser", parser_ok, parser_detail)

    # 4.1) 口令解析性能冒烟（避免规则膨胀导致解析卡顿）
    parser_perf_ok = parser_ok
    parser_perf_detail = "skip"
    if hasattr(assistant, "_parse_operation_command_text"):
        try:
            sample_inputs = [
                "助播 置顶3号链接",
                "assistant pin link three",
                "cohost unpin link 2",
                "助播 秒杀活动上架",
                "assistant start flash sale now",
            ]
            start_t = time.perf_counter()
            for i in range(800):
                assistant._parse_operation_command_text(sample_inputs[i % len(sample_inputs)])
            cost_ms = (time.perf_counter() - start_t) * 1000.0
            parser_perf_ok = cost_ms < 250.0
            parser_perf_detail = f"{cost_ms:.1f}ms/800"
        except Exception as e:
            parser_perf_ok = False
            parser_perf_detail = str(e)
    add("Parser Perf", parser_perf_ok, parser_perf_detail)

    # 5) 唤醒词
    wake_ok = hasattr(assistant, "_pass_voice_wake_word")
    wake_detail = "方法不存在"
    if wake_ok:
        wake_cases = [
            ("助播 把3号链接置顶", True),
            ("assistant pin link three", True),
            ("random text pin link three", False),
        ]
        wake_failed = []
        for text, expected in wake_cases:
            got = assistant._pass_voice_wake_word(text)
            if got != expected:
                wake_failed.append(f"{text} -> {got}")
        wake_ok = len(wake_failed) == 0
        wake_detail = "匹配正常" if wake_ok else "; ".join(wake_failed[:2])
    add("Wake Words", wake_ok, wake_detail)

    # 6) 语音门控（严格唤醒词 / 宽松显式指令）
    gate_ok = hasattr(assistant, "_pass_voice_wake_word") and hasattr(assistant, "_parse_operation_command_text")
    gate_detail = "方法不存在"
    if gate_ok:
        strict_backup = settings.VOICE_STRICT_WAKE_WORD
        try:
            def _allow(text):
                has_wake = assistant._pass_voice_wake_word(text)
                cmd = assistant._parse_operation_command_text(text)
                if settings.VOICE_STRICT_WAKE_WORD:
                    return has_wake
                return has_wake or bool(cmd)

            settings.VOICE_STRICT_WAKE_WORD = True
            strict_no_wake = _allow("pin link 3 please")
            strict_with_wake = _allow("assistant pin link three")

            settings.VOICE_STRICT_WAKE_WORD = False
            loose_no_wake = _allow("pin link 3 please")

            gate_ok = (strict_no_wake is False) and (strict_with_wake is True) and (loose_no_wake is True)
            gate_detail = (
                f"strict_no_wake={strict_no_wake}, strict_with_wake={strict_with_wake}, "
                f"loose_no_wake={loose_no_wake}"
            )
        except Exception as e:
            gate_ok = False
            gate_detail = str(e)
        finally:
            settings.VOICE_STRICT_WAKE_WORD = strict_backup
    add("Voice Gate", gate_ok, gate_detail)

    # 7) 发送文本清洗
    sanitize_fn = getattr(getattr(assistant, "operations", None), "_sanitize_outgoing_text", None)
    sanitize_ok = callable(sanitize_fn)
    sanitize_detail = "方法不存在"
    if sanitize_ok:
        probe = sanitize_fn("⌘a⌫ Ctrl+a hello")
        sanitize_ok = "hello" in probe and "⌘" not in probe and "ctrl+a" not in probe.lower()
        sanitize_detail = probe
    add("Send Sanitizer", sanitize_ok, sanitize_detail)

    # 8) 状态持久化
    save_ok = hasattr(assistant, "_save_runtime_state")
    save_detail = "方法不存在"
    if save_ok:
        try:
            assistant._save_runtime_state()
            save_detail = "runtime_state.json 已写入"
        except Exception as e:
            save_ok = False
            save_detail = str(e)
    add("Runtime State Save", save_ok, save_detail)

    pass_count = sum(1 for x in checks if x["结果"] == "PASS")
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pass_count": pass_count,
        "total": len(checks),
        "checks": checks,
    }


# 兼容旧版会话对象（热重载后旧对象缺少新属性/方法）
if (not hasattr(st.session_state.assistant, "voice_command_enabled")) or (not hasattr(st.session_state.assistant, "analytics")):
    st.session_state.assistant = LiveAssistant()

# 定义日志文件路径
LOG_FILE = "logs/app.log"

def load_logs():
    """读取最新的日志"""
    if os.path.exists(LOG_FILE):
        # EXE 在 Windows 中文系统下默认文本编码常为 gbk，
        # 日志文件可能是 UTF-8；这里固定 UTF-8 并容错，避免解码崩溃。
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            # 读取最后 50 行
            lines = f.readlines()[-50:]
            return "".join(lines)
    return "暂无日志"


def render_monitor_body():
    """渲染监控区主体（可被静态渲染或 fragment 局部刷新复用）。"""
    col_d1, col_d2 = st.columns([2, 1])
    monitor_snapshot = _get_status_snapshot(st.session_state.assistant)

    with col_d1:
        st.write("🧭 **动作拟人化统计**")
        trace = monitor_snapshot.get("human_like_last_action_trace") or {}
        traces = list(monitor_snapshot.get("human_like_recent_action_traces") or [])
        trace_df = _build_human_trace_dataframe(traces)

        t1, t2, t3, t4, t5, t6 = st.columns(6)
        t1.metric("最近动作耗时(ms)", int((trace or {}).get("elapsed_ms") or 0))
        t2.metric("最近动作延迟(ms)", int((trace or {}).get("delay_ms") or 0))
        t3.metric("最近动作延迟次数", int((trace or {}).get("delay_count") or 0))
        t4.metric("累计延迟(ms)", int(monitor_snapshot.get("human_like_delay_total_ms") or 0))
        t5.metric("最近成功率(20次)", f"{int(monitor_snapshot.get('human_like_recent_success_pct') or 0)}%")
        t6.metric("最近P95耗时(ms)", int(monitor_snapshot.get("human_like_recent_p95_ms") or 0))

        if not trace_df.empty:
            csv_bytes = trace_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "导出动作轨迹 CSV",
                data=csv_bytes,
                file_name=f"human_action_traces_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="download_human_trace_csv",
                use_container_width=False,
            )
            metric_df = trace_df[["time", "elapsed_ms", "delay_ms"]].dropna(subset=["time"])
            if not metric_df.empty:
                st.line_chart(metric_df.tail(30).set_index("time"), use_container_width=True, height=180)
            view_cols = [c for c in ["action", "ok", "elapsed_ms", "delay_ms", "delay_count", "last_delay_reason", "note"] if c in trace_df.columns]
            if view_cols:
                st.dataframe(trace_df[view_cols].tail(12), use_container_width=True, hide_index=True, height=220)
        else:
            st.info("暂无动作统计，请先触发一次置顶/取消置顶/秒杀动作。")

        st.write("🎙️ **实时语音输入流（原始识别）**")
        voice_list = _get_recent_voice_inputs(st.session_state.assistant, limit=80)
        if voice_list:
            st.dataframe(
                voice_list,
                column_config={
                    "time": "时间",
                    "source": "来源",
                    "text": "识别文本",
                    "lang": "语言",
                    "status": "处理状态",
                    "note": "备注",
                    "command": "命令",
                },
                use_container_width=True,
                height=220,
            )
        else:
            st.info("暂无语音识别文本")

        st.write("📜 **实时弹幕流**")
        danmu_list = list(st.session_state.assistant.danmu_log)
        if danmu_list:
            st.dataframe(
                danmu_list,
                column_config={
                    "time": "时间",
                    "user": "用户",
                    "text": "内容",
                    "status": "状态",
                    "reply": "回复内容",
                    "action": "触发动作",
                    "action_receipt": "动作回执",
                },
                use_container_width=True,
                height=220
            )
        else:
            st.info("暂无弹幕数据")

    with col_d2:
        st.write("📝 **系统日志 (最后50行)**")
        logs = load_logs()
        st.code(logs, language="text")


@st.fragment(run_every="1s")
def render_monitor_fragment():
    """仅局部刷新监控区，避免整页闪烁。"""
    try:
        render_monitor_body()
    except Exception as e:
        st.error(f"运行监控渲染异常：{e}")


@st.fragment(run_every="1s")
def render_top_live_status_fragment():
    """顶部状态区高频刷新，减少“命令已执行但文字晚更新”的体感延迟。"""
    assistant = st.session_state.assistant
    live_snapshot = _get_status_snapshot(assistant)
    live_launch = build_chrome_debug_commands(
        port=settings.BROWSER_PORT,
        user_data_path=settings.USER_DATA_PATH,
        chrome_executable=settings.CHROME_EXECUTABLE,
    )
    live_voice_mode = _get_voice_mode(assistant)
    live_is_python_voice_mode = _is_local_voice_mode(live_voice_mode)
    live_is_tab_media_voice_mode = _is_tab_media_voice_mode(live_voice_mode)
    live_is_loopback_voice_mode = _is_loopback_voice_mode(live_voice_mode)
    live_effective_loopback = (
        (not live_is_tab_media_voice_mode)
        and (live_is_loopback_voice_mode or str(live_snapshot.get("voice_capture_mode") or "").strip().lower() == "loopback")
    )
    live_voice_diag = _get_voice_diag(assistant)
    live_browser_name = _infer_browser_name(voice_diag=live_voice_diag, launch_cmd=live_launch.get("primary", ""))
    if live_is_python_voice_mode:
        if live_is_tab_media_voice_mode:
            live_mic_guide = "当前为播放器内部音频流识别（tab_media_stream）：不录屏、不录麦，直接识别页面播放器音频。"
        elif live_effective_loopback:
            live_mic_guide = "当前为系统回采 ASR 模式：请将浏览器声音路由到回采输入设备（如 BlackHole/Stereo Mix/VB-CABLE）。"
        else:
            live_mic_guide = "当前为 Python 本地 ASR 模式：仅需系统麦克风权限，不依赖 TikTok 网页站点权限。"
    else:
        live_mic_guide = get_microphone_permission_guide(browser_name=live_browser_name)

    cloud_test_status = _get_cloud_asr_test_status(assistant)
    cloud_test_enabled = bool(cloud_test_status.get("enabled"))
    # 主监听停止时，补一条“识别-only”轮询，保证 ASR 调试可独立观察结果。
    if (not bool(live_snapshot.get("running"))) and (not cloud_test_enabled):
        if hasattr(assistant, "poll_voice_inputs_when_stopped"):
            try:
                assistant.poll_voice_inputs_when_stopped(limit=8)
            except Exception:
                pass
            live_snapshot = _get_status_snapshot(assistant)

    recent_voice_items = _get_recent_voice_inputs(assistant, limit=1)
    latest = recent_voice_items[0] if recent_voice_items else {
        "text": "-",
        "lang": "-",
        "status": "idle",
        "note": "no_input",
        "command": "",
    }

    st.caption(
        f"统一语言: {live_snapshot['language']} | Voice Error: {live_snapshot['voice_error']} | "
        f"Start Error: {live_snapshot['start_error']}"
    )
    st.caption(
        f"运行策略: local_first={'on' if bool(getattr(settings, 'LOCAL_FIRST_MODE', False)) else 'off'} "
        f"| llm_remote={'on' if bool(getattr(settings, 'LLM_REMOTE_ENABLED', True)) else 'off'}"
    )
    langs = live_snapshot.get("voice_langs") or []
    lang_chain = ",".join(langs) if langs else "-"
    runtime_chain = ",".join(list(live_snapshot.get("voice_runtime_chain") or [])) or "-"
    st.caption(
        f"语音模式: {live_voice_mode} | ASR配置: {live_snapshot.get('voice_provider') or 'n/a'} "
        f"| ASR运行: {live_snapshot.get('voice_runtime_provider') or '-'}({live_snapshot.get('voice_runtime_provider_type') or 'unknown'}) "
        f"| 链路: {runtime_chain} | 识别语言链: {lang_chain}"
    )
    runtime_err = str(live_snapshot.get("voice_runtime_error") or "").strip()
    if runtime_err:
        st.warning(f"ASR运行错误: {runtime_err}")
    st.caption(
        f"动作执行模式: {live_snapshot.get('operation_mode') or 'ocr_vision'} "
        f"| OCR可用: {'yes' if live_snapshot.get('operation_ocr_available') else 'no'} "
        f"| OCR引擎: {live_snapshot.get('operation_ocr_provider') or '-'} "
        f"| OCR源: {live_snapshot.get('operation_ocr_source') or '-'} "
        f"| OCR耗时: {live_snapshot.get('operation_ocr_ms') or 0}ms "
        f"| OCR行数: {live_snapshot.get('operation_ocr_lines') or 0} "
        f"| 点击驱动: {live_snapshot.get('operation_last_click_driver') or '-'} "
        f"| dom_fallback={'on' if live_snapshot.get('operation_dom_fallback_enabled') else 'off'} "
        f"| fixed_row={'on' if live_snapshot.get('operation_fixed_row_enabled') else 'off'} "
        f"| force_fixed={'on' if live_snapshot.get('operation_fixed_row_force') else 'off'} "
        f"| idx_required={'on' if live_snapshot.get('operation_fixed_row_require_index') else 'off'} "
        f"| calib_log={'on' if live_snapshot.get('operation_fixed_row_calib_log_enabled') else 'off'}"
    )
    fixed_row_last = live_snapshot.get("operation_fixed_row_last") or {}
    if isinstance(fixed_row_last, dict) and fixed_row_last:
        target = fixed_row_last.get("target") or {}
        vp = target.get("viewport") if isinstance(target, dict) else {}
        vp_txt = "-"
        if isinstance(vp, dict):
            vp_txt = f"({int(vp.get('x') or 0)},{int(vp.get('y') or 0)})"
        st.caption(
            f"固定点位最近记录: action={fixed_row_last.get('action') or '-'} "
            f"| link={fixed_row_last.get('link_index') or 0} "
            f"| source={target.get('x_source') if isinstance(target, dict) else '-'} "
            f"| point={vp_txt} "
            f"| result={fixed_row_last.get('result_reason') or '-'} "
            f"| ok={'yes' if fixed_row_last.get('ok') else 'no'}"
        )
    st.caption(
        f"拟人执行: {'on' if live_snapshot.get('human_like_enabled') else 'off'} "
        f"| delay={live_snapshot.get('human_like_delay_min_ms') or 0}-{live_snapshot.get('human_like_delay_max_ms') or 0}ms "
        f"| post={live_snapshot.get('human_like_post_delay_min_ms') or 0}-{live_snapshot.get('human_like_post_delay_max_ms') or 0}ms "
        f"| jitter={live_snapshot.get('human_like_click_jitter_px') or 0:.1f}px "
        f"| ocr_click={'physical' if live_snapshot.get('human_like_ocr_physical_click_enabled') else 'js'} "
        f"| keyboard_only={'on' if live_snapshot.get('human_like_message_keyboard_only_enabled') else 'off'} "
        f"| force_physical={'on' if live_snapshot.get('human_like_force_full_physical_chain') else 'off'} "
        f"| 累计延迟={live_snapshot.get('human_like_delay_total_ms') or 0}ms/{live_snapshot.get('human_like_delay_calls') or 0}次"
    )
    st.caption(
        f"动作摘要(最近{live_snapshot.get('human_like_recent_window') or 0}次): "
        f"success={live_snapshot.get('human_like_recent_success_pct') or 0}% "
        f"| p95={live_snapshot.get('human_like_recent_p95_ms') or 0}ms "
        f"| avg={live_snapshot.get('human_like_recent_avg_ms') or 0}ms "
        f"| avg_delay={live_snapshot.get('human_like_recent_avg_delay_ms') or 0}ms"
    )
    trace = live_snapshot.get("human_like_last_action_trace") or {}
    if isinstance(trace, dict) and trace:
        last_click_point = live_snapshot.get("operation_last_click_point") or {}
        point_text = "-"
        if isinstance(last_click_point, dict) and ("x" in last_click_point or "y" in last_click_point):
            point_text = f"({int(last_click_point.get('x') or 0)},{int(last_click_point.get('y') or 0)})"
        st.caption(
            f"最近动作: {trace.get('action') or '-'} "
            f"| ok={trace.get('ok')} "
            f"| elapsed={trace.get('elapsed_ms') or 0}ms "
            f"| delay={trace.get('delay_ms') or 0}ms/{trace.get('delay_count') or 0}次 "
            f"| click={live_snapshot.get('operation_last_click_driver') or '-'}@{point_text} "
            f"| last={trace.get('last_delay_reason') or '-'}:{trace.get('last_delay_ms') or 0}ms"
        )
    if live_snapshot.get("operation_last_click_error"):
        st.caption(f"点击回退原因: {live_snapshot.get('operation_last_click_error')}")
    st.caption(
        f"网页信息源: {live_snapshot.get('web_info_mode') or 'ocr_only'} "
        f"| OCR可用: {'yes' if live_snapshot.get('web_ocr_available') else 'no'} "
        f"| OCR页型: {live_snapshot.get('web_ocr_page_type') or '-'} "
        f"| Live态: {live_snapshot.get('web_ocr_live_phase') or ('live' if live_snapshot.get('web_ocr_live') else 'non_live')} "
        f"({float(live_snapshot.get('web_ocr_live_confidence') or 0.0):.2f}) "
        f"| OCR耗时: {live_snapshot.get('web_ocr_ms') or 0}ms "
        f"| OCR行数: {live_snapshot.get('web_ocr_lines') or 0} "
        f"| OCR弹幕: {live_snapshot.get('web_ocr_chat_count') or 0} "
        f"| OCR区块: {live_snapshot.get('web_ocr_block_count') or 0} "
        f"| 采集: {live_snapshot.get('web_capture_backend') or '-'} "
        f"| 采集耗时: {live_snapshot.get('web_capture_ms') or 0}ms"
    )
    tags = list(live_snapshot.get("web_ocr_scene_tags") or [])
    if tags:
        st.caption(f"场景标签: {', '.join(tags[:8])}")
    if live_snapshot.get("web_info_mode") in {"ocr_hybrid", "ocr_only", "screen_ocr"} and live_snapshot.get("web_ocr_error"):
        st.caption(f"网页OCR状态: {live_snapshot.get('web_ocr_error')}")
    if live_snapshot.get("web_info_mode") == "screen_ocr" and live_snapshot.get("web_capture_error"):
        st.caption(f"屏幕采集状态: {live_snapshot.get('web_capture_error')}")
    if live_snapshot.get("operation_mode") == "ocr_vision":
        ocr_err = str(live_snapshot.get("operation_ocr_error") or "").strip()
        if ocr_err:
            st.caption(f"OCR状态: {ocr_err}")
        ocr_text = _short_text(live_snapshot.get("operation_ocr_text") or "", 120)
        if ocr_text:
            st.caption(f"OCR最近文本: {ocr_text}")
    st.caption(
        f"语音采集模式: {live_snapshot.get('voice_capture_mode') or '-'} | "
        f"语音输入: device={live_snapshot.get('voice_device_name') or 'default'} "
        f"| rms={live_snapshot.get('voice_last_audio_rms') or 0} "
        f"| no_text_count={live_snapshot.get('voice_no_text_count') or 0}"
    )
    if live_snapshot.get("voice_capture_mode") == "loopback" and live_snapshot.get("voice_loopback_likely_mic"):
        st.warning("当前 loopback 模式疑似仍在使用实体麦克风，建议切到回采设备（BlackHole/Stereo Mix/VB-CABLE）。")

    latest_text = latest.get("text") or live_snapshot.get("voice_last_text") or "-"
    latest_text = _short_text(latest_text, 220)
    latest_lang = latest.get("lang") or live_snapshot.get("voice_last_text_lang") or "unknown"
    st.caption(f"最近识别(实时): {latest_text} ({latest_lang})")
    st.caption(
        f"最新输入: {latest_text} "
        f"| lang={latest_lang} "
        f"| status={latest.get('status') or '-'} "
        f"| note={latest.get('note') or '-'} "
        f"| command={latest.get('command') or '-'}"
    )
    if cloud_test_enabled:
        st.markdown("**🎧 播放器流ASR(B站)实时转写对比**")
        st.caption(
            f"running={cloud_test_status.get('running')} "
            f"| provider={cloud_test_status.get('provider') or '-'}({cloud_test_status.get('provider_type') or 'unknown'}) "
            f"| capture={cloud_test_status.get('capture_mode') or '-'} "
            f"| device={cloud_test_status.get('device_name') or '-'} "
            f"| err={cloud_test_status.get('error') or 'none'} "
            f"| asr_err={cloud_test_status.get('provider_error') or 'none'}"
        )
        test_rows = _poll_cloud_asr_test_transcripts(assistant, limit=8)
        if test_rows:
            text_lines = []
            for row in test_rows:
                text_lines.append(
                    f"[{row.get('time')}] ({row.get('lang') or '-'}) {row.get('text') or ''}"
                )
            st.code("\n".join(text_lines), language="text")
        else:
            st.caption("暂无转写结果：请在 Bilibili 页面播放音频后等待 1-2 秒。")

    if not live_snapshot["browser_connected"]:
        if live_snapshot.get("web_info_mode") == "screen_ocr":
            st.error("主链路异常：屏幕采集不可用。请检查系统录屏权限/显示器可见性。")
        else:
            st.error("主链路异常：浏览器未连接到直播标签页。")
            st.code(live_launch["primary"], language="bash")

    if live_snapshot["voice_enabled"] and not live_snapshot["running"]:
        if cloud_test_enabled:
            st.info("当前处于播放器流ASR测试模式：无需启动主监听，也不要求 TikTok 直播页。")
        elif live_snapshot.get("browser_connected"):
            st.info("浏览器已连接目标页，等待你点击“启动监听”进入运行中。")
        elif live_snapshot.get("voice_running"):
            st.info("当前仅麦克风采集在运行（测试/预热模式），主监听未启动，语音口令不会执行页面动作。请点击“启动监听”。")
        else:
            st.warning("语音口令已启用，但主监听未启动。请点击“启动监听”，或点“🧪 打开模拟网页测试”（会自动启动监听）。")

    if live_snapshot["start_error"] not in ("none", ""):
        st.warning(f"最近启动失败：{live_snapshot['start_error']}")
        if live_snapshot["start_detail"]:
            st.caption(f"启动诊断：{live_snapshot['start_detail']}")

    if live_snapshot["voice_enabled"] and live_snapshot["voice_error"] not in ("none", ""):
        st.warning(f"语音通道异常：{live_snapshot['voice_error']}")
        err = str(live_snapshot.get("voice_error") or "")
        if "tab_audio_js_no_result" in err or "browser_page_context_unavailable" in err:
            st.caption("未拿到可执行JS的浏览器页面上下文。请确认 Chrome 已用远程调试端口启动；若在 B站 ASR 测试中，请先点击“重新打开B站测试页”再重试。")
        st.caption(live_mic_guide)
    elif live_snapshot["voice_enabled"] and live_snapshot["voice_running"] and not live_snapshot.get("voice_last_result_at"):
        st.info("语音监听已运行，但尚未捕获到有效语音文本。请靠近麦克风说出完整口令再观察“最近识别”。")


def _core_health(snapshot):
    total = 3
    ok = 0
    checks = []

    browser_ok = bool(snapshot["browser_connected"])
    checks.append(("浏览器连接", browser_ok))
    ok += 1 if browser_ok else 0

    running_ok = bool(snapshot["running"])
    checks.append(("主监听运行", running_ok))
    ok += 1 if running_ok else 0

    voice_needed = bool(snapshot["voice_enabled"])
    voice_ok = (not voice_needed) or (snapshot["voice_error"] in ("none", None, ""))
    checks.append(("语音通道", voice_ok))
    ok += 1 if voice_ok else 0

    return {
        "ok": ok,
        "total": total,
        "ratio": ok / total,
        "checks": checks,
    }

# 页面样式与概览
st.markdown(
    """
    <style>
    .block-container { padding-top: 0.8rem; padding-bottom: 1rem; max-width: 1400px; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    div[data-testid="stCaptionContainer"] { word-break: break-word; }
    div[data-testid="stHorizontalBlock"] > div:has(div[data-testid="metric-container"]) {
      background: #f8fafc;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 0.25rem 0.4rem;
    }
    section[data-testid="stSidebar"] .stButton > button {
      border-radius: 10px;
      min-height: 42px;
    }
    section[data-testid="stSidebar"] .stTextArea textarea {
      line-height: 1.35;
    }
    section[data-testid="stSidebar"] [data-testid="stExpander"] {
      border-radius: 10px;
      border: 1px solid #e5e7eb;
      margin-bottom: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

snapshot = _get_status_snapshot(st.session_state.assistant)
startup_state = _get_startup_state(st.session_state.assistant)
launch_info = build_chrome_debug_commands(
    port=settings.BROWSER_PORT,
    user_data_path=settings.USER_DATA_PATH,
    chrome_executable=settings.CHROME_EXECUTABLE,
)
mock_shop_url = _get_mock_shop_url(st.session_state.assistant)
launch_info_mock = build_chrome_debug_commands(
    port=settings.BROWSER_PORT,
    user_data_path=settings.USER_DATA_PATH,
    chrome_executable=settings.CHROME_EXECUTABLE,
    startup_url=mock_shop_url,
)
mic_perm = _get_mic_permission_state(st.session_state.assistant)
voice_mode = _get_voice_mode(st.session_state.assistant)
is_python_voice_mode = _is_local_voice_mode(voice_mode)
is_tab_media_voice_mode = _is_tab_media_voice_mode(voice_mode)
is_loopback_voice_mode = _is_loopback_voice_mode(voice_mode)
effective_loopback_voice_mode = (
    (not is_tab_media_voice_mode)
    and (is_loopback_voice_mode or str(snapshot.get("voice_capture_mode") or "").strip().lower() == "loopback")
)
voice_diag = _get_voice_diag(st.session_state.assistant)
browser_name = _infer_browser_name(voice_diag=voice_diag, launch_cmd=launch_info.get("primary", ""))
if is_python_voice_mode:
    if is_tab_media_voice_mode:
        mic_guide = "当前使用播放器内部音频流识别（tab_media_stream），不依赖系统麦克风/回采设备。"
    elif effective_loopback_voice_mode:
        mic_guide = "当前为系统回采 ASR 模式：请将浏览器播放音频路由到回采输入设备。"
    else:
        mic_guide = "当前为 Python 本地 ASR 模式：仅需系统麦克风权限，不依赖 TikTok 网页站点权限。"
else:
    mic_guide = get_microphone_permission_guide(browser_name=browser_name)
health = _core_health(snapshot)
st.title("AI 直播助手控制台")
top1, top2, top3, top4, top5 = st.columns(5)
run_state = "启动中" if snapshot["starting"] else ("运行中" if snapshot["running"] else "已停止")
source_label = "屏幕源" if snapshot.get("web_info_mode") == "screen_ocr" else "浏览器"
source_ok_text = "已就绪" if snapshot.get("web_info_mode") == "screen_ocr" else "已连接"
top1.metric("运行状态", run_state)
top2.metric(source_label, source_ok_text if snapshot["browser_connected"] else "未连接")
top3.metric("LLM", "在线" if snapshot["llm_online"] else "离线")
top4.metric("语音监听", "已开启" if snapshot["voice_enabled"] else "已关闭")
top5.metric("语音状态", "running" if snapshot["voice_running"] else "stopped")
render_top_live_status_fragment()

if not snapshot["llm_online"]:
    st.info("LLM 当前离线，主功能仍可运行（关键词回复/运营动作/暖场）。")

# 侧边栏：状态与控制
with st.sidebar:
    st.title("控制中心")

    st.subheader("系统状态")
    col1, col2 = st.columns(2)
    col1.metric(source_label, source_ok_text if snapshot["browser_connected"] else "未连接")
    col2.metric("LLM", "在线" if snapshot["llm_online"] else "离线")
    if not snapshot["llm_online"]:
        st.caption("提示：在项目根目录创建 .env 并配置 LLM_API_KEY 后，点击“强制重载系统”。")

    with st.expander("🔐 LLM API（OpenAI 协议）", expanded=not snapshot["llm_online"]):
        st.caption("支持 OpenAI 兼容接口（如 OpenAI / DeepSeek / 其他兼容网关）。")
        if "llm_api_provider_ui" not in st.session_state:
            st.session_state.llm_api_provider_ui = "自定义"
        if "llm_api_key_ui" not in st.session_state:
            st.session_state.llm_api_key_ui = str(getattr(settings, "LLM_API_KEY", "") or "")
        if "llm_base_url_ui" not in st.session_state:
            st.session_state.llm_base_url_ui = str(getattr(settings, "LLM_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com")
        if "llm_model_name_ui" not in st.session_state:
            st.session_state.llm_model_name_ui = str(getattr(settings, "LLM_MODEL_NAME", "deepseek-chat") or "deepseek-chat")
        if "llm_remote_enabled_ui" not in st.session_state:
            st.session_state.llm_remote_enabled_ui = bool(getattr(settings, "LLM_REMOTE_ENABLED", True))

        provider_options = ["自定义", "OpenAI 官方", "DeepSeek"]
        st.selectbox("服务商预设", options=provider_options, key="llm_api_provider_ui")
        if st.button("应用预设", key="btn_apply_llm_provider_preset", use_container_width=True):
            preset = st.session_state.llm_api_provider_ui
            if preset == "OpenAI 官方":
                st.session_state.llm_base_url_ui = "https://api.openai.com/v1"
                st.session_state.llm_model_name_ui = "gpt-4o-mini"
            elif preset == "DeepSeek":
                st.session_state.llm_base_url_ui = "https://api.deepseek.com"
                st.session_state.llm_model_name_ui = "deepseek-chat"
            st.rerun()

        with st.form("llm_api_config_form", clear_on_submit=False):
            st.text_input("LLM_API_KEY", key="llm_api_key_ui", type="password", placeholder="sk-...")
            st.text_input("LLM_BASE_URL", key="llm_base_url_ui", placeholder="https://api.openai.com/v1")
            st.text_input("LLM_MODEL_NAME", key="llm_model_name_ui", placeholder="gpt-4o-mini")
            st.checkbox("启用远端 LLM（LLM_REMOTE_ENABLED）", key="llm_remote_enabled_ui")
            save_llm_cfg = st.form_submit_button("💾 保存到 .env 并重载系统", use_container_width=True)

        if save_llm_cfg:
            api_key = str(st.session_state.llm_api_key_ui or "").strip()
            base_url = str(st.session_state.llm_base_url_ui or "").strip()
            model_name = str(st.session_state.llm_model_name_ui or "").strip()
            remote_enabled = bool(st.session_state.llm_remote_enabled_ui)

            if remote_enabled and not api_key:
                st.error("已启用远端 LLM，请先填写 LLM_API_KEY。")
            elif not base_url:
                st.error("请填写 LLM_BASE_URL。")
            elif not model_name:
                st.error("请填写 LLM_MODEL_NAME。")
            else:
                updates = {
                    "LLM_API_KEY": api_key,
                    "LLM_BASE_URL": base_url,
                    "LLM_MODEL_NAME": model_name,
                    "LLM_REMOTE_ENABLED": "true" if remote_enabled else "false",
                }
                env_path = _save_env_values(updates)
                os.environ.update(updates)
                _reload_runtime_settings()
                _reset_assistant_for_rerun()
                st.success(f"已写入配置：{env_path}")
                st.rerun()

    with st.expander("🎙️ 阿里云 ASR API 配置", expanded=False):
        st.caption("用于云端语音识别（FunASR）。在下方 `ASR Provider` 中可直接选择 `dashscope_funasr` 或 `hybrid_local_cloud`。")
        if "voice_dashscope_api_key_ui" not in st.session_state:
            st.session_state.voice_dashscope_api_key_ui = str(getattr(settings, "VOICE_DASHSCOPE_API_KEY", "") or "")
        if "voice_dashscope_model_ui" not in st.session_state:
            st.session_state.voice_dashscope_model_ui = str(getattr(settings, "VOICE_DASHSCOPE_MODEL", "paraformer-realtime-v2") or "paraformer-realtime-v2")
        if "voice_dashscope_sample_rate_ui" not in st.session_state:
            st.session_state.voice_dashscope_sample_rate_ui = int(getattr(settings, "VOICE_DASHSCOPE_SAMPLE_RATE", 16000) or 16000)
        if "voice_dashscope_lang_hints_ui" not in st.session_state:
            st.session_state.voice_dashscope_lang_hints_ui = ",".join(list(getattr(settings, "VOICE_DASHSCOPE_LANGUAGE_HINTS", []) or []))
        if "voice_dashscope_base_ws_ui" not in st.session_state:
            st.session_state.voice_dashscope_base_ws_ui = str(getattr(settings, "VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL", "") or "")
        if "voice_dashscope_punctuation_ui" not in st.session_state:
            st.session_state.voice_dashscope_punctuation_ui = bool(getattr(settings, "VOICE_DASHSCOPE_ENABLE_PUNCTUATION", True))
        if "voice_dashscope_disable_itn_ui" not in st.session_state:
            st.session_state.voice_dashscope_disable_itn_ui = bool(getattr(settings, "VOICE_DASHSCOPE_DISABLE_ITN", False))

        with st.form("dashscope_asr_config_form", clear_on_submit=False):
            st.text_input("VOICE_DASHSCOPE_API_KEY", key="voice_dashscope_api_key_ui", type="password", placeholder="sk-...")
            st.text_input("VOICE_DASHSCOPE_MODEL", key="voice_dashscope_model_ui", placeholder="paraformer-realtime-v2")
            st.number_input(
                "VOICE_DASHSCOPE_SAMPLE_RATE",
                min_value=8000,
                max_value=48000,
                step=1000,
                key="voice_dashscope_sample_rate_ui",
            )
            st.text_input("VOICE_DASHSCOPE_LANGUAGE_HINTS（逗号分隔）", key="voice_dashscope_lang_hints_ui", placeholder="zh,en")
            st.text_input(
                "VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL（可选）",
                key="voice_dashscope_base_ws_ui",
                placeholder="wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
            )
            st.checkbox("开启语义标点（VOICE_DASHSCOPE_ENABLE_PUNCTUATION）", key="voice_dashscope_punctuation_ui")
            st.checkbox("禁用 ITN（VOICE_DASHSCOPE_DISABLE_ITN）", key="voice_dashscope_disable_itn_ui")
            save_dashscope_cfg = st.form_submit_button("💾 保存阿里云 ASR 配置并重载系统", use_container_width=True)

        if save_dashscope_cfg:
            api_key = str(st.session_state.voice_dashscope_api_key_ui or "").strip()
            model_name = str(st.session_state.voice_dashscope_model_ui or "").strip()
            sample_rate = int(st.session_state.voice_dashscope_sample_rate_ui or 16000)
            lang_hints = str(st.session_state.voice_dashscope_lang_hints_ui or "").strip()
            base_ws = str(st.session_state.voice_dashscope_base_ws_ui or "").strip()
            punctuation_enabled = bool(st.session_state.voice_dashscope_punctuation_ui)
            disable_itn = bool(st.session_state.voice_dashscope_disable_itn_ui)

            if not model_name:
                st.error("请填写 VOICE_DASHSCOPE_MODEL。")
            else:
                updates = {
                    "VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK": "false",
                    "VOICE_DASHSCOPE_API_KEY": api_key,
                    "DASHSCOPE_API_KEY": api_key,
                    "VOICE_DASHSCOPE_MODEL": model_name,
                    "VOICE_DASHSCOPE_SAMPLE_RATE": str(sample_rate),
                    "VOICE_DASHSCOPE_LANGUAGE_HINTS": lang_hints,
                    "VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL": base_ws,
                    "VOICE_DASHSCOPE_ENABLE_PUNCTUATION": "true" if punctuation_enabled else "false",
                    "VOICE_DASHSCOPE_DISABLE_ITN": "true" if disable_itn else "false",
                }
                env_path = _save_env_values(updates)
                os.environ.update(updates)
                _reload_runtime_settings()
                _reset_assistant_for_rerun()
                st.success(f"阿里云 ASR 配置已写入：{env_path}（请在 ASR Provider 里显式选择云端或混合模式）")
                st.rerun()

    with st.expander("统一语言与回复设置", expanded=True):
        st.caption("输出强约束：所有自动回复/暖场/知识库问答都只按“统一语言”输出。")
        st.caption("输入兼容：知识库内容与语气模板可中英文混写，系统会提取语义和风格后统一输出。")
        lang_options = list(settings.REPLY_LANGUAGES.keys())
        code_to_label = {v: k for k, v in settings.REPLY_LANGUAGES.items()}
        operation_mode_labels = {
            "OCR视觉模式（默认，禁DOM兜底）": "ocr_vision",
            "DOM执行模式（仅显式启用）": "dom",
        }
        operation_mode_to_label = {v: k for k, v in operation_mode_labels.items()}
        web_info_mode_labels = {
            "严格OCR信息源（默认，不读取DOM文本）": "ocr_only",
            "纯屏幕OCR信息源（完全无DOM）": "screen_ocr",
            "OCR优先信息源（兼容）": "ocr_hybrid",
            "DOM信息源（仅显式启用）": "dom",
        }
        web_info_mode_to_label = {v: k for k, v in web_info_mode_labels.items()}
        current_label = code_to_label.get(
            _get_unified_language(st.session_state.assistant),
            lang_options[0]
        )
        current_operation_mode = _get_operation_execution_mode(st.session_state.assistant)
        current_operation_mode_label = operation_mode_to_label.get(current_operation_mode, "OCR视觉模式（默认，禁DOM兜底）")
        current_web_info_mode = _get_web_info_source_mode(st.session_state.assistant)
        current_web_info_mode_label = web_info_mode_to_label.get(current_web_info_mode, "严格OCR信息源（默认，不读取DOM文本）")

        if "reply_lang_label" not in st.session_state:
            st.session_state.reply_lang_label = current_label
        if st.session_state.reply_lang_label not in lang_options:
            st.session_state.reply_lang_label = lang_options[0]

        st.selectbox("统一语言（回复/暖场/知识库问答）", options=lang_options, key="reply_lang_label")
        if "operation_execution_mode_label" not in st.session_state:
            st.session_state.operation_execution_mode_label = current_operation_mode_label
        if st.session_state.operation_execution_mode_label not in operation_mode_labels:
            st.session_state.operation_execution_mode_label = "OCR视觉模式（默认，禁DOM兜底）"
        st.selectbox(
            "页面操作模式（置顶/秒杀等动作）",
            options=list(operation_mode_labels.keys()),
            key="operation_execution_mode_label",
            help="默认 OCR视觉模式不启用 DOM 兜底；只有你显式选择 DOM 模式时才会启用 DOM 执行链路。",
        )
        mode_preview = operation_mode_labels.get(st.session_state.operation_execution_mode_label, "ocr_vision")
        if mode_preview == "ocr_vision":
            st.caption("OCR视觉模式：优先 OCR 锚点点击，默认不走 DOM 兜底。")
        elif mode_preview == "dom":
            st.caption("DOM 执行模式已显式启用：系统将允许 DOM 执行与回执链路。")
        if "web_info_source_mode_label" not in st.session_state:
            st.session_state.web_info_source_mode_label = current_web_info_mode_label
        if st.session_state.web_info_source_mode_label not in web_info_mode_labels:
            st.session_state.web_info_source_mode_label = "严格OCR信息源（默认，不读取DOM文本）"
        st.selectbox(
            "网页信息读取方式",
            options=list(web_info_mode_labels.keys()),
            key="web_info_source_mode_label",
            help="纯屏幕OCR信息源：页面类型/弹幕抽取/动作回执都仅基于屏幕OCR文本+图片特征，不读取DOM文本。",
        )
        web_mode_preview = web_info_mode_labels.get(st.session_state.web_info_source_mode_label, "ocr_only")
        if web_mode_preview == "ocr_only":
            st.caption("严格OCR信息源已选：建议同时使用 OCR视觉模式，以获得完整 OCR 操作链路。")
        elif web_mode_preview == "screen_ocr":
            st.caption("纯屏幕OCR信息源已选：系统完全不读取浏览器DOM文本，所有信息来自屏幕识别。")
        elif web_mode_preview == "dom":
            st.caption("DOM 信息源已显式启用：仅在你需要兼容旧链路时建议使用。")

        if "tone_template_input" not in st.session_state:
            st.session_state.tone_template_input = st.session_state.assistant.tone_template
        st.text_area(
            "语气模板（可手写）",
            key="tone_template_input",
            height=96,
            placeholder="示例(中英均可)：像闺蜜推荐一样，热情但不过度推销；confident but not pushy, end with one emoji."
        )
        if "voice_command_enabled_ui" not in st.session_state:
            st.session_state.voice_command_enabled_ui = _get_voice_command_enabled(st.session_state.assistant)
        if "reply_enabled_ui" not in st.session_state:
            st.session_state.reply_enabled_ui = _get_reply_enabled(st.session_state.assistant)
        if "proactive_enabled_ui" not in st.session_state:
            st.session_state.proactive_enabled_ui = _get_proactive_enabled(st.session_state.assistant)
        c_toggle_1, c_toggle_2 = st.columns(2)
        with c_toggle_1:
            st.checkbox(
                "启用自动回复弹幕",
                key="reply_enabled_ui",
                help="关闭后系统仍监听弹幕和执行运营动作，但不会自动发送回复。"
            )
            st.checkbox(
                "启用自动暖场话术",
                key="proactive_enabled_ui",
                help="关闭后系统不会主动发送暖场文案。"
            )
        with c_toggle_2:
            st.checkbox(
                "启用语音口令监听（物理听到主播说话）",
                key="voice_command_enabled_ui",
                help="支持 Python 本地 ASR（麦克风/系统回采）与浏览器 Web Speech；开启后可识别“将3号链接置顶一下”“将秒杀活动上架一下”。"
            )

        current_human_cfg = _get_human_like_settings(st.session_state.assistant)
        if "human_like_enabled_ui" not in st.session_state:
            st.session_state.human_like_enabled_ui = bool(current_human_cfg.get("enabled", True))
        if "human_like_delay_min_ms_ui" not in st.session_state:
            st.session_state.human_like_delay_min_ms_ui = int(float(current_human_cfg.get("delay_min_seconds", 0.04) or 0.04) * 1000)
        if "human_like_delay_max_ms_ui" not in st.session_state:
            st.session_state.human_like_delay_max_ms_ui = int(float(current_human_cfg.get("delay_max_seconds", 0.20) or 0.20) * 1000)
        if "human_like_post_delay_min_ms_ui" not in st.session_state:
            st.session_state.human_like_post_delay_min_ms_ui = int(float(current_human_cfg.get("post_delay_min_seconds", 0.03) or 0.03) * 1000)
        if "human_like_post_delay_max_ms_ui" not in st.session_state:
            st.session_state.human_like_post_delay_max_ms_ui = int(float(current_human_cfg.get("post_delay_max_seconds", 0.16) or 0.16) * 1000)
        if "human_like_click_jitter_px_ui" not in st.session_state:
            st.session_state.human_like_click_jitter_px_ui = float(current_human_cfg.get("click_jitter_px", 1.8) or 1.8)
        if "ocr_physical_click_enabled_ui" not in st.session_state:
            st.session_state.ocr_physical_click_enabled_ui = bool(current_human_cfg.get("ocr_physical_click_enabled", True))
        if "ocr_vision_allow_dom_fallback_ui" not in st.session_state:
            st.session_state.ocr_vision_allow_dom_fallback_ui = bool(current_human_cfg.get("ocr_vision_allow_dom_fallback", False))
        if "os_keyboard_fallback_enabled_ui" not in st.session_state:
            st.session_state.os_keyboard_fallback_enabled_ui = bool(current_human_cfg.get("keyboard_fallback_enabled", False))
        if "message_keyboard_only_enabled_ui" not in st.session_state:
            st.session_state.message_keyboard_only_enabled_ui = bool(current_human_cfg.get("message_keyboard_only_enabled", True))
        if "force_full_physical_chain_ui" not in st.session_state:
            st.session_state.force_full_physical_chain_ui = bool(current_human_cfg.get("force_full_physical_chain", False))
        if "pin_click_test_confirm_popup_ui" not in st.session_state:
            st.session_state.pin_click_test_confirm_popup_ui = bool(current_human_cfg.get("pin_click_test_confirm_popup", False))
        if "os_keyboard_typing_min_ms_ui" not in st.session_state:
            st.session_state.os_keyboard_typing_min_ms_ui = int(float(current_human_cfg.get("typing_min_interval_seconds", 0.018) or 0.018) * 1000)
        if "os_keyboard_typing_max_ms_ui" not in st.session_state:
            st.session_state.os_keyboard_typing_max_ms_ui = int(float(current_human_cfg.get("typing_max_interval_seconds", 0.055) or 0.055) * 1000)

        with st.expander("真人化执行参数（键鼠）", expanded=False):
            p1, p2, p3, p4 = st.columns(4)
            if p1.button("保守预设", key="btn_human_preset_safe", use_container_width=True):
                _apply_human_preset_to_ui("保守（低风险）")
                _set_human_like_settings(st.session_state.assistant, _build_human_like_payload_from_ui())
                st.rerun()
            if p2.button("平衡预设", key="btn_human_preset_balanced", use_container_width=True):
                _apply_human_preset_to_ui("平衡（推荐）")
                _set_human_like_settings(st.session_state.assistant, _build_human_like_payload_from_ui())
                st.rerun()
            if p3.button("激进预设", key="btn_human_preset_fast", use_container_width=True):
                _apply_human_preset_to_ui("激进（速度优先）")
                _set_human_like_settings(st.session_state.assistant, _build_human_like_payload_from_ui())
                st.rerun()
            if p4.button("全链路物理预设", key="btn_human_preset_physical", use_container_width=True):
                _apply_human_preset_to_ui("强制全链路物理键鼠")
                st.session_state.operation_execution_mode_label = "OCR视觉模式（默认，禁DOM兜底）"
                st.session_state.web_info_source_mode_label = "纯屏幕OCR信息源（完全无DOM）"
                _set_human_like_settings(st.session_state.assistant, _build_human_like_payload_from_ui())
                st.rerun()
            st.checkbox(
                "启用真人化动作节奏",
                key="human_like_enabled_ui",
                help="开启后会在点击与动作链路加入随机停顿、微抖动和拟人轨迹。",
            )
            st.checkbox(
                "强制全链路物理鼠标键盘执行",
                key="force_full_physical_chain_ui",
                help="开启后：强制 OCR视觉 + screen_ocr，禁用 DOM 点击/表单提交回退，发送链路优先系统键盘。",
            )
            h1, h2 = st.columns(2)
            with h1:
                st.slider("动作前延迟最小(ms)", 0, 500, key="human_like_delay_min_ms_ui")
                st.slider("动作后延迟最小(ms)", 0, 500, key="human_like_post_delay_min_ms_ui")
                st.slider("键盘打字最小间隔(ms)", 1, 160, key="os_keyboard_typing_min_ms_ui")
            with h2:
                st.slider("动作前延迟最大(ms)", 10, 900, key="human_like_delay_max_ms_ui")
                st.slider("动作后延迟最大(ms)", 10, 900, key="human_like_post_delay_max_ms_ui")
                st.slider("键盘打字最大间隔(ms)", 1, 220, key="os_keyboard_typing_max_ms_ui")
            st.slider("鼠标点击抖动(px)", 0.0, 6.0, key="human_like_click_jitter_px_ui", step=0.1)
            st.checkbox(
                "OCR视觉模式优先物理鼠标点击",
                key="ocr_physical_click_enabled_ui",
                help="开启后在 OCR 视觉命中时优先走系统鼠标真实移动点击；失败才回退 JS 点击。",
            )
            st.checkbox(
                "允许 OCR视觉模式回退到 DOM（不推荐）",
                key="ocr_vision_allow_dom_fallback_ui",
                help="默认关闭。关闭后未显式选择 DOM 时不会启用 DOM 回退链路。",
            )
            st.checkbox(
                "置顶点击测试：点击后结果提示",
                key="pin_click_test_confirm_popup_ui",
                help="仅对 pin/unpin 固定点位点击生效。开启后，点击后显示小提示；若耗时超阈值会提示失败原因。",
            )
            st.checkbox(
                "启用系统键盘输入兜底",
                key="os_keyboard_fallback_enabled_ui",
                help="仅在 DOM 输入失败时回退为系统逐字符输入；在极端页面抖动场景更稳。",
            )

        if st.button("✅ 应用回复设置", use_container_width=True):
            selected_lang = settings.REPLY_LANGUAGES.get(
                st.session_state.reply_lang_label,
                settings.DEFAULT_REPLY_LANGUAGE
            )
            force_full_physical = bool(st.session_state.get("force_full_physical_chain_ui", False))
            target_operation_mode = operation_mode_labels.get(
                st.session_state.operation_execution_mode_label,
                "ocr_vision",
            )
            target_web_info_mode = web_info_mode_labels.get(
                st.session_state.web_info_source_mode_label,
                "ocr_only",
            )
            if force_full_physical:
                target_operation_mode = "ocr_vision"
                target_web_info_mode = "screen_ocr"
            st.session_state.assistant.update_reply_settings(
                language=selected_lang,
                tone_template=st.session_state.tone_template_input
            )
            _set_reply_enabled(st.session_state.assistant, st.session_state.reply_enabled_ui)
            _set_proactive_enabled(st.session_state.assistant, st.session_state.proactive_enabled_ui)
            _set_voice_command_enabled(st.session_state.assistant, st.session_state.voice_command_enabled_ui)
            _set_operation_execution_mode(
                st.session_state.assistant,
                target_operation_mode,
            )
            _set_web_info_source_mode(
                st.session_state.assistant,
                target_web_info_mode,
            )
            _set_human_like_settings(st.session_state.assistant, _build_human_like_payload_from_ui())
            st.success("回复设置已应用")
            st.rerun()

    with st.expander("运行控制", expanded=True):
        quick_cfg = _get_human_like_settings(st.session_state.assistant)
        if "pin_click_test_confirm_popup_quick_ui" not in st.session_state:
            st.session_state.pin_click_test_confirm_popup_quick_ui = bool(quick_cfg.get("pin_click_test_confirm_popup", False))
        if "pin_click_test_confirm_popup_quick_prev_ui" not in st.session_state:
            st.session_state.pin_click_test_confirm_popup_quick_prev_ui = bool(st.session_state.pin_click_test_confirm_popup_quick_ui)
        st.toggle(
            "🧪 置顶点击测试弹窗（即时）",
            key="pin_click_test_confirm_popup_quick_ui",
            help="开启后，pin/unpin 在点击后显示结果提示；若耗时超阈值会提示失败原因。该开关即时生效。",
        )
        quick_now = bool(st.session_state.pin_click_test_confirm_popup_quick_ui)
        quick_prev = bool(st.session_state.pin_click_test_confirm_popup_quick_prev_ui)
        if quick_now != quick_prev:
            _set_human_like_settings(st.session_state.assistant, {"pin_click_test_confirm_popup": quick_now})
            st.session_state.pin_click_test_confirm_popup_ui = quick_now
            st.session_state.pin_click_test_confirm_popup_quick_prev_ui = quick_now
            st.success(f"置顶点击测试弹窗已{'开启' if quick_now else '关闭'}")
            st.rerun()

        if st.session_state.assistant.is_running:
            if st.button("停止监听", use_container_width=True):
                st.session_state.assistant.stop()
                st.rerun()
        else:
            startup_state = _get_startup_state(st.session_state.assistant)
            if st.button(
                "启动监听",
                use_container_width=True,
                disabled=bool(startup_state.get("is_starting")),
            ):
                with st.spinner("正在启动系统并连接直播标签页..."):
                    started = st.session_state.assistant.start()
                if started:
                    st.success("系统启动成功")
                    st.rerun()
                else:
                    state = _get_startup_state(st.session_state.assistant)
                    err = state.get("last_start_error") or "unknown"
                    detail = state.get("last_start_detail") or ""
                    st.error(f"启动失败：{err}")
                    if detail:
                        st.caption(f"诊断信息：{detail}")

        if st.button("🧪 打开模拟网页测试", key="btn_sidebar_connect_mock", use_container_width=True):
            if hasattr(st.session_state.assistant, "connect_mock_shop") and st.session_state.assistant.connect_mock_shop():
                auto_started = True
                if not bool(getattr(st.session_state.assistant, "is_running", False)):
                    auto_started = bool(st.session_state.assistant.start())
                if auto_started:
                    st.success("已连接内置测试网页（Mock），并已启动监听。")
                else:
                    st.warning("已连接内置测试网页（Mock），但自动启动监听失败，请手动点击“启动监听”。")
                st.rerun()
            else:
                state = _get_startup_state(st.session_state.assistant)
                err = state.get("last_start_error") or "unknown"
                detail = state.get("last_start_detail") or ""
                st.error(f"连接内置测试网页失败：{err}")
                if detail:
                    st.caption(f"诊断信息：{detail}")
                st.code(launch_info_mock["primary"], language="bash")

        st.markdown("---")
        st.caption("🎧 播放器流ASR测试（Bilibili）")
        st.caption("该测试可独立运行：不依赖主监听，也不要求当前是 TikTok 页面。")
        # Streamlit 限制：widget key 在实例化后不可在同轮脚本内修改。
        # 若上一轮开启失败，这里在控件创建前执行重置。
        if st.session_state.get("cloud_asr_bili_test_force_reset_pending"):
            st.session_state.cloud_asr_bili_test_enabled_ui = False
            st.session_state.cloud_asr_bili_test_prev_ui = False
            st.session_state.cloud_asr_bili_test_force_reset_pending = False

        st.text_input(
            "B站测试URL",
            key="cloud_asr_bili_test_url_ui",
            help="开启开关后会打开该页面；请在页面中播放视频/直播，系统将直接读取播放器内部音频流并做ASR。",
        )
        st.selectbox(
            "测试ASR Provider",
            options=["follow_current", "whisper_local", "dashscope_funasr", "hybrid_local_cloud", "auto", "google", "sphinx"],
            key="cloud_asr_bili_test_provider_ui",
            format_func=lambda x: "follow_current（跟随当前配置）" if x == "follow_current" else x,
            help="本地/云端都走同一条播放器抓流链路，仅切换识别模型。",
        )
        st.toggle(
            "开启播放器流ASR测试开关",
            key="cloud_asr_bili_test_enabled_ui",
            help="走浏览器内媒体流识别（不录屏、不录麦），用于本地/云端 ASR 对比测试。",
        )
        cloud_asr_last_error = str(st.session_state.get("cloud_asr_bili_test_last_error") or "").strip()
        if cloud_asr_last_error:
            if any(
                k in cloud_asr_last_error
                for k in [
                    "capture_stream_unavailable",
                    "media_element_not_found",
                    "media_stream_no_audio_track",
                    "tab_audio_js_no_result",
                    "browser_page_context_unavailable",
                ]
            ):
                st.error("开启失败：未能从当前页面播放器抓到音频流。")
                st.caption("请确认 Bilibili 页面正在播放视频/直播（不是静止页）且页面未关闭，然后点击“重新打开B站测试页”再试。")
            else:
                st.error(f"开启播放器流ASR测试失败：{cloud_asr_last_error}")
            st.session_state.cloud_asr_bili_test_last_error = ""
        current_cloud_test = bool(st.session_state.cloud_asr_bili_test_enabled_ui)
        prev_cloud_test = bool(st.session_state.cloud_asr_bili_test_prev_ui)
        if current_cloud_test != prev_cloud_test:
            need_rerun = False
            if current_cloud_test:
                if hasattr(st.session_state.assistant, "start_cloud_asr_bilibili_test"):
                    selected_provider = st.session_state.cloud_asr_bili_test_provider_ui
                    result = st.session_state.assistant.start_cloud_asr_bilibili_test(
                        url=st.session_state.cloud_asr_bili_test_url_ui,
                        provider=(None if selected_provider == "follow_current" else selected_provider),
                    )
                else:
                    result = {"ok": False, "error": "assistant_method_missing"}
                if result.get("ok"):
                    st.success("播放器流ASR测试已开启：Bilibili 页面已打开，开始播放音频即可。")
                    st.session_state.cloud_asr_bili_test_prev_ui = True
                    need_rerun = True
                else:
                    st.session_state.cloud_asr_bili_test_force_reset_pending = True
                    st.session_state.cloud_asr_bili_test_prev_ui = False
                    st.session_state.cloud_asr_bili_test_last_error = str(result.get("error") or "unknown")
                    need_rerun = True
            else:
                if hasattr(st.session_state.assistant, "stop_cloud_asr_bilibili_test"):
                    result = st.session_state.assistant.stop_cloud_asr_bilibili_test(restore_previous=True)
                else:
                    result = {"ok": True}
                st.session_state.cloud_asr_bili_test_prev_ui = False
                if result.get("ok"):
                    st.info("播放器流ASR测试已关闭。")
                    need_rerun = True
                else:
                    st.error(f"关闭播放器流ASR测试失败：{result.get('error') or 'unknown'}")
            if need_rerun:
                st.rerun()

        if st.session_state.cloud_asr_bili_test_enabled_ui:
            if st.button("🔄 重新打开B站测试页", key="btn_reopen_cloud_asr_bili", use_container_width=True):
                if hasattr(st.session_state.assistant, "start_cloud_asr_bilibili_test"):
                    selected_provider = st.session_state.cloud_asr_bili_test_provider_ui
                    result = st.session_state.assistant.start_cloud_asr_bilibili_test(
                        url=st.session_state.cloud_asr_bili_test_url_ui,
                        provider=(None if selected_provider == "follow_current" else selected_provider),
                    )
                else:
                    result = {"ok": False, "error": "assistant_method_missing"}
                if result.get("ok"):
                    st.success("已重新打开B站测试页。")
                else:
                    err = str(result.get("error") or "unknown")
                    if any(k in err for k in [
                        "capture_stream_unavailable",
                        "media_element_not_found",
                        "media_stream_no_audio_track",
                        "tab_audio_js_no_result",
                        "browser_page_context_unavailable",
                    ]):
                        st.error("重开失败：页面播放器音频流不可用。")
                        st.caption("请先在 Bilibili 页面点击播放，再重试。")
                    else:
                        st.error(f"重开失败：{err}")

        if st.button("🧪 运行系统自检", key="btn_self_test_sidebar", use_container_width=True):
            with st.spinner("正在执行自检..."):
                st.session_state.self_test_results = _run_system_self_check(st.session_state.assistant)
            st.success("自检已完成")

        if st.button("📘 使用说明书", key="btn_toggle_user_guide", use_container_width=True):
            st.session_state.show_user_guide = not st.session_state.show_user_guide
            st.rerun()

        if st.button("🎙️ 申请音频输入权限", key="btn_request_mic_permission", use_container_width=True):
            result = _request_mic_permission(st.session_state.assistant)
            status = result.get("status", "unknown")
            if status == "requesting":
                st.info("已触发权限申请，请在浏览器弹窗中点击允许。")
            elif status == "granted":
                if is_tab_media_voice_mode:
                    st.success("当前链路为播放器流识别：无需麦克风权限，页面播放器音频即可识别。")
                else:
                    st.success("麦克风权限已允许。")
            elif status == "wrong_page":
                st.error("当前连接页面不是 TikTok 直播间页，请先切到 `https://www.tiktok.com/@xxx/live` 后再申请。")
                page_title = result.get("page_title") or ""
                page_url = result.get("page_url") or ""
                if page_title or page_url:
                    st.caption(f"当前页面: {page_title or '-'} | {page_url or '-'}")
            elif status == "needs_in_tab_click":
                st.warning("当前被浏览器快速拒绝，已在直播页面右上角放置 `Enable Mic` 按钮。请切到直播页手动点一次。")
                st.caption("点完后再回控制台点一次“申请音频输入权限”，状态应变为 granted。")
            elif status == "denied":
                st.warning(f"麦克风权限被拒绝: {result.get('error')}")
                perm_state = result.get("permissionState")
                if perm_state:
                    st.caption(f"浏览器站点权限状态: {perm_state}")
                if is_python_voice_mode:
                    if effective_loopback_voice_mode:
                        st.caption("请在系统音频设置中确认“浏览器输出 -> 回采输入设备”链路已建立，然后重试。")
                    else:
                        st.caption("请在系统设置中允许当前运行程序访问麦克风，然后重新点击“申请音频输入权限”。")
                else:
                    st.caption(f"请在 {browser_name} 地址栏左侧锁形图标 -> 站点权限 -> 麦克风 -> 允许。")
                    settings_hint = _browser_mic_settings_hint(browser_name)
                    if settings_hint:
                        st.caption(f"或直接打开 {settings_hint}，确保 TikTok 不是“阻止”。")
            elif status == "no_page":
                st.error("浏览器未连接到直播页，请先在“视觉调试”里连接浏览器。")
            elif status == "unsupported_context":
                st.error("当前页面不是安全上下文，无法申请麦克风。请确保在 https 的 TikTok 直播页面。")
            elif status == "unsupported":
                err = result.get("error") or ""
                if err == "missing_pyaudio":
                    st.error("本地 ASR 缺少 PyAudio 依赖，请先安装后再试。")
                elif err == "missing_speech_recognition":
                    st.error("本地 ASR 缺少 SpeechRecognition 依赖，请先安装后再试。")
                else:
                    st.error(f"当前环境不支持麦克风采集：{err}")
                if is_python_voice_mode:
                    st.caption("本地 ASR 依赖安装命令：")
                    for cmd in get_python_asr_install_guide():
                        st.code(cmd, language="bash")
            elif status == "error" and is_python_voice_mode:
                err_msg = str(result.get("error") or "unknown")
                if "loopback_device_required_for_dashscope_cloud_asr" in err_msg:
                    st.error("DashScope 仅云上模式需要系统回采设备：未检测到可用回采输入。")
                else:
                    st.error(f"本地音频输入初始化失败：{err_msg}")
                if effective_loopback_voice_mode:
                    st.caption("请检查系统是否存在可用回采输入设备（BlackHole/Stereo Mix/VB-CABLE）。")
                    st.caption("也可在 .env 指定设备：VOICE_LOOPBACK_DEVICE_INDEX=0（按设备列表调整）")
                else:
                    st.caption("请检查系统是否存在可用输入设备，并设置默认麦克风。")
                    st.caption("也可在 .env 指定设备：VOICE_PYTHON_MIC_DEVICE_INDEX=0（按设备列表调整）")
            else:
                details = result.get("details")
                page_title = result.get("page_title") or ""
                page_url = result.get("page_url") or ""
                extra = f" | details={details}" if details else ""
                st.warning(f"权限申请状态: {status} {result.get('error') or ''}{extra}")
                if page_title or page_url:
                    st.caption(f"当前页面: {page_title or '-'} | {page_url or '-'}")

        if is_python_voice_mode:
            st.markdown("---")
            if is_tab_media_voice_mode:
                st.caption("🎛️ 播放器内音频流调试（Tab Media ASR）")
            elif effective_loopback_voice_mode:
                st.caption("🎛️ 本地音频回采调试（Loopback ASR）")
            else:
                st.caption("🎛️ 本地麦克风调试（Python ASR）")

            mode_options = ["tab_audio_asr", "python_asr", "system_loopback_asr", "web_speech"]
            mode_labels = {
                "tab_audio_asr": "tab_audio_asr（推荐：播放器内部音频流）",
                "python_asr": "python_asr（本地麦克风）",
                "system_loopback_asr": "system_loopback_asr（系统回采设备）",
                "web_speech": "web_speech（浏览器WebSpeech）",
            }
            if "voice_input_mode_ui" not in st.session_state:
                st.session_state.voice_input_mode_ui = voice_mode if voice_mode in mode_options else "tab_audio_asr"
            if st.session_state.voice_input_mode_ui not in mode_options:
                st.session_state.voice_input_mode_ui = "tab_audio_asr"
            st.selectbox(
                "语音输入链路",
                options=mode_options,
                format_func=lambda x: mode_labels.get(x, x),
                key="voice_input_mode_ui",
                help="tab_audio_asr：直接读取页面播放器音频流（不录屏、不录麦）。",
            )
            if st.button("应用语音输入链路", key="btn_apply_voice_input_mode", use_container_width=True):
                if hasattr(st.session_state.assistant, "set_voice_input_mode") and st.session_state.assistant.set_voice_input_mode(st.session_state.voice_input_mode_ui):
                    st.success(f"语音输入链路已切换为 {st.session_state.voice_input_mode_ui}")
                    st.rerun()
                else:
                    st.error("切换语音输入链路失败")

            provider_options = ["whisper_local", "dashscope_funasr", "hybrid_local_cloud", "auto", "google", "sphinx"]
            if "voice_asr_provider_ui" not in st.session_state:
                st.session_state.voice_asr_provider_ui = str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "whisper_local") or "whisper_local")
            provider_aliases = {
                "dashscope": "dashscope_funasr",
                "aliyun_funasr": "dashscope_funasr",
                "funasr": "dashscope_funasr",
                "hybrid": "hybrid_local_cloud",
                "local_cloud": "hybrid_local_cloud",
                "cloud_local": "hybrid_local_cloud",
            }
            st.session_state.voice_asr_provider_ui = provider_aliases.get(
                st.session_state.voice_asr_provider_ui,
                st.session_state.voice_asr_provider_ui,
            )
            if st.session_state.voice_asr_provider_ui not in provider_options:
                st.session_state.voice_asr_provider_ui = "whisper_local"
            st.selectbox(
                "ASR Provider",
                options=provider_options,
                key="voice_asr_provider_ui",
                help="本地/云端 provider 都可配合 tab_audio_asr 使用（同样走播放器内部音频流）。"
            )
            dashscope_force_loopback = bool(getattr(settings, "VOICE_DASHSCOPE_FORCE_LOOPBACK", True))
            if st.session_state.voice_asr_provider_ui == "dashscope_funasr" and dashscope_force_loopback and (not is_tab_media_voice_mode):
                st.caption("已启用“仅云上 ASR”采集策略：使用系统回采（loopback），不走本地实体麦克风。")
            else:
                st.caption("阿里云 Key 可在左侧「🎙️ 阿里云 ASR API 配置」里配置；云端不会再被自动当作备用链路注入。")
            if st.button("应用 ASR Provider", key="btn_apply_asr_provider", use_container_width=True):
                if hasattr(st.session_state.assistant, "set_voice_asr_provider") and st.session_state.assistant.set_voice_asr_provider(st.session_state.voice_asr_provider_ui):
                    st.success(f"ASR Provider 已切换为 {st.session_state.voice_asr_provider_ui}")
                    st.rerun()
                else:
                    st.error("切换 ASR Provider 失败")

            devices = _list_python_mic_devices(st.session_state.assistant)
            if is_tab_media_voice_mode:
                st.caption("当前链路不需要本地输入设备，识别源来自页面播放器内部音频流。")
            elif not devices:
                if effective_loopback_voice_mode:
                    st.warning("未检测到可用回采输入设备。请先确认 BlackHole/Stereo Mix/VB-CABLE 等系统回采设备可用。")
                else:
                    st.warning("未检测到可用输入设备。请先在系统设置确认麦克风可用。")
            else:
                options = [-1] + [int(d.get("index", -1)) for d in devices]
                labels = {-1: "系统默认设备"}
                for d in devices:
                    idx = int(d.get("index", -1))
                    labels[idx] = f"{idx}: {d.get('name', '')}"

                selected_default = _get_python_mic_selected_index(st.session_state.assistant)
                if selected_default not in options:
                    selected_default = -1

                if "voice_mic_select_ui" not in st.session_state:
                    st.session_state.voice_mic_select_ui = selected_default
                if st.session_state.voice_mic_select_ui not in options:
                    st.session_state.voice_mic_select_ui = selected_default

                st.selectbox(
                    "输入设备",
                    options=options,
                    format_func=lambda x: labels.get(x, str(x)),
                    key="voice_mic_select_ui",
                    help="建议先选“系统默认设备”，若无识别结果再切到具体设备。"
                )

                ca, cb = st.columns(2)
                with ca:
                    if st.button("应用设备并重启语音", key="btn_apply_mic_device", use_container_width=True):
                        ok = _set_python_mic_selected_index(
                            st.session_state.assistant,
                            st.session_state.voice_mic_select_ui,
                        )
                        if ok:
                            st.success("麦克风设备已应用。")
                            st.rerun()
                        else:
                            st.error("应用设备失败。")
                with cb:
                    if st.button("录音测试(2.5秒)", key="btn_probe_mic", use_container_width=True):
                        with st.spinner("请对麦克风说一句完整口令..."):
                            st.session_state.voice_probe_result = _probe_python_mic(
                                st.session_state.assistant,
                                duration_seconds=2.5,
                            )

                probe = st.session_state.get("voice_probe_result")
                if isinstance(probe, dict):
                    if probe.get("ok"):
                        st.caption(
                            f"Probe: device={probe.get('deviceIndex')} "
                            f"RMS={probe.get('rms')} "
                            f"lang={probe.get('lang') or '-'}"
                        )
                        if probe.get("text"):
                            st.success(f"识别结果：{probe.get('text')}")
                        elif probe.get("error"):
                            st.warning(f"未识别出文本：{probe.get('error')}")
                        else:
                            st.warning("录音成功，但未识别出文本。请靠近麦克风再试。")
                    else:
                        st.error(f"录音测试失败：{probe.get('error') or 'unknown'}")

            st.caption("🧪 口令链路自测（不走 ASR，仅验证解析与页面动作）")
            if "manual_voice_cmd_text" not in st.session_state:
                st.session_state.manual_voice_cmd_text = "assistant pin link three"
            st.text_input(
                "测试口令",
                key="manual_voice_cmd_text",
                help="用于验证口令解析+点击动作链路本身是否正常（支持中文/英文口令）。"
            )
            st.caption("示例：助播 置顶3号链接 ｜ assistant pin link three ｜ assistant start flash sale")
            if st.button("执行口令链路测试", key="btn_manual_voice_cmd_test", use_container_width=True):
                if hasattr(st.session_state.assistant, "execute_manual_voice_text"):
                    result = st.session_state.assistant.execute_manual_voice_text(st.session_state.manual_voice_cmd_text)
                else:
                    result = {"ok": False, "error": "assistant_method_missing"}
                st.session_state.manual_voice_cmd_result = result
            cmd_result = st.session_state.get("manual_voice_cmd_result")
            if isinstance(cmd_result, dict):
                if cmd_result.get("ok"):
                    st.success(f"链路执行成功：{cmd_result.get('command')}")
                else:
                    st.warning(
                        f"链路执行结果：ok={cmd_result.get('ok')} "
                        f"error={cmd_result.get('error')} "
                        f"command={cmd_result.get('command')}"
                    )

        if st.button("♻️ 强制重载系统", use_container_width=True, help="如果修改了代码或遇到异常，点击此按钮重置系统"):
            _reload_runtime_settings()
            _reset_assistant_for_rerun()
            st.rerun()

    with st.expander("环境信息", expanded=False):
        launch_info = build_chrome_debug_commands(
            port=settings.BROWSER_PORT,
            user_data_path=settings.USER_DATA_PATH,
            chrome_executable=settings.CHROME_EXECUTABLE,
        )
        masked_key = f"{settings.LLM_API_KEY[:6]}******" if settings.LLM_API_KEY else "未配置"
        st.caption(f"API Key: {masked_key}")
        st.caption(f"Model: {settings.LLM_MODEL_NAME}")
        st.caption(f"Platform: {launch_info['platform_label']}")
        st.caption(f"Browser Resolver: {launch_info.get('browser_family', 'chrome')} | {launch_info.get('resolved_executable') or 'auto'}")
        st.caption(f"Browser Port: {settings.BROWSER_PORT}")
        st.caption(f"Unified Lang: {_get_unified_language(st.session_state.assistant)}")
        st.caption(f"Reply: {'ON' if _get_reply_enabled(st.session_state.assistant) else 'OFF'}")
        st.caption(f"Proactive: {'ON' if _get_proactive_enabled(st.session_state.assistant) else 'OFF'}")
        st.caption(f"Voice Cmd: {'ON' if _get_voice_command_enabled(st.session_state.assistant) else 'OFF'}")
        st.caption(
            f"Voice Cross-Lang: {'ON' if getattr(settings, 'VOICE_COMMAND_CROSS_LANGUAGE_ENABLED', True) else 'OFF'} "
            f"| order={','.join(getattr(settings, 'VOICE_COMMAND_CROSS_LANGUAGE_ORDER', []) or [])} "
            f"| max={getattr(settings, 'VOICE_COMMAND_MAX_LANGS', 2)}"
        )
        startup_state = _get_startup_state(st.session_state.assistant)
        st.caption(
            f"Startup: {'starting' if startup_state.get('is_starting') else ('running' if startup_state.get('is_running') else 'stopped')} "
            f"| err={startup_state.get('last_start_error') or 'none'}"
        )
        voice_state = _get_voice_state(st.session_state.assistant)
        if voice_state:
            running = "running" if voice_state.get("running") else "stopped"
            err = voice_state.get("error") or "none"
            runtime_provider = voice_state.get("runtimeProvider") or voice_state.get("provider") or "-"
            runtime_type = voice_state.get("runtimeProviderType") or "unknown"
            runtime_error = voice_state.get("runtimeProviderError") or "none"
            st.caption(
                f"Voice State: {running} | err={err} | capture={voice_state.get('captureMode') or '-'} "
                f"| asr={runtime_provider}({runtime_type}) | asr_err={runtime_error}"
            )
            if voice_state.get("deviceIndex") is not None:
                st.caption(f"Voice Device(Runtime): {voice_state.get('deviceIndex')} | {voice_state.get('deviceName') or '-'}")
        mic_perm = _get_mic_permission_state(st.session_state.assistant)
        st.caption(f"Mic Permission: {mic_perm.get('status')} | err={mic_perm.get('error') or 'none'}")
        if mic_perm.get("permissionState"):
            st.caption(f"Mic Permission API: {mic_perm.get('permissionState')}")
        if mic_perm.get("deviceIndex") is not None:
            st.caption(f"Mic Device Index: {mic_perm.get('deviceIndex')}")
        if hasattr(st.session_state.assistant, "get_voice_mic_device"):
            try:
                mic_cfg = st.session_state.assistant.get_voice_mic_device() or {}
                st.caption(f"Mic Preference: {mic_cfg.get('deviceIndex')} | hint={mic_cfg.get('nameHint') or '-'}")
            except Exception:
                pass
        voice_agent = getattr(st.session_state.assistant, "voice", None)
        if voice_agent is not None and getattr(voice_agent, "permission_blocked", False):
            st.caption("Voice Permission Lock: ON（检测到浏览器拒绝，等待手动重新授权）")
        if is_python_voice_mode and voice_agent is not None and hasattr(voice_agent, "list_input_devices"):
            devices = voice_agent.list_input_devices()
            if devices:
                dev_text = ", ".join([f"{d['index']}:{d['name']}" for d in devices[:8]])
                st.caption(f"Detected Mics: {dev_text}")
            else:
                st.caption("Detected Mics: none（未检测到输入设备，语音口令无法触发）")
        st.caption(f"Browser Detected: {browser_name}")
        st.caption("Chrome 调试启动命令（当前系统）:")
        st.code(launch_info["primary"], language="bash")
        st.caption(mic_guide)

if st.session_state.get("show_user_guide"):
    with st.expander("📘 使用说明书（点击收起）", expanded=True):
        st.markdown(_load_user_guide_markdown())
    
# 主界面：选项卡
tab0, tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["🚀 主功能面板", "📊 运行监控", "🧠 知识库调试", "👁️ 视觉调试", "💬 场控调试", "📈 数据报表", "✅ 系统自检"]
)

with tab0:
    st.subheader("🚀 主功能面板")
    if snapshot.get("web_info_mode") == "screen_ocr":
        st.markdown("优先保障主链路：屏幕采集 -> 麦克风权限 -> 启动监听 -> 自检。")
    else:
        st.markdown("优先保障主链路：连接浏览器 -> 麦克风权限 -> 启动监听 -> 自检。")

    p1, p2, p3 = st.columns([1.2, 1.2, 2.6])
    p1.metric("主链路健康度", f"{health['ok']}/{health['total']}")
    p2.metric("语音权限", mic_perm.get("status") or "unknown")
    p3.progress(health["ratio"], text=f"核心可用率 {int(health['ratio'] * 100)}%")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        connect_btn_text = "1. 屏幕源就绪" if snapshot.get("web_info_mode") == "screen_ocr" else "1. 连接浏览器"
        if st.button(connect_btn_text, key="btn_quick_connect", use_container_width=True):
            success = st.session_state.assistant.connect_browser()
            if success:
                if snapshot.get("web_info_mode") == "screen_ocr":
                    st.success("屏幕采集已就绪")
                else:
                    st.success("浏览器已连接")
                st.rerun()
            else:
                state = _get_startup_state(st.session_state.assistant)
                err = state.get("last_start_error") or "unknown"
                st.error(f"连接失败：{err}")
                if snapshot.get("web_info_mode") != "screen_ocr":
                    st.code(launch_info["primary"], language="bash")
    with c2:
        if st.button("2. 申请音频输入权限", key="btn_quick_mic", use_container_width=True):
            result = _request_mic_permission(st.session_state.assistant)
            status = result.get("status", "unknown")
            if status in ("requesting", "granted"):
                st.success(f"权限状态：{status}")
            elif status == "wrong_page":
                st.error("请先切到 TikTok 直播间页（@xxx/live）再申请音频输入权限。")
            elif status == "denied" and is_python_voice_mode:
                if effective_loopback_voice_mode:
                    st.error("系统回采输入不可用或被拒绝，请检查回采设备权限与音频路由后重试。")
                else:
                    st.error("系统麦克风权限被拒绝，请在系统设置中允许当前运行程序后重试。")
                st.caption("若仍失败，可先检查本地 ASR 依赖：")
                for cmd in get_python_asr_install_guide():
                    st.code(cmd, language="bash")
            else:
                st.warning(f"权限状态：{status} {result.get('error') or ''}")
                page_title = result.get("page_title") or ""
                page_url = result.get("page_url") or ""
                if page_title or page_url:
                    st.caption(f"当前页面: {page_title or '-'} | {page_url or '-'}")
    with c3:
        startup_state = _get_startup_state(st.session_state.assistant)
        if st.button(
            "3. 启动主监听",
            key="btn_quick_start",
            use_container_width=True,
            disabled=bool(startup_state.get("is_starting")),
        ):
            with st.spinner("正在启动..."):
                started = st.session_state.assistant.start()
            if started:
                st.success("主监听已启动")
                st.rerun()
            else:
                state = _get_startup_state(st.session_state.assistant)
                st.error(f"启动失败：{state.get('last_start_error') or 'unknown'}")
    with c4:
        if st.button("4. 运行自检", key="btn_quick_self_test", use_container_width=True):
            with st.spinner("自检中..."):
                st.session_state.self_test_results = _run_system_self_check(st.session_state.assistant)
            st.success("自检完成")

    st.caption("当前启动命令（按你系统自动生成）")
    st.code(launch_info["primary"], language="bash")
    st.caption("如需测试 mock 页面，请使用左侧“🧪 打开模拟网页测试”按钮。")

# Tab 1: 运行监控 (实时日志)
with tab1:
    st.subheader("实时运行监控")
    
    # 自动刷新复选框
    auto_refresh = st.checkbox("自动刷新 (每2秒)", value=True)

    if auto_refresh:
        render_monitor_fragment()
    else:
        render_monitor_body()

# Tab 2: 知识库调试
with tab2:
    st.subheader("🧠 知识库问答测试")
    st.markdown("直接与配置的 LLM (DeepSeek) 进行对话测试，验证 RAG 或纯对话功能。")
    st.caption(f"当前统一语言: {_get_unified_language(st.session_state.assistant)}")
    st.caption("说明：问题、知识库、语气模板可中英混写；最终回复只按统一语言输出。")

    uploaded_file = st.file_uploader(
        "上传知识文件（txt / xlsx）",
        type=["txt", "xlsx"],
        key="knowledge_uploader"
    )
    if uploaded_file is not None:
        if st.button("导入到知识库", key="btn_ingest"):
            save_dir = Path("data/uploads")
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = save_dir / uploaded_file.name
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            with st.spinner("正在导入知识库..."):
                st.session_state.assistant.knowledge.ingest_knowledge(str(save_path))
            st.success(f"导入完成：{save_path}")
    
    user_input = st.text_area(
        "输入测试问题",
        key="knowledge_test_input",
        height=88,
        placeholder="例如：这件衣服有红色的吗？ / Do you have this in brown?",
    )
    qa_btn_col1, qa_btn_col2 = st.columns([2, 1])
    send_clicked = qa_btn_col1.button("发送给 LLM", key="btn_llm", use_container_width=True)
    if qa_btn_col2.button("清空输入", key="btn_llm_clear", use_container_width=True):
        st.session_state.knowledge_test_input = ""
        st.rerun()

    if send_clicked:
        if not user_input:
            st.warning("请输入问题")
        else:
            with st.spinner("思考中..."):
                start_time = time.time()
                # 调用 KnowledgeAgent
                reply = st.session_state.assistant.knowledge.query(
                    user_input,
                    language=_get_unified_language(st.session_state.assistant),
                    tone_template=st.session_state.assistant.tone_template
                )
                end_time = time.time()
                
                st.success(f"耗时: {end_time - start_time:.2f}s")
                st.info(f"回复({ _get_unified_language(st.session_state.assistant) }): {reply}")

# Tab 3: 视觉调试
with tab3:
    st.subheader("👁️ 视觉识别测试")
    st.markdown("测试 OpenCV 模板匹配功能。请确保浏览器已打开且位于直播间页面。")
    launch_info = build_chrome_debug_commands(
        port=settings.BROWSER_PORT,
        user_data_path=settings.USER_DATA_PATH,
        chrome_executable=settings.CHROME_EXECUTABLE,
    )
    mock_shop_url = _get_mock_shop_url(st.session_state.assistant)
    launch_info_mock = build_chrome_debug_commands(
        port=settings.BROWSER_PORT,
        user_data_path=settings.USER_DATA_PATH,
        chrome_executable=settings.CHROME_EXECUTABLE,
        startup_url=mock_shop_url,
    )
    
    col_v1, col_v2 = st.columns(2)
    
    with col_v1:
        if st.button("尝试连接浏览器"):
            success = st.session_state.assistant.connect_browser()
            if success:
                st.success("连接成功！")
            else:
                st.error(f"连接失败，请检查 Chrome 调试端口 {settings.BROWSER_PORT} 是否开启。")
                st.code(launch_info["primary"], language="bash")

    with col_v2:
        template_name = st.text_input("输入模板文件名 (不含路径)", "pin_icon.png")
        if st.button("在屏幕上查找"):
            if not st.session_state.assistant.vision.page:
                st.error("请先连接浏览器")
            else:
                with st.spinner("截图并查找中..."):
                    try:
                        coords = find_button_on_screen(
                            st.session_state.assistant.vision,
                            template_name
                        )
                        if coords:
                            st.success(f"找到目标！坐标: {coords}")
                        else:
                            st.warning("未找到目标。")
                    except Exception as e:
                        st.error(f"发生错误: {e}")

# Tab 4: 场控调试
with tab4:
    st.subheader("💬 场控与弹幕流测试")
    st.markdown("发送模拟弹幕，它将进入系统处理流程（包括场控、LLM、日志记录）。")
    
    col_sim1, col_sim2 = st.columns([3, 1])
    with col_sim1:
        test_danmu = st.text_input("模拟弹幕内容", placeholder="例如：主播这件衣服多少钱？")
    with col_sim2:
        test_user = st.text_input("模拟用户名", value="TestUser")
    
    if st.button("发送模拟弹幕"):
        if not test_danmu:
            st.warning("请输入弹幕内容")
        else:
            msg = {'user': test_user, 'text': test_danmu}
            # 调用主流程处理
            st.session_state.assistant.handle_message(msg)
            st.success("弹幕已发送！请切换到“运行监控”查看处理结果。")

# Tab 5: 数据报表
with tab5:
    st.subheader("📈 数据报表与周期趋势")
    st.markdown("按周期查看弹幕走势、回复效率、用户结构与高频问题，并支持日报/周报一键生成。")

    analytics = getattr(st.session_state.assistant, "analytics", None)
    if not analytics:
        st.warning("当前助手实例不支持数据报表，请点击“强制重载系统”后重试。")
    else:
        today = date.today()
        default_start = today - timedelta(days=6)
        default_end = today

        ctrl1, ctrl2, ctrl3 = st.columns([3, 1, 2])
        with ctrl1:
            picked = st.date_input(
                "统计周期（开始/结束）",
                value=(default_start, default_end),
                key="analytics_range_picker",
            )
        with ctrl2:
            refresh_clicked = st.button("刷新看板", use_container_width=True, key="btn_refresh_analytics_dashboard")
        with ctrl3:
            st.caption("建议：日常看 7 天，复盘看 14~30 天。")

        range_start, range_end = default_start, default_end
        if isinstance(picked, (tuple, list)) and len(picked) == 2:
            if isinstance(picked[0], date):
                range_start = picked[0]
            if isinstance(picked[1], date):
                range_end = picked[1]
        elif isinstance(picked, date):
            range_start = picked
            range_end = picked
        if range_start > range_end:
            range_start, range_end = range_end, range_start

        cache_key = f"{range_start.isoformat()}::{range_end.isoformat()}"
        cached = st.session_state.get("analytics_dashboard_cache")
        if refresh_clicked or (not isinstance(cached, dict)) or cached.get("key") != cache_key:
            data = analytics.get_dashboard_data(start_date=range_start, end_date=range_end)
            st.session_state.analytics_dashboard_cache = {"key": cache_key, "data": data}
        else:
            data = cached.get("data")

        if not isinstance(data, dict):
            st.warning("当前周期暂无可视化数据。")
        else:
            cur = data.get("current", {}) or {}
            prev = data.get("previous", {}) or {}
            period = data.get("range", {}) or {}
            compare_period = data.get("compare_range", {}) or {}
            st.caption(
                f"当前周期：{period.get('start_date')} ~ {period.get('end_date')}（{period.get('days')}天）"
                f" | 对比周期：{compare_period.get('start_date')} ~ {compare_period.get('end_date')}"
            )

            def _fmt_pct(v):
                return f"{round(float(v or 0.0) * 100, 2)}%"

            def _fmt_pp_delta(a, b):
                return f"{round((float(a or 0.0) - float(b or 0.0)) * 100, 2)} pp"

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("弹幕总量", int(cur.get("total_messages", 0)), int(cur.get("total_messages", 0)) - int(prev.get("total_messages", 0)))
            m2.metric("独立用户", int(cur.get("unique_users", 0)), int(cur.get("unique_users", 0)) - int(prev.get("unique_users", 0)))
            m3.metric("回复覆盖率", _fmt_pct(cur.get("reply_rate", 0.0)), _fmt_pp_delta(cur.get("reply_rate", 0.0), prev.get("reply_rate", 0.0)))
            m4.metric("提问占比", _fmt_pct(cur.get("question_rate", 0.0)), _fmt_pp_delta(cur.get("question_rate", 0.0), prev.get("question_rate", 0.0)))
            m5.metric("平均耗时(ms)", round(float(cur.get("avg_processing_ms", 0.0)), 2), round(float(cur.get("avg_processing_ms", 0.0)) - float(prev.get("avg_processing_ms", 0.0)), 2))
            m6.metric("P90耗时(ms)", round(float(cur.get("p90_processing_ms", 0.0)), 2), round(float(cur.get("p90_processing_ms", 0.0)) - float(prev.get("p90_processing_ms", 0.0)), 2))

            st.markdown("#### 趋势变化")
            daily_df = pd.DataFrame(data.get("daily_series", []))
            if not daily_df.empty:
                daily_df["date"] = pd.to_datetime(daily_df["date"])
                trend_cols = ["messages", "replied", "questions"]
                trend_df = daily_df.set_index("date")[trend_cols]
                st.line_chart(trend_df, use_container_width=True, height=230)

                rate_df = daily_df.copy()
                rate_df["reply_rate_pct"] = rate_df["reply_rate"].astype(float) * 100.0
                rate_df["question_rate_pct"] = rate_df["question_rate"].astype(float) * 100.0
                st.line_chart(
                    rate_df.set_index("date")[["reply_rate_pct", "question_rate_pct"]],
                    use_container_width=True,
                    height=210,
                )
            else:
                st.info("当前周期暂无趋势数据。")

            st.markdown("#### 时段与结构分布")
            c1, c2 = st.columns(2)
            with c1:
                hourly_df = pd.DataFrame(data.get("hourly_series", []))
                if not hourly_df.empty:
                    st.caption("24小时弹幕/提问分布")
                    st.bar_chart(hourly_df.set_index("hour")[["messages", "questions"]], use_container_width=True, height=240)
                else:
                    st.info("暂无时段分布数据。")
            with c2:
                intent_df = pd.DataFrame(data.get("intent_series", []))
                if not intent_df.empty:
                    st.caption("用户意图分布")
                    st.bar_chart(intent_df.set_index("intent")[["count"]], use_container_width=True, height=240)
                else:
                    st.info("暂无意图分布数据。")

            c3, c4, c5 = st.columns(3)
            with c3:
                user_type_df = pd.DataFrame(data.get("user_type_series", []))
                st.caption("用户类型分布")
                if not user_type_df.empty:
                    st.bar_chart(user_type_df.set_index("user_type")[["count"]], use_container_width=True, height=210)
                else:
                    st.info("暂无数据")
            with c4:
                status_df = pd.DataFrame(data.get("status_series", []))
                st.caption("处理状态分布")
                if not status_df.empty:
                    st.bar_chart(status_df.set_index("status")[["count"]], use_container_width=True, height=210)
                else:
                    st.info("暂无数据")
            with c5:
                lang_df = pd.DataFrame(data.get("language_series", []))
                st.caption("语言分布")
                if not lang_df.empty:
                    st.bar_chart(lang_df.set_index("language")[["count"]], use_container_width=True, height=210)
                else:
                    st.info("暂无数据")

            st.markdown("#### 高频问题与关键用户")
            q_col, u_col = st.columns(2)
            with q_col:
                q_df = pd.DataFrame(data.get("top_questions", []))
                if not q_df.empty:
                    st.dataframe(q_df, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无高频问题。")
            with u_col:
                u_df = pd.DataFrame(data.get("top_users", []))
                if not u_df.empty:
                    st.dataframe(u_df, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无关键用户样本。")

            st.markdown("#### 优化建议")
            suggestions = data.get("recommendations", []) or []
            if suggestions:
                for idx, tip in enumerate(suggestions, 1):
                    st.write(f"{idx}. {tip}")
            else:
                st.info("当前周期暂无建议。")

        st.divider()
        st.markdown("#### 报告生成与历史查看")
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            if st.button("生成今日日报", use_container_width=True, key="btn_report_daily_today"):
                result = analytics.generate_daily_report()
                st.session_state.report_generation_result = result
                st.success(f"已生成：{result['path']}")
        with r2:
            if st.button("生成昨日日报", use_container_width=True, key="btn_report_daily_yesterday"):
                result = analytics.generate_daily_report(date.today() - timedelta(days=1))
                st.session_state.report_generation_result = result
                st.success(f"已生成：{result['path']}")
        with r3:
            if st.button("生成本周周报", use_container_width=True, key="btn_report_week_current"):
                result = analytics.generate_current_week_report()
                st.session_state.report_generation_result = result
                st.success(f"已生成：{result['path']}")
        with r4:
            if st.button("生成上周周报", use_container_width=True, key="btn_report_week_last"):
                result = analytics.generate_last_full_week_report()
                st.session_state.report_generation_result = result
                st.success(f"已生成：{result['path']}")

        gen = st.session_state.report_generation_result
        if gen:
            st.caption(f"最近生成：{gen['type']} | {gen['label']} | events={gen['event_count']}")
            analysis = gen.get("analysis", {})
            gm1, gm2, gm3, gm4 = st.columns(4)
            gm1.metric("弹幕总量", analysis.get("total_messages", 0))
            gm2.metric("独立用户", analysis.get("unique_users", 0))
            gm3.metric("提问占比", f"{round(float(analysis.get('question_rate', 0.0)) * 100, 2)}%")
            gm4.metric("回复覆盖率", f"{round(float(analysis.get('reply_rate', 0.0)) * 100, 2)}%")

        reports = analytics.list_reports(limit=50)
        if reports:
            selected = st.selectbox("历史报告", options=reports, index=0, key="report_file_selected")
            content = analytics.load_report_text(selected)
            st.text_area("报告内容预览", value=content, height=360)
        else:
            st.info("暂无报告，请先生成。")

    st.divider()
    st.subheader("🎤 本地语音触发压测（一键）")
    st.caption("流程：离线校验 -> 生成语音 -> 播放语音 -> 日志扫描。请先确保主监听已启动且麦克风权限已允许。")

    if sys.platform == "darwin":
        st.caption("当前平台：macOS（使用 say + afplay）")
    elif os.name == "nt":
        st.caption("当前平台：Windows（使用 PowerShell TTS + SoundPlayer）")
    else:
        st.warning("当前平台未提供一键语音压测流程，请使用离线压测脚本。")

    c1, c2, c3 = st.columns(3)
    with c1:
        stress_profile = st.selectbox(
            "压测档位",
            options=["quick", "all", "positive", "negative", "zh", "en"],
            index=0,
            key="voice_stress_profile_ui",
        )
    with c2:
        stress_rounds = st.number_input("播放轮数", min_value=1, max_value=5, value=1, step=1, key="voice_stress_rounds_ui")
    with c3:
        stress_gap = st.number_input("句间隔(秒)", min_value=0, max_value=8, value=2, step=1, key="voice_stress_gap_ui")

    if st.button("🚀 一键跑本地语音压测", key="btn_run_local_voice_stress", use_container_width=True):
        with st.spinner("正在执行一键压测（请勿关闭页面）..."):
            st.session_state.voice_stress_result = _run_local_voice_stress_pipeline(
                profile=stress_profile,
                rounds=stress_rounds,
                gap_seconds=stress_gap,
            )
        if st.session_state.voice_stress_result.get("ok"):
            st.success("一键压测完成。")
        else:
            st.error("一键压测中断，请查看步骤日志。")

    vr = st.session_state.voice_stress_result
    if vr:
        st.caption(f"最近运行时间：{vr.get('run_at')} | 结果：{'PASS' if vr.get('ok') else 'FAIL'}")
        for step in vr.get("steps", []):
            with st.expander(f"[{step.get('step')}] {'OK' if step.get('ok') else 'FAIL'} | code={step.get('code')}"):
                st.code(step.get("cmd", ""), language="bash")
                if step.get("stdout"):
                    st.text_area("stdout", value=step.get("stdout"), height=120, key=f"stdout_{step.get('step')}_{vr.get('run_at')}")
                if step.get("stderr"):
                    st.text_area("stderr", value=step.get("stderr"), height=120, key=f"stderr_{step.get('step')}_{vr.get('run_at')}")

# Tab 6: 系统自检
with tab6:
    st.subheader("✅ 系统自检")
    st.markdown("一键检查关键链路：配置、浏览器、语音监听、设备权限、口令解析、唤醒词、发送清洗。")

    col_t1, col_t2 = st.columns([1, 1])
    with col_t1:
        if st.button("🧪 运行系统自检", key="btn_self_test_tab", use_container_width=True):
            with st.spinner("正在执行自检..."):
                st.session_state.self_test_results = _run_system_self_check(st.session_state.assistant)
            st.success("自检已完成")
    with col_t2:
        if st.button("清空自检结果", key="btn_self_test_clear", use_container_width=True):
            st.session_state.self_test_results = None
            st.rerun()

    report = st.session_state.self_test_results
    if report:
        st.caption(f"最近自检时间: {report['time']}")
        st.metric("通过率", f"{report['pass_count']}/{report['total']}")
        st.dataframe(report["checks"], use_container_width=True, hide_index=True)
    else:
        st.info("点击上方按钮执行自检。")

    st.divider()
    st.subheader("🚀 全链路自动回归（替代人工冒烟）")
    st.caption("调用 scripts/global_feature_test.py，自动覆盖 EXE/Mock/语音/运营/知识库/报表等能力。")
    col_r1, col_r2, col_r3 = st.columns([1.2, 1, 1])
    with col_r1:
        regression_profile = st.selectbox(
            "回归档位",
            options=["full", "offline"],
            index=0,
            key="global_regression_profile_ui",
            help="full: 包含浏览器与麦克风端到端；offline: 仅离线验证。",
        )
    with col_r2:
        if st.button("▶️ 运行全链路回归", key="btn_run_global_regression", use_container_width=True):
            with st.spinner("正在执行全链路回归，请稍候..."):
                st.session_state.full_regression_result = _run_global_feature_regression(profile=regression_profile)
            if st.session_state.full_regression_result.get("ok"):
                st.success("全链路回归通过。")
            else:
                st.error("全链路回归未通过，请查看失败项与日志。")
    with col_r3:
        if st.button("清空回归结果", key="btn_clear_global_regression", use_container_width=True):
            st.session_state.full_regression_result = None
            st.rerun()

    gr = st.session_state.full_regression_result
    if gr:
        st.caption(f"最近运行时间: {gr.get('run_at')} | 结果: {'PASS' if gr.get('ok') else 'FAIL'}")
        payload = gr.get("report") if isinstance(gr.get("report"), dict) else {}
        if payload:
            st.metric("回归通过率", f"{payload.get('pass_count', 0)}/{payload.get('total_checks', 0)}")
            failed = [x for x in (payload.get("checks") or []) if not bool(x.get("ok"))]
            if failed:
                fail_df = [
                    {
                        "检查项": item.get("name"),
                        "结果": "FAIL",
                        "详情": item.get("detail"),
                    }
                    for item in failed
                ]
                st.dataframe(fail_df, use_container_width=True, hide_index=True)
            else:
                st.success("未发现失败项。")
            if gr.get("report_json"):
                st.code(str(gr.get("report_json")), language="text")
        step = gr.get("step") or {}
        with st.expander(f"[global_feature_test] {'OK' if step.get('ok') else 'FAIL'} | code={step.get('code')}"):
            st.code(step.get("cmd", ""), language="bash")
            if gr.get("report_md"):
                st.caption(f"报告文件: {gr.get('report_md')}")
            if step.get("stdout"):
                st.text_area("stdout", value=step.get("stdout"), height=140, key=f"global_reg_stdout_{gr.get('run_at')}")
            if step.get("stderr"):
                st.text_area("stderr", value=step.get("stderr"), height=140, key=f"global_reg_stderr_{gr.get('run_at')}")
