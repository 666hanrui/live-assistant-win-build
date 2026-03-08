#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "data" / "reports" / "global_feature_test"

FEATURES = {
    "danmu": "弹幕感知",
    "reply": "回复决策",
    "ops": "运营执行",
    "voice": "语音触发",
    "knowledge": "知识库RAG",
    "analytics": "数据分析",
    "mock": "Mock联调",
    "exe": "EXE链路",
    "state": "状态持久化",
}

VOICE_MODES = {
    "web_speech",
}


@dataclass
class CheckResult:
    check_id: str
    name: str
    features: List[str]
    required: bool
    ok: bool
    detail: str
    duration_ms: int
    extra: Dict[str, object] = field(default_factory=dict)


def _run_cmd(cmd: List[str], timeout: int = 60, env: Dict[str, str] | None = None) -> Dict[str, object]:
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "code": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


def _http_get(url: str, timeout: float = 2.5) -> Tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return int(resp.getcode()), body


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass


def _check_launcher_self() -> Tuple[bool, str, Dict[str, object]]:
    env = os.environ.copy()
    env["APP_LAUNCHER_SELF_CHECK"] = "1"
    env["DASHBOARD_AUTO_OPEN"] = "false"
    res = _run_cmd([sys.executable, "app_launcher.py"], timeout=90, env=env)
    ok = res["code"] == 0 and "APP_LAUNCHER_SELF_CHECK_OK" in str(res["stdout"])
    detail = f"code={res['code']}, token_ok={'yes' if ok else 'no'}"
    return ok, detail, {"cmd": "APP_LAUNCHER_SELF_CHECK=1 python app_launcher.py", **res}


def _check_dashboard_service_status() -> Tuple[bool, str, Dict[str, object]]:
    res = _run_cmd([sys.executable, "scripts/dashboard_service.py", "status"], timeout=40)
    text = f"{res['stdout']}\n{res['stderr']}".strip()
    ok = ("RUNNING" in text or "STOPPED" in text) and int(res["code"]) in {0, 1}
    detail = f"code={res['code']}, matched={'yes' if ok else 'no'}"
    return ok, detail, {"cmd": "python scripts/dashboard_service.py status", **res}


def _check_mock_server_http(strict_http: bool = True) -> Tuple[bool, str, Dict[str, object]]:
    try:
        port = _find_free_port()
    except PermissionError as e:
        mock_html = ROOT / "stress" / "mock_shop" / "mock_tiktok_shop.html"
        if strict_http:
            return False, f"mock_port_bind_blocked:{e}", {"error": str(e)}
        if not mock_html.exists():
            return False, "mock_html_missing", {"error": str(e), "path": str(mock_html)}
        body = mock_html.read_text(encoding="utf-8", errors="ignore")
        ok = ("mock_tiktok_shop" in body) and ("dashboard_live" in body)
        return ok, "network_restricted_fallback_file_check", {"path": str(mock_html)}

    cmd = [sys.executable, "scripts/mock_shop_server.py", "--host", "127.0.0.1", "--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    endpoints = [
        f"http://127.0.0.1:{port}/",
        f"http://127.0.0.1:{port}/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_idle",
        f"http://127.0.0.1:{port}/streamer/live/product/dashboard?mock_tiktok_shop=1&view=dashboard_live",
        f"http://127.0.0.1:{port}/workbench/live/overview?mock_tiktok_shop=1",
    ]
    checks = []
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.2)
            try:
                code, body = _http_get(endpoints[0], timeout=1.2)
                if code == 200 and "Mock TikTok Shop" in body:
                    ready = True
                    break
            except Exception:
                continue
        if not ready:
            return False, "mock_server_not_ready", {"cmd": " ".join(cmd)}

        for url in endpoints:
            code, body = _http_get(url, timeout=2.0)
            checks.append({"url": url, "code": code, "marker": "mock_tiktok_shop" in body or "Mock TikTok Shop" in body})
        ok = all(x["code"] == 200 and x["marker"] for x in checks)
        detail = "all_endpoints_ok" if ok else "endpoint_check_failed"
        return ok, detail, {"checks": checks, "cmd": " ".join(cmd)}
    except urllib.error.HTTPError as e:
        return False, f"http_error:{e.code}", {"cmd": " ".join(cmd), "error": str(e)}
    except Exception as e:
        return False, f"mock_http_exception:{e}", {"cmd": " ".join(cmd), "error": str(e)}
    finally:
        _terminate_process(proc)


