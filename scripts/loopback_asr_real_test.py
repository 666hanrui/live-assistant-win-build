#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_PATH = ROOT / "stress" / "voice" / "test_cases.json"
REPORT_DIR = ROOT / "data" / "reports" / "voice_stress"

LOOPBACK_MODES = {
    "system_loopback_asr",
    "loopback_asr",
    "system_audio_asr",
    "tab_audio_asr",
    "loopback",
}


def _load_cases(path: Path) -> List[Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def _filter_cases(cases: List[Dict[str, object]], profile: str) -> List[Dict[str, object]]:
    if profile == "all":
        return list(cases)
    if profile == "quick":
        keep = {
            "P001",
            "P005",
            "P013",
            "P015",
            "L001",
            "L003",
            "L005",
            "L006",
            "N001",
            "N003",
            "N006",
            "M002",
        }
        return [c for c in cases if str(c.get("id") or "") in keep]
    if profile == "positive":
        return [c for c in cases if "positive" in str(c.get("category", ""))]
    if profile == "negative":
        return [c for c in cases if ("negative" in str(c.get("category", "")) or "risk" in str(c.get("category", "")))]
    if profile == "zh":
        return [c for c in cases if str(c.get("lang", "")).startswith("zh")]
    if profile == "en":
        return [c for c in cases if str(c.get("lang", "")).startswith("en")]
    return list(cases)


def _choose_audio_root(explicit_dir: str) -> Path:
    if explicit_dir:
        return Path(explicit_dir).expanduser()
    if sys.platform == "darwin":
        return ROOT / "stress" / "voice" / "audio_mac"
    if os.name == "nt":
        return ROOT / "stress" / "voice" / "audio"
    raise RuntimeError(f"unsupported_os:{sys.platform}")


def _resolve_audio_file(audio_root: Path, case_id: str) -> Path | None:
    candidates: List[Path] = []
    if sys.platform == "darwin":
        candidates = [audio_root / f"{case_id}.aiff", audio_root / f"{case_id}.wav"]
    elif os.name == "nt":
        candidates = [audio_root / f"{case_id}.wav", audio_root / f"{case_id}.aiff"]
    else:
        candidates = [audio_root / f"{case_id}.wav", audio_root / f"{case_id}.aiff"]

    for item in candidates:
        if item.exists():
            return item
    return None


def _play_audio(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["afplay", str(path)], check=True)
        return

    if os.name == "nt":
        escaped_path = str(path).replace("'", "''")
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                "$p = New-Object System.Media.SoundPlayer @'"
                + escaped_path
                + "'@; $p.Load(); $p.PlaySync();"
            ),
        ]
        subprocess.run(cmd, check=True)
        return

    raise RuntimeError(f"unsupported_os:{sys.platform}")


def _normalize_expected(case: Dict[str, object], strict_wake: bool) -> Dict[str, object]:
    expect = dict(case.get("expect") or {})
    key = "strict" if strict_wake else "loose"
    item = dict(expect.get(key) or {})
    return {
        "action": item.get("action") or "none",
        "link_index": item.get("link_index"),
    }


def _action_tuple(command: Dict[str, object] | None) -> Tuple[str, object]:
    if not isinstance(command, dict):
        return ("none", None)
    return (str(command.get("action") or "none"), command.get("link_index"))


def _setup_runtime_env(args: argparse.Namespace) -> None:
    os.environ["VOICE_COMMAND_INPUT_MODE"] = str(args.mode).strip().lower()
    os.environ["VOICE_COMMAND_ENABLED"] = "true"
    os.environ["VOICE_PYTHON_ASR_PROVIDER"] = str(args.provider).strip().lower()
    os.environ["VOICE_COMMAND_POLL_INTERVAL_SECONDS"] = str(max(0.12, float(args.poll_interval)))
    os.environ["VOICE_STRICT_WAKE_WORD"] = "true" if bool(args.strict_wake_word) else "false"
    os.environ["VOICE_LOOPBACK_DEVICE_INDEX"] = str(int(args.loopback_device_index))
    os.environ["VOICE_LOOPBACK_DEVICE_NAME_HINT"] = str(args.loopback_device_name_hint or "").strip()
    os.environ["VOICE_PYTHON_LISTEN_TIMEOUT_SECONDS"] = str(max(0.6, float(args.listen_timeout)))
    os.environ["VOICE_PYTHON_PHRASE_TIME_LIMIT_SECONDS"] = str(max(0.8, float(args.phrase_time_limit)))
    os.environ["VOICE_PYTHON_AMBIENT_ADJUST_SECONDS"] = str(max(0.0, float(args.ambient_adjust)))
    os.environ["VOICE_COMMAND_FALLBACK_LANGUAGES"] = str(args.fallback_languages or "en-US,zh-CN")
    os.environ["VOICE_COMMAND_MAX_LANGS"] = "2"


