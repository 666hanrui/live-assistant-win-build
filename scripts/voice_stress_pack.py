#!/usr/bin/env python3
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CASES_PATH = ROOT / "stress" / "voice" / "test_cases.json"
REPORT_DIR = ROOT / "data" / "reports" / "voice_stress"
LOG_FILE = ROOT / "logs" / "app.log"


def load_cases():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data.get("cases", [])


def filter_cases(cases, profile):
    if profile == "all":
        return cases
    if profile == "quick":
        keep_ids = {
            "P001", "P005", "P013", "P015", "L001", "L003", "L005", "L006", "N001", "N003", "N006", "M002"
        }
        return [c for c in cases if c.get("id") in keep_ids]
    if profile == "positive":
        return [c for c in cases if "positive" in str(c.get("category", ""))]
    if profile == "negative":
        return [c for c in cases if "negative" in str(c.get("category", "")) or "risk" in str(c.get("category", ""))]
    if profile == "zh":
        return [c for c in cases if str(c.get("lang", "")).startswith("zh")]
    if profile == "en":
        return [c for c in cases if str(c.get("lang", "")).startswith("en")]
    return cases


def create_parser_probe():
    from main import LiveAssistant

    probe = LiveAssistant.__new__(LiveAssistant)
    return probe


def normalize_action(cmd):
    if not cmd:
        return {"action": "none", "link_index": None}
    return {
        "action": cmd.get("action") or "none",
        "link_index": cmd.get("link_index"),
    }


def detect_for_mode(probe, text, strict):
    has_wake = bool(probe._pass_voice_wake_word(text))
    parsed = probe._parse_operation_command_text(text)

    if strict and not has_wake:
        return {"action": "none", "link_index": None}, has_wake, normalize_action(parsed)
    if (not has_wake) and (not parsed):
        return {"action": "none", "link_index": None}, has_wake, normalize_action(parsed)
    return normalize_action(parsed), has_wake, normalize_action(parsed)


def is_match(got, exp):
    exp_action = exp.get("action", "none")
    exp_idx = exp.get("link_index")
    return got.get("action") == exp_action and got.get("link_index") == exp_idx