def _check_assistant_core_logic() -> Tuple[bool, str, Dict[str, object]]:
    import app_config.settings as settings
    from agents.analytics_agent import AnalyticsAgent
    from main import LiveAssistant

    assistant = LiveAssistant()
    checks = []

    cases = [
        ("助播 将3号链接置顶一下", ("pin_product", 3)),
        ("助播 将3号链接取消置顶", ("unpin_product", 3)),
        ("assistant pin link three", ("pin_product", 3)),
        ("assistant please pin link number 99", ("pin_product", 99)),
        ("assistant stop flash sale now", ("stop_flash_sale", None)),
        ("assistant enable limited offer", ("start_flash_sale", None)),
        ("assistant disable limited offer", ("stop_flash_sale", None)),
        ("assistant please start flashsale for link number 2", ("start_flash_sale", 2)),
        ("assistant please pop the link again", ("repin_product", None)),
    ]
    parse_ok = True
    for text, expected in cases:
        got_raw = assistant._parse_operation_command_text(text)
        got = (got_raw.get("action"), got_raw.get("link_index")) if isinstance(got_raw, dict) else None
        if got != expected:
            parse_ok = False
    checks.append(("command_parser", parse_ok))

    parser_negative = assistant._parse_operation_command_text("assistant limited offer details")
    parser_negative_ok = parser_negative is None
    checks.append(("command_parser_negative", parser_negative_ok))

    wake_cases = [
        ("助播 把3号链接置顶", True),
        ("assistant pin link three", True),
        ("random text pin link three", False),
    ]
    wake_ok = all(assistant._pass_voice_wake_word(text) == expected for text, expected in wake_cases)
    checks.append(("wake_word", wake_ok))

    strict_backup = bool(getattr(settings, "VOICE_STRICT_WAKE_WORD", False))
    gate_ok = False
    try:
        settings.VOICE_STRICT_WAKE_WORD = True
        strict_no_wake = bool(assistant._pass_voice_wake_word("pin link 3 please"))
        strict_with_wake = bool(assistant._pass_voice_wake_word("assistant pin link three"))
        settings.VOICE_STRICT_WAKE_WORD = False
        loose_with_cmd = bool(assistant._parse_operation_command_text("pin link 3 please"))
        gate_ok = (strict_no_wake is False) and (strict_with_wake is True) and (loose_with_cmd is True)
    finally:
        settings.VOICE_STRICT_WAKE_WORD = strict_backup
    checks.append(("voice_gate", gate_ok))

    sanitized = assistant.operations._sanitize_outgoing_text("⌘a⌫ Ctrl+a hello")
    sanitize_ok = ("hello" in sanitized) and ("ctrl+a" not in sanitized.lower()) and ("⌘" not in sanitized)
    checks.append(("send_sanitizer", sanitize_ok))

    with tempfile.TemporaryDirectory(prefix="assistant_state_") as td:
        state_path = Path(td) / "runtime_state.json"
        assistant._runtime_state_file = state_path
        assistant._save_runtime_state()
        saved = json.loads(state_path.read_text(encoding="utf-8"))
    state_ok = all(k in saved for k in ("unified_language", "reply_enabled", "voice_command_enabled"))
    checks.append(("runtime_state_save", state_ok))

    with tempfile.TemporaryDirectory(prefix="assistant_events_") as td:
        tmp_root = Path(td)
        assistant.analytics = AnalyticsAgent(
            events_file=str(tmp_root / "events.jsonl"),
            reports_dir=str(tmp_root / "reports"),
            state_file=str(tmp_root / "reports" / "report_state.json"),
        )
        assistant.reply_enabled = False
        assistant.handle_message({"user": "global_test_user", "text": "hello from global test"}, allow_llm=False)
        events_file = tmp_root / "events.jsonl"
        event_lines = events_file.read_text(encoding="utf-8").splitlines() if events_file.exists() else []
        danmu_ok = len(event_lines) >= 1
        checks.append(("danmu_record", danmu_ok))

        keyword_keys = list(getattr(settings, "KEYWORD_REPLIES", {}).keys())
        keyword_ok = False
        if keyword_keys:
            assistant.reply_enabled = True
            assistant.operations.send_message = lambda _msg: True
            assistant.handle_message({"user": "global_test_user_2", "text": keyword_keys[0]}, allow_llm=False)
            latest = assistant.danmu_log[0] if assistant.danmu_log else {}
            keyword_ok = str((latest or {}).get("status")) == "replied"
        checks.append(("reply_decision", keyword_ok))

    voice_diag = assistant.voice.diagnose_voice_capability()
    voice_diag_ok = isinstance(voice_diag, dict) and bool(voice_diag.get("mode"))
    checks.append(("voice_diag", voice_diag_ok))

    all_ok = all(ok for _name, ok in checks)
    detail = ", ".join(f"{name}={'PASS' if ok else 'FAIL'}" for name, ok in checks)
    return all_ok, detail, {"checks": checks}