def _reload_runtime_modules():
    import app_config.settings as settings  # type: ignore
    import main as main_module  # type: ignore

    importlib.reload(settings)
    importlib.reload(main_module)
    return settings, main_module


def _run_case(
    assistant,
    case: Dict[str, object],
    expected: Dict[str, object],
    audio_path: Path,
    action_events: List[Dict[str, object]],
    args: argparse.Namespace,
) -> Dict[str, object]:
    case_id = str(case.get("id") or "")
    text = str(case.get("text") or "")
    case_lang = str(case.get("lang") or "zh-CN")

    # 语言按用例切换，避免中英文语族门控互相拦截。
    assistant.reply_language = case_lang if case_lang else assistant.reply_language
    start_ok = False
    start_error = ""
    try:
        assistant.voice.stop()
    except Exception:
        pass
    try:
        start_ok = bool(assistant.voice.start(language=assistant.reply_language, fallback_languages=[]))
    except Exception as e:
        start_ok = False
        start_error = str(e)
    time.sleep(max(0.2, float(args.pre_settle_seconds)))

    before_voice_log_len = len(assistant.voice_input_log)
    before_action_len = len(action_events)
    started_at_ms = int(time.time() * 1000)

    play_err = ""
    try:
        _play_audio(audio_path)
    except Exception as e:
        play_err = str(e)

    deadline = time.time() + max(1.8, float(args.wait_seconds))
    while time.time() < deadline:
        assistant._poll_voice_commands()
        time.sleep(max(0.05, float(args.poll_interval)))

    new_actions = list(action_events[before_action_len:])
    new_voice_count = max(0, len(assistant.voice_input_log) - before_voice_log_len)
    new_voice_logs = list(assistant.voice_input_log)[:new_voice_count]
    state = assistant.voice.get_state()

    expected_tuple = (str(expected.get("action") or "none"), expected.get("link_index"))
    matched = False
    for ev in new_actions:
        got_tuple = (str(ev.get("action") or "none"), ev.get("link_index"))
        if got_tuple == expected_tuple:
            matched = True
            break

    if expected_tuple[0] == "none":
        matched = len(new_actions) == 0

    source_hit = any(str(item.get("source") or "") == "python_loopback" for item in new_voice_logs)
    gate_note = ",".join(
        sorted({str(item.get("note") or "") for item in new_voice_logs if str(item.get("note") or "").strip()})
    )
    row = {
        "id": case_id,
        "text": text,
        "lang": case_lang,
        "category": case.get("category"),
        "critical": bool(case.get("critical", True)),
        "audio_file": str(audio_path),
        "start_ok": start_ok,
        "start_error": start_error,
        "play_error": play_err,
        "expected": {"action": expected_tuple[0], "link_index": expected_tuple[1]},
        "actions": [
            {
                "action": str(ev.get("action") or "none"),
                "link_index": ev.get("link_index"),
                "trigger_source": ev.get("trigger_source"),
                "at_ms": ev.get("at_ms"),
            }
            for ev in new_actions
        ],
        "voice_entries": [
            {
                "source": item.get("source"),
                "status": item.get("status"),
                "note": item.get("note"),
                "command": item.get("command"),
                "text": item.get("text"),
                "lang": item.get("lang"),
            }
            for item in new_voice_logs
        ],
        "source_hit": source_hit,
        "gate_note": gate_note,
        "state": {
            "running": bool(state.get("running")) if isinstance(state, dict) else False,
            "error": state.get("error") if isinstance(state, dict) else None,
            "captureMode": state.get("captureMode") if isinstance(state, dict) else None,
            "source": state.get("source") if isinstance(state, dict) else None,
            "deviceName": state.get("deviceName") if isinstance(state, dict) else None,
            "lastAudioRms": state.get("lastAudioRms") if isinstance(state, dict) else None,
            "lastText": state.get("lastText") if isinstance(state, dict) else None,
            "lastTextLang": state.get("lastTextLang") if isinstance(state, dict) else None,
            "lastResultAt": state.get("lastResultAt") if isinstance(state, dict) else None,
        },
        "started_at_ms": started_at_ms,
        "ok": bool(start_ok and matched and (not play_err)),
    }
    return row