def run_offline(profile, output_json=False):
    probe = create_parser_probe()
    cases = filter_cases(load_cases(), profile)

    rows = []
    strict_pass = 0
    loose_pass = 0
    strict_critical_pass = 0
    loose_critical_pass = 0
    critical_total = 0

    for case in cases:
        text = str(case.get("text", ""))
        expected = case.get("expect", {})
        exp_strict = expected.get("strict", {"action": "none", "link_index": None})
        exp_loose = expected.get("loose", {"action": "none", "link_index": None})

        got_strict, has_wake, parsed = detect_for_mode(probe, text, strict=True)
        got_loose, _, _ = detect_for_mode(probe, text, strict=False)

        ok_strict = is_match(got_strict, exp_strict)
        ok_loose = is_match(got_loose, exp_loose)
        critical = bool(case.get("critical", True))
        if critical:
            critical_total += 1

        strict_pass += 1 if ok_strict else 0
        loose_pass += 1 if ok_loose else 0
        strict_critical_pass += 1 if (critical and ok_strict) else 0
        loose_critical_pass += 1 if (critical and ok_loose) else 0

        rows.append(
            {
                "id": case.get("id"),
                "category": case.get("category"),
                "critical": critical,
                "text": text,
                "has_wake": has_wake,
                "parsed": parsed,
                "expect_strict": exp_strict,
                "expect_loose": exp_loose,
                "got_strict": got_strict,
                "got_loose": got_loose,
                "ok_strict": ok_strict,
                "ok_loose": ok_loose,
            }
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = REPORT_DIR / f"offline_{profile}_{now}.md"
    json_path = REPORT_DIR / f"offline_{profile}_{now}.json"

    lines = []
    lines.append(f"# Voice Stress Offline Report ({profile})")
    lines.append("")
    lines.append(f"- Generated At: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Total Cases: {len(rows)}")
    lines.append(f"- Strict Pass: {strict_pass}/{len(rows)}")
    lines.append(f"- Loose Pass: {loose_pass}/{len(rows)}")
    if critical_total:
        lines.append(f"- Strict Critical Pass: {strict_critical_pass}/{critical_total}")
        lines.append(f"- Loose Critical Pass: {loose_critical_pass}/{critical_total}")
    lines.append("")
    lines.append("## Failed Cases")
    failed = [r for r in rows if (not r["ok_strict"]) or (not r["ok_loose"])]
    if not failed:
        lines.append("- None")
    else:
        for r in failed:
            lines.append(
                f"- [{r['id']}] critical={r['critical']} strict={r['ok_strict']} loose={r['ok_loose']} "
                f"| text={r['text']} | strict_got={r['got_strict']} strict_exp={r['expect_strict']} "
                f"| loose_got={r['got_loose']} loose_exp={r['expect_loose']}"
            )
    lines.append("")
    lines.append("## Summary By Category")
    by_cat = {}
    for r in rows:
        cat = r["category"]
        stat = by_cat.setdefault(cat, {"count": 0, "strict_ok": 0, "loose_ok": 0})
        stat["count"] += 1
        stat["strict_ok"] += 1 if r["ok_strict"] else 0
        stat["loose_ok"] += 1 if r["ok_loose"] else 0
    for k, v in sorted(by_cat.items()):
        lines.append(f"- {k}: strict {v['strict_ok']}/{v['count']}, loose {v['loose_ok']}/{v['count']}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if output_json:
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "profile": profile,
                    "total": len(rows),
                    "strict_pass": strict_pass,
                    "loose_pass": loose_pass,
                    "critical_total": critical_total,
                    "strict_critical_pass": strict_critical_pass,
                    "loose_critical_pass": loose_critical_pass,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print(f"offline_report_md={md_path}")
    if output_json:
        print(f"offline_report_json={json_path}")
    print(f"strict_pass={strict_pass}/{len(rows)} loose_pass={loose_pass}/{len(rows)}")


def parse_log_minutes(minutes=30):
    if not LOG_FILE.exists():
        print("log_not_found")
        return

    since = datetime.now() - timedelta(minutes=minutes)
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
    cand_re = re.compile(r"收到口令候选\[(.*?)\]:\s*(.*)$")
    act_re = re.compile(r"运营动作触发:\s*source=([^,]+),\s*action=([^,]+),\s*ok=(True|False)")

    total_lines = 0
    candidate_count = 0
    action_count = 0
    action_ok = 0
    start_failed = 0
    perm_failed = 0
    by_source = {}
    by_action = {}
    recent_candidates = []

    for raw in LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        m_ts = ts_re.match(raw)
        if not m_ts:
            continue
        try:
            ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            continue
        if ts < since:
            continue
        total_lines += 1

        if "语音口令监听启动失败" in raw:
            start_failed += 1
        if "语音通道不可用" in raw:
            perm_failed += 1

        m_c = cand_re.search(raw)
        if m_c:
            source = m_c.group(1).strip()
            text = m_c.group(2).strip()
            candidate_count += 1
            by_source[source] = by_source.get(source, 0) + 1
            recent_candidates.append((ts.strftime("%H:%M:%S"), source, text))
            recent_candidates = recent_candidates[-10:]

        m_a = act_re.search(raw)
        if m_a:
            source = m_a.group(1).strip()
            action = m_a.group(2).strip()
            ok = m_a.group(3).strip() == "True"
            action_count += 1
            if ok:
                action_ok += 1
            by_source[source] = by_source.get(source, 0) + 1
            by_action[action] = by_action.get(action, 0) + 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = REPORT_DIR / f"log_scan_{minutes}m_{now}.md"
    lines = []
    lines.append(f"# Voice Stress Log Scan ({minutes}m)")
    lines.append("")
    lines.append(f"- Since: {since.isoformat(timespec='seconds')}")
    lines.append(f"- Parsed Lines: {total_lines}")
    lines.append(f"- Candidates: {candidate_count}")
    lines.append(f"- Actions: {action_count}")
    lines.append(f"- Action OK: {action_ok}")
    lines.append(f"- Start Failed: {start_failed}")
    lines.append(f"- Channel Unavailable Warnings: {perm_failed}")
    lines.append("")
    lines.append("## By Source")
    if by_source:
        for k, v in sorted(by_source.items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## By Action")
    if by_action:
        for k, v in sorted(by_action.items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Recent Candidates")
    if recent_candidates:
        for t, s, txt in recent_candidates:
            lines.append(f"- {t} [{s}] {txt}")
    else:
        lines.append("- none")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"log_scan_report={md_path}")
    print(f"candidates={candidate_count} actions={action_count} action_ok={action_ok}")


def export_manual_script(profile):
    cases = filter_cases(load_cases(), profile)
    out = ROOT / "stress" / "voice" / f"manual_script_{profile}.txt"
    lines = []
    lines.append(f"# Voice Stress Manual Script ({profile})")
    lines.append("# 逐条朗读或TTS播放，观察 logs/app.log 的候选与动作日志")
    lines.append("")
    for c in cases:
        lines.append(f"[{c.get('id')}] {c.get('text')}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"manual_script={out}")


def main():
    parser = argparse.ArgumentParser(description="Voice stress test pack runner.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_offline = sub.add_parser("offline", help="离线校验语音口令解析逻辑（严格/宽松）")
    p_offline.add_argument("--profile", default="all", choices=["all", "quick", "positive", "negative", "zh", "en"])
    p_offline.add_argument("--json", action="store_true", help="同时输出 json 详情")

    p_log = sub.add_parser("log-scan", help="扫描最近日志中的语音候选与动作触发")
    p_log.add_argument("--minutes", type=int, default=30)

    p_script = sub.add_parser("export-script", help="导出人工朗读脚本")
    p_script.add_argument("--profile", default="all", choices=["all", "quick", "positive", "negative", "zh", "en"])

    args = parser.parse_args()
    if args.cmd == "offline":
        run_offline(args.profile, output_json=args.json)
    elif args.cmd == "log-scan":
        parse_log_minutes(args.minutes)
    elif args.cmd == "export-script":
        export_manual_script(args.profile)


if __name__ == "__main__":
    main()