def _check_knowledge_offline_query() -> Tuple[bool, str, Dict[str, object]]:
    from agents.knowledge_agent import KnowledgeAgent

    with tempfile.TemporaryDirectory(prefix="knowledge_global_") as td:
        root = Path(td)
        kb_file = root / "kb.txt"
        kb_file.write_text(
            "主推款价格是99元。下单立减10元。48小时内发货。支持七天无理由退货。",
            encoding="utf-8",
        )

        agent = KnowledgeAgent(api_key="", base_url="", model_name="test")
        agent.persist_directory = str(root / "chroma_db")
        agent.local_knowledge_path = str(root / "local_knowledge_chunks.json")
        agent.local_entries = []
        agent.local_chunks = []
        agent.vector_store = None
        if agent.embeddings:
            agent.load_vector_store()

        agent.ingest_knowledge(str(kb_file))
        agent.has_llm = False
        agent.llm = None

        ans_price = agent.query("主推款价格是多少？", language="zh-CN")
        ans_shipping = agent.query("什么时候发货？", language="zh-CN")

    ok = bool(
        ans_price
        and ans_shipping
        and ("99" in ans_price or "价格" in ans_price or "元" in ans_price)
        and ("发货" in ans_shipping or "小时" in ans_shipping)
    )
    detail = f"price={ans_price}, shipping={ans_shipping}"
    return ok, detail, {"price_answer": ans_price, "shipping_answer": ans_shipping}


def _check_analytics_reports() -> Tuple[bool, str, Dict[str, object]]:
    from agents.analytics_agent import AnalyticsAgent

    with tempfile.TemporaryDirectory(prefix="analytics_global_") as td:
        root = Path(td)
        agent = AnalyticsAgent(
            events_file=str(root / "analytics" / "events.jsonl"),
            reports_dir=str(root / "reports"),
            state_file=str(root / "reports" / "report_state.json"),
        )

        now = datetime.now()
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=8)
        samples = [
            {
                "timestamp": now.isoformat(timespec="seconds"),
                "user": "u1",
                "text": "这个多少钱？",
                "status": "replied",
                "reply": "今天99元。",
                "action": "pin_product",
                "language": "zh-CN",
                "llm_candidate": True,
                "processing_ms": 180,
            },
            {
                "timestamp": yesterday.isoformat(timespec="seconds"),
                "user": "u2",
                "text": "什么时候发货？",
                "status": "replied",
                "reply": "48小时内发货。",
                "action": "",
                "language": "zh-CN",
                "llm_candidate": True,
                "processing_ms": 120,
            },
            {
                "timestamp": last_week.isoformat(timespec="seconds"),
                "user": "u3",
                "text": "支持退货吗？",
                "status": "ignored",
                "reply": "",
                "action": "",
                "language": "zh-CN",
                "llm_candidate": False,
                "processing_ms": 90,
            },
        ]
        for ev in samples:
            agent.record_danmu_event(ev)

        d_today = agent.generate_daily_report(date.today())
        d_yesterday = agent.generate_daily_report(date.today() - timedelta(days=1))
        w_current = agent.generate_current_week_report()
        w_last = agent.generate_last_full_week_report()
        created = agent.maybe_generate_periodic_reports()
        listed = agent.list_reports(limit=20)

        must_exist = [
            Path(d_today["path"]),
            Path(d_yesterday["path"]),
            Path(w_current["path"]),
            Path(w_last["path"]),
        ]
        exists_ok = all(p.exists() for p in must_exist)
        content_ok = "直播优化建议" in agent.load_report_text(str(must_exist[0]))
        list_ok = len(listed) >= 4

    ok = exists_ok and content_ok and list_ok
    detail = f"files={len(must_exist)}, listed={len(listed)}, periodic_created={len(created)}"
    return ok, detail, {"listed_reports": listed[:6], "periodic_created": created}