def run_real_test(args: argparse.Namespace) -> Dict[str, object]:
    _setup_runtime_env(args)
    settings, main_module = _reload_runtime_modules()
    LiveAssistant = getattr(main_module, "LiveAssistant")

    mode = str(args.mode).strip().lower()
    if mode not in LOOPBACK_MODES:
        raise RuntimeError(f"invalid_loopback_mode:{mode}")

    audio_root = _choose_audio_root(args.audio_dir)
    cases = _filter_cases(_load_cases(Path(args.cases_path)), args.profile)
    if args.case_ids:
        wanted = {x.strip() for x in str(args.case_ids).split(",") if x.strip()}
        cases = [c for c in cases if str(c.get("id") or "") in wanted]

    if int(args.max_cases) > 0:
        cases = cases[: int(args.max_cases)]
    if not cases:
        raise RuntimeError("no_cases_selected")

    missing_audio = []
    for case in cases:
        case_id = str(case.get("id") or "")
        audio_file = _resolve_audio_file(audio_root, case_id)
        if audio_file is None:
            missing_audio.append(case_id)
    if missing_audio:
        raise RuntimeError(f"audio_missing_for_cases:{','.join(missing_audio)}")

    assistant = LiveAssistant()
    assistant.voice_command_enabled = True
    assistant.reply_enabled = False
    assistant.proactive_enabled = False
    assistant.reply_language = str(args.default_language or "zh-CN")

    if int(args.loopback_device_index) >= 0 or str(args.loopback_device_name_hint or "").strip():
        assistant.set_voice_mic_device(
            device_index=int(args.loopback_device_index),
            name_hint=str(args.loopback_device_name_hint or "").strip(),
        )

    diag = assistant.voice.diagnose_voice_capability()
    if str(diag.get("mode") or "").strip().lower() not in LOOPBACK_MODES:
        raise RuntimeError(f"runtime_mode_not_loopback:{diag}")

    perm = assistant.voice.request_microphone_permission()
    perm_status = str(perm.get("status") or "").strip().lower()
    if perm_status != "granted":
        raise RuntimeError(f"loopback_permission_not_granted:{perm}")

    action_events: List[Dict[str, object]] = []
    original_execute = assistant._execute_operation_command

    def _mock_execute(command, trigger_source="", log_entry=None):
        act, idx = _action_tuple(command)
        event = {
            "at_ms": int(time.time() * 1000),
            "action": act,
            "link_index": idx,
            "trigger_source": trigger_source,
            "command": dict(command or {}) if isinstance(command, dict) else {},
        }
        action_events.append(event)
        if isinstance(log_entry, dict):
            log_entry["status"] = "voice_action_mocked"
            log_entry["action"] = act
            log_entry["action_receipt"] = "mock_ok"
        return True

    assistant._execute_operation_command = _mock_execute

    rows: List[Dict[str, object]] = []
    started_at = datetime.now().isoformat(timespec="seconds")

    try:
        for case in cases:
            case_id = str(case.get("id") or "")
            audio_file = _resolve_audio_file(audio_root, case_id)
            expected = _normalize_expected(case, bool(args.strict_wake_word))
            row = _run_case(assistant, case, expected, audio_file, action_events, args)
            rows.append(row)
            time.sleep(max(0.0, float(args.post_case_gap_seconds)))
    finally:
        assistant._execute_operation_command = original_execute
        try:
            assistant.voice.stop()
        except Exception:
            pass

    total = len(rows)
    pass_count = sum(1 for r in rows if bool(r.get("ok")))
    source_hit_count = sum(1 for r in rows if bool(r.get("source_hit")))
    critical_total = sum(1 for r in rows if bool(r.get("critical", True)))
    critical_pass = sum(1 for r in rows if bool(r.get("critical", True)) and bool(r.get("ok")))
    rms_values = []
    for r in rows:
        state = dict(r.get("state") or {})
        try:
            rms = int(state.get("lastAudioRms") or 0)
        except Exception:
            rms = 0
        if rms > 0:
            rms_values.append(rms)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": started_at,
        "profile": args.profile,
        "mode": mode,
        "provider": str(getattr(settings, "VOICE_PYTHON_ASR_PROVIDER", "")),
        "strict_wake_word": bool(args.strict_wake_word),
        "audio_root": str(audio_root),
        "total": total,
        "pass_count": pass_count,
        "critical_total": critical_total,
        "critical_pass": critical_pass,
        "source_hit_count": source_hit_count,
        "source_hit_rate": (float(source_hit_count) / float(total)) if total else 0.0,
        "rms_avg": (sum(rms_values) / float(len(rms_values))) if rms_values else 0.0,
        "rms_max": max(rms_values) if rms_values else 0,
        "rows": rows,
        "diag": diag,
        "permission": perm,
        "overall_ok": bool(total > 0 and pass_count == total and source_hit_count > 0),
    }
    return payload