def _check_browser_mock_e2e() -> Tuple[bool, str, Dict[str, object]]:
    from main import LiveAssistant

    assistant = LiveAssistant()
    try:
        assistant.set_web_info_source_mode("dom", persist=False)
    except Exception:
        pass
    try:
        assistant.set_operation_execution_mode("dom", persist=False)
    except Exception:
        pass

    connected = bool(assistant.connect_mock_shop(view="dashboard_live"))
    if not connected:
        detail = f"connect_mock_failed: {assistant.last_start_error} | {assistant.last_start_detail}"
        return False, detail, {}

    commands = [
        "助播 将3号链接置顶",
        "助播 将3号链接取消置顶",
        "助播 开始秒杀活动",
        "助播 停止秒杀活动",
    ]
    results = []
    for text in commands:
        cmd = assistant._parse_operation_command_text(text)
        ok = bool(assistant._execute_operation_command(cmd, trigger_source="global_feature_test"))
        results.append({"text": text, "cmd": cmd, "ok": ok})

    all_ok = all(item["ok"] for item in results)
    detail = ", ".join(f"{item['text']}={'PASS' if item['ok'] else 'FAIL'}" for item in results)
    return all_ok, detail, {"results": results}


def _check_microphone_runtime() -> Tuple[bool, str, Dict[str, object]]:
    from main import LiveAssistant

    assistant = LiveAssistant()
    diag = assistant.voice.diagnose_voice_capability()
    mode = str(diag.get("mode") or "").strip().lower()
    if mode not in VOICE_MODES:
        return False, f"unsupported_voice_mode:{mode}", {"diag": diag}

    if not bool(diag.get("speechRecognition")):
        return False, "web_speech_unavailable", {"diag": diag}

    perm = assistant.voice.request_microphone_permission()
    perm_status = str(perm.get("status") or "").strip().lower()
    if perm_status not in {"granted", "requesting", "idle", "needs_in_tab_click", "wrong_page", "no_page"}:
        return False, f"microphone_permission_failed:{perm.get('error')}", {"diag": diag, "permission": perm}

    ok = str(diag.get("captureMode") or "browser_mic").strip().lower() == "browser_mic"
    detail = (
        f"mode={mode}, capture_mode={diag.get('captureMode') or 'browser_mic'}, "
        f"permission={perm_status}, err={perm.get('error') or 'none'}"
    )
    return ok, detail, {"diag": diag, "permission": perm}


def _render_markdown(payload: Dict[str, object]) -> str:
    lines = []
    lines.append("# Global Feature Test Report")
    lines.append("")
    lines.append(f"- Generated At: {payload['generated_at']}")
    lines.append(f"- Profile: {payload['profile']}")
    lines.append(f"- Overall: {'PASS' if payload['overall_ok'] else 'FAIL'}")
    lines.append(f"- Pass: {payload['pass_count']}/{payload['total_checks']}")
    lines.append("")
    lines.append("## Feature Coverage")
    covered = payload.get("covered_feature_names") or []
    missing = payload.get("missing_feature_names") or []
    lines.append(f"- Covered: {', '.join(covered) if covered else 'none'}")
    lines.append(f"- Missing: {', '.join(missing) if missing else 'none'}")
    lines.append("")
    lines.append("## Checks")
    for item in payload.get("checks") or []:
        mark = "PASS" if item.get("ok") else "FAIL"
        req = "required" if item.get("required") else "optional"
        lines.append(
            f"- [{mark}] {item.get('name')} ({req}, {item.get('duration_ms')}ms): {item.get('detail')}"
        )
    lines.append("")
    return "\n".join(lines)


def run(profile: str) -> Dict[str, object]:
    checks: List[CheckResult] = []

    def do_check(
        check_id: str,
        name: str,
        features: List[str],
        required: bool,
        fn: Callable[[], Tuple[bool, str, Dict[str, object]]],
    ) -> None:
        started = time.perf_counter()
        try:
            ok, detail, extra = fn()
        except Exception as e:
            ok = False
            detail = f"exception: {e}"
            extra = {"traceback": traceback.format_exc()}
        duration_ms = int((time.perf_counter() - started) * 1000)
        checks.append(
            CheckResult(
                check_id=check_id,
                name=name,
                features=list(features),
                required=required,
                ok=bool(ok),
                detail=str(detail),
                duration_ms=duration_ms,
                extra=dict(extra or {}),
            )
        )

    do_check("launcher_self", "EXE 启动器自检", ["exe"], True, _check_launcher_self)
    do_check("dashboard_service", "服务脚本状态检查", ["exe"], True, _check_dashboard_service_status)
    do_check(
        "mock_http",
        "Mock 页面 HTTP 联调",
        ["mock"],
        True,
        (lambda: _check_mock_server_http(strict_http=(profile == "full"))),
    )
    do_check(
        "assistant_core",
        "主流程核心逻辑（解析/门控/回复/落盘）",
        ["danmu", "reply", "ops", "voice", "state"],
        True,
        _check_assistant_core_logic,
    )
    do_check("knowledge_offline", "知识库离线检索", ["knowledge"], True, _check_knowledge_offline_query)
    do_check("analytics_reports", "分析报表生成", ["analytics"], True, _check_analytics_reports)

    if profile == "full":
        do_check("browser_e2e", "浏览器+Mock 运营动作端到端", ["ops", "mock"], True, _check_browser_mock_e2e)
        do_check("microphone_e2e", "麦克风权限与识别链路", ["voice"], True, _check_microphone_runtime)

    covered = set()
    for item in checks:
        for ft in item.features:
            covered.add(ft)
    missing = sorted(set(FEATURES.keys()) - covered)
    for key in missing:
        checks.append(
            CheckResult(
                check_id=f"coverage_missing_{key}",
                name=f"覆盖门禁: {FEATURES[key]}",
                features=[key],
                required=True,
                ok=False,
                detail="missing_feature_test",
                duration_ms=0,
                extra={},
            )
        )

    overall_ok = all((not item.required) or item.ok for item in checks)
    pass_count = sum(1 for item in checks if item.ok)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "overall_ok": overall_ok,
        "pass_count": pass_count,
        "total_checks": len(checks),
        "covered_features": sorted(covered),
        "covered_feature_names": [FEATURES[k] for k in sorted(covered) if k in FEATURES],
        "missing_features": missing,
        "missing_feature_names": [FEATURES[k] for k in missing if k in FEATURES],
        "checks": [
            {
                "check_id": item.check_id,
                "name": item.name,
                "features": item.features,
                "feature_names": [FEATURES[k] for k in item.features if k in FEATURES],
                "required": item.required,
                "ok": item.ok,
                "detail": item.detail,
                "duration_ms": item.duration_ms,
                "extra": item.extra,
            }
            for item in checks
        ],
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Global feature test runner.")
    parser.add_argument(
        "--profile",
        default="full",
        choices=["full", "offline"],
        help="full: 覆盖浏览器/麦克风端到端；offline: 仅离线能力验证。",
    )
    args = parser.parse_args()

    payload = run(profile=args.profile)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"{args.profile}_{ts}.json"
    md_path = REPORT_DIR / f"{args.profile}_{ts}.md"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")

    print(f"global_feature_report_json={json_path}")
    print(f"global_feature_report_md={md_path}")
    print(f"overall={'PASS' if payload['overall_ok'] else 'FAIL'} pass={payload['pass_count']}/{payload['total_checks']}")
    return 0 if payload["overall_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