def _render_markdown(payload: Dict[str, object]) -> str:
    rows = list(payload.get("rows") or [])
    lines: List[str] = []
    lines.append("# Loopback ASR Real Test Report")
    lines.append("")
    lines.append(f"- Generated At: {payload.get('generated_at')}")
    lines.append(f"- Profile: {payload.get('profile')}")
    lines.append(f"- Mode: {payload.get('mode')}")
    lines.append(f"- Provider: {payload.get('provider')}")
    lines.append(f"- Total: {payload.get('pass_count')}/{payload.get('total')}")
    lines.append(f"- Critical: {payload.get('critical_pass')}/{payload.get('critical_total')}")
    lines.append(f"- Source Hit: {payload.get('source_hit_count')}/{payload.get('total')}")
    lines.append(f"- RMS Avg/Max: {payload.get('rms_avg'):.1f}/{payload.get('rms_max')}")
    lines.append(f"- Overall: {'PASS' if payload.get('overall_ok') else 'FAIL'}")
    lines.append("")
    lines.append("## Failed Cases")
    failed = [r for r in rows if not bool(r.get("ok"))]
    if not failed:
        lines.append("- None")
    else:
        for row in failed:
            lines.append(
                f"- [{row.get('id')}] expected={row.get('expected')} actions={row.get('actions')} "
                f"source_hit={row.get('source_hit')} start_error={row.get('start_error') or 'none'} "
                f"play_error={row.get('play_error') or 'none'} "
                f"voice_error={dict(row.get('state') or {}).get('error')}"
            )
    lines.append("")
    lines.append("## Case Summary")
    for row in rows:
        mark = "PASS" if row.get("ok") else "FAIL"
        state = dict(row.get("state") or {})
        lines.append(
            f"- [{mark}] {row.get('id')} | exp={row.get('expected')} | actions={row.get('actions')} "
            f"| source_hit={row.get('source_hit')} | start_ok={row.get('start_ok')} "
            f"| rms={state.get('lastAudioRms')} "
            f"| last_text={state.get('lastText') or ''} | note={row.get('gate_note') or ''}"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Real loopback ASR e2e test.")
    parser.add_argument("--profile", default="quick", choices=["all", "quick", "positive", "negative", "zh", "en"])
    parser.add_argument("--cases-path", default=str(CASES_PATH))
    parser.add_argument("--case-ids", default="", help="Comma-separated case ids to run, e.g. P001,P013")
    parser.add_argument("--max-cases", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--audio-dir", default="", help="Override audio directory.")
    parser.add_argument("--mode", default="system_loopback_asr", help="Loopback mode alias.")
    parser.add_argument("--provider", default="whisper_local", help="ASR provider.")
    parser.add_argument("--default-language", default="zh-CN")
    parser.add_argument("--fallback-languages", default="en-US,zh-CN")
    parser.add_argument("--strict-wake-word", action="store_true")
    parser.add_argument("--loopback-device-index", type=int, default=-1)
    parser.add_argument("--loopback-device-name-hint", default="")
    parser.add_argument("--wait-seconds", type=float, default=6.0)
    parser.add_argument("--poll-interval", type=float, default=0.15)
    parser.add_argument("--pre-settle-seconds", type=float, default=0.35)
    parser.add_argument("--post-case-gap-seconds", type=float, default=0.5)
    parser.add_argument("--listen-timeout", type=float, default=2.4)
    parser.add_argument("--phrase-time-limit", type=float, default=3.6)
    parser.add_argument("--ambient-adjust", type=float, default=0.2)
    parser.add_argument("--json", action="store_true", help="Also write json report.")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        payload = run_real_test(args)
        md_path = REPORT_DIR / f"loopback_real_{args.profile}_{ts}.md"
        md_path.write_text(_render_markdown(payload), encoding="utf-8")
        print(f"loopback_real_report_md={md_path}")
        if args.json:
            json_path = REPORT_DIR / f"loopback_real_{args.profile}_{ts}.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"loopback_real_report_json={json_path}")
        print(f"overall={'PASS' if payload.get('overall_ok') else 'FAIL'} pass={payload.get('pass_count')}/{payload.get('total')}")
        return 0 if payload.get("overall_ok") else 2
    except Exception as e:
        fail_payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "overall_ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }
        md_path = REPORT_DIR / f"loopback_real_{args.profile}_{ts}_failed.md"
        md_path.write_text(
            "# Loopback ASR Real Test Report\n\n"
            f"- Overall: FAIL\n"
            f"- Error: {e}\n\n"
            "## Traceback\n\n"
            f"```\n{fail_payload['traceback']}\n```\n",
            encoding="utf-8",
        )
        print(f"loopback_real_report_md={md_path}")
        if args.json:
            json_path = REPORT_DIR / f"loopback_real_{args.profile}_{ts}_failed.json"
            json_path.write_text(json.dumps(fail_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"loopback_real_report_json={json_path}")
        print(f"overall=FAIL error={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
