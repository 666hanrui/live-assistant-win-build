import json
import math
import re
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from utils.logger import logger


class AnalyticsAgent:
    def __init__(
        self,
        events_file="data/analytics/danmu_events.jsonl",
        reports_dir="data/reports",
        state_file="data/reports/report_state.json",
    ):
        self.events_file = Path(events_file)
        self.reports_dir = Path(reports_dir)
        self.state_file = Path(state_file)
        self._lock = threading.Lock()
        self._ensure_paths()

    def _ensure_paths(self):
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "daily").mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "weekly").mkdir(parents=True, exist_ok=True)

    def record_danmu_event(self, event):
        payload = {
            "timestamp": event.get("timestamp") or datetime.now().isoformat(timespec="seconds"),
            "user": str(event.get("user") or "未知用户").strip(),
            "text": str(event.get("text") or "").strip(),
            "status": str(event.get("status") or "unknown").strip(),
            "reply": str(event.get("reply") or "").strip(),
            "action": str(event.get("action") or "").strip(),
            "language": str(event.get("language") or "").strip(),
            "llm_candidate": bool(event.get("llm_candidate", False)),
            "processing_ms": int(event.get("processing_ms") or 0),
        }
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _load_state(self):
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _event_dt(self, event):
        raw = event.get("timestamp")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except Exception:
            return None

    def _iter_events(self):
        if not self.events_file.exists():
            return []
        events = []
        for raw in self.events_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append(item)
            except Exception:
                continue
        return events

    def _events_between(self, start_dt, end_dt, events=None):
        source_events = events if events is not None else self._iter_events()
        events = []
        for ev in source_events:
            ev_dt = self._event_dt(ev)
            if not ev_dt:
                continue
            if start_dt <= ev_dt < end_dt:
                events.append(ev)
        return events

    def _safe_rate(self, num, den):
        try:
            den = float(den)
            if den <= 0:
                return 0.0
            return round(float(num) / den, 4)
        except Exception:
            return 0.0

    def _percentile(self, values, q):
        if not values:
            return 0.0
        data = sorted(float(v) for v in values)
        if len(data) == 1:
            return round(data[0], 2)
        q = max(0.0, min(1.0, float(q)))
        pos = (len(data) - 1) * q
        low = int(math.floor(pos))
        high = int(math.ceil(pos))
        if low == high:
            return round(data[low], 2)
        frac = pos - low
        return round(data[low] * (1 - frac) + data[high] * frac, 2)

    def _is_question(self, text):
        text = (text or "").strip().lower()
        if not text:
            return False
        if "?" in text or "？" in text:
            return True
        hints = [
            "多少", "什么", "怎么", "几号", "有没有", "可不可以", "是否", "能不能",
            "what", "how", "when", "where", "which", "can you", "do you", "is it", "are there",
        ]
        return any(h in text for h in hints)

    def _normalize_question(self, text):
        text = (text or "").strip().lower()
        text = re.sub(r"^@\S+\s*", "", text)
        text = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:80]

    def _message_intents(self, text):
        text = (text or "").lower()
        ascii_text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        intents = set()
        rules = {
            "价格优惠": ["价格", "多少钱", "优惠", "折扣", "券", "price", "discount", "coupon", "deal", "cost", "cheap"],
            "商品咨询": ["尺码", "尺寸", "材质", "颜色", "质量", "size", "fit", "material", "quality", "color", "fabric"],
            "物流售后": ["发货", "物流", "退换", "售后", "shipping", "delivery", "refund", "return", "warranty"],
            "互动陪伴": ["hi", "hello", "love", "nice", "wow", "amazing", "加油", "支持", "喜欢", "哈哈", "666", "cute"],
            "运营动作": ["置顶", "链接", "秒杀", "上架", "pin", "top", "feature", "flash", "promotion", "sale"],
        }
        for intent, words in rules.items():
            hit = False
            for w in words:
                w = (w or "").strip().lower()
                if not w:
                    continue
                if re.search(r"[a-z]", w):
                    if re.search(rf"(^|\s){re.escape(w)}(\s|$)", ascii_text):
                        hit = True
                        break
                else:
                    if w in text:
                        hit = True
                        break
            if hit:
                intents.add(intent)
        if not intents:
            intents.add("泛互动")
        return intents

    def _classify_user_type(self, intent_counter):
        if not intent_counter:
            return "泛互动型"
        if intent_counter.get("价格优惠", 0) + intent_counter.get("商品咨询", 0) >= 3:
            return "购买决策型"
        if intent_counter.get("物流售后", 0) >= 2:
            return "售后保障型"
        if intent_counter.get("互动陪伴", 0) >= 2 and intent_counter.get("价格优惠", 0) == 0:
            return "互动陪伴型"
        if intent_counter.get("运营动作", 0) >= 1:
            return "运营指令型"
        return "泛互动型"

    def _analyze(self, events):
        total = len(events)
        users = Counter()
        status_counter = Counter()
        intent_counter = Counter()
        language_counter = Counter()
        action_counter = Counter()
        question_counter = Counter()
        question_samples = {}
        hour_counter = Counter()
        processing_values = []
        user_intent_map = defaultdict(Counter)
        user_question_count = Counter()
        llm_candidate_count = 0

        for ev in events:
            user = str(ev.get("user") or "未知用户").strip()
            text = str(ev.get("text") or "").strip()
            status = str(ev.get("status") or "").strip()
            language = str(ev.get("language") or "").strip()
            action = str(ev.get("action") or "").strip()
            dt = self._event_dt(ev)
            if dt:
                hour_counter[f"{dt.hour:02d}:00"] += 1
            users[user] += 1
            status_counter[status] += 1
            if language:
                language_counter[language] += 1
            if action:
                action_counter[action] += 1
            if bool(ev.get("llm_candidate", False)):
                llm_candidate_count += 1

            try:
                val = int(ev.get("processing_ms") or 0)
                if val > 0:
                    processing_values.append(val)
            except Exception:
                pass

            intents = self._message_intents(text)
            for it in intents:
                intent_counter[it] += 1
                user_intent_map[user][it] += 1

            if self._is_question(text):
                user_question_count[user] += 1
                norm_q = self._normalize_question(text)
                if norm_q:
                    question_counter[norm_q] += 1
                    question_samples.setdefault(norm_q, text)

        replied = status_counter.get("replied", 0)
        avg_processing = round(sum(processing_values) / len(processing_values), 2) if processing_values else 0.0
        p50_processing = self._percentile(processing_values, 0.5)
        p90_processing = self._percentile(processing_values, 0.9)
        peak_hours = [x[0] for x in hour_counter.most_common(3)]

        user_type_counter = Counter()
        top_users = []
        for user, msg_count in users.most_common(20):
            utype = self._classify_user_type(user_intent_map[user])
            user_type_counter[utype] += 1
            top_users.append(
                {
                    "user": user,
                    "messages": msg_count,
                    "questions": int(user_question_count[user]),
                    "type": utype,
                }
            )

        top_questions = []
        for norm_q, count in question_counter.most_common(8):
            top_questions.append(
                {
                    "question": question_samples.get(norm_q, norm_q),
                    "count": count,
                }
            )

        return {
            "total_messages": total,
            "unique_users": len(users),
            "replied_messages": replied,
            "reply_rate": self._safe_rate(replied, total),
            "question_messages": int(sum(question_counter.values())),
            "question_rate": self._safe_rate(sum(question_counter.values()), total),
            "llm_candidate_messages": int(llm_candidate_count),
            "llm_candidate_rate": self._safe_rate(llm_candidate_count, total),
            "avg_processing_ms": avg_processing,
            "p50_processing_ms": p50_processing,
            "p90_processing_ms": p90_processing,
            "top_questions": top_questions,
            "intent_counts": dict(intent_counter.most_common()),
            "status_counts": dict(status_counter.most_common()),
            "language_counts": dict(language_counter.most_common()),
            "action_counts": dict(action_counter.most_common()),
            "user_type_counts": dict(user_type_counter.most_common()),
            "top_users": top_users[:10],
            "peak_hours": peak_hours,
            "hourly_distribution": {f"{h:02d}:00": hour_counter.get(f"{h:02d}:00", 0) for h in range(24)},
        }

    def _build_recommendations(self, analysis):
        suggestions = []
        intents = analysis.get("intent_counts", {})
        reply_rate = float(analysis.get("reply_rate") or 0.0)
        question_rate = float(analysis.get("question_rate") or 0.0)
        peak_hours = analysis.get("peak_hours", [])

        if intents.get("价格优惠", 0) >= 3:
            suggestions.append("价格与优惠问题偏多：建议每10-15分钟固定口播“价格+优惠+限时机制”，并在公屏置顶。")
        if intents.get("商品咨询", 0) >= 3:
            suggestions.append("商品咨询高频：建议增加“尺码/材质/颜色”三段式FAQ，并在直播间反复演示细节。")
        if intents.get("物流售后", 0) >= 2:
            suggestions.append("物流售后关注度上升：建议设置固定话术，明确发货时效、退换条件、客服路径。")
        if reply_rate < 0.35 and question_rate > 0.2:
            suggestions.append("回复覆盖率偏低：建议提升关键词模板覆盖，减少非关键弹幕占用，优先回答高意图提问。")
        if analysis.get("avg_processing_ms", 0) > 1200:
            suggestions.append("平均处理时延偏高：建议继续降低 LLM 超时，并对高峰期仅处理最新问题（已启用）。")
        if float(analysis.get("p90_processing_ms") or 0.0) > 1800:
            suggestions.append("P90 时延偏高：建议进一步收紧高峰期 LLM 触发条件，优先处理高意图问题。")
        if peak_hours:
            suggestions.append(f"互动高峰时段集中在 {', '.join(peak_hours)}：建议在该时段强化福利口播和互动引导。")
        if not suggestions:
            suggestions.append("当前互动结构平稳：建议保持现有节奏，持续补充知识库并迭代关键词模板。")
        return suggestions

    def _counter_to_series(self, counter_dict, name_key="name", value_key="count", limit=10):
        data = []
        if not isinstance(counter_dict, dict):
            return data
        for k, v in sorted(counter_dict.items(), key=lambda x: x[1], reverse=True)[: max(1, int(limit))]:
            data.append({name_key: k, value_key: int(v)})
        return data

    def _build_daily_series(self, events, start_date, end_date):
        days = []
        cursor = start_date
        daily_counter = {}
        while cursor <= end_date:
            k = cursor.isoformat()
            daily_counter[k] = {"messages": 0, "replied": 0, "questions": 0, "avg_processing_ms": 0.0}
            cursor += timedelta(days=1)

        processing_map = defaultdict(list)
        for ev in events:
            dt = self._event_dt(ev)
            if not dt:
                continue
            k = dt.date().isoformat()
            if k not in daily_counter:
                continue
            text = str(ev.get("text") or "").strip()
            status = str(ev.get("status") or "").strip()
            daily_counter[k]["messages"] += 1
            if status == "replied":
                daily_counter[k]["replied"] += 1
            if self._is_question(text):
                daily_counter[k]["questions"] += 1
            try:
                ms = int(ev.get("processing_ms") or 0)
                if ms > 0:
                    processing_map[k].append(ms)
            except Exception:
                pass

        cursor = start_date
        while cursor <= end_date:
            k = cursor.isoformat()
            item = daily_counter[k]
            item["date"] = k
            item["reply_rate"] = self._safe_rate(item["replied"], item["messages"])
            item["question_rate"] = self._safe_rate(item["questions"], item["messages"])
            vals = processing_map.get(k, [])
            item["avg_processing_ms"] = round(sum(vals) / len(vals), 2) if vals else 0.0
            days.append(item)
            cursor += timedelta(days=1)
        return days

    def _build_hourly_series(self, events):
        hourly = {h: {"hour": f"{h:02d}:00", "messages": 0, "replied": 0, "questions": 0} for h in range(24)}
        for ev in events:
            dt = self._event_dt(ev)
            if not dt:
                continue
            idx = dt.hour
            text = str(ev.get("text") or "").strip()
            status = str(ev.get("status") or "").strip()
            hourly[idx]["messages"] += 1
            if status == "replied":
                hourly[idx]["replied"] += 1
            if self._is_question(text):
                hourly[idx]["questions"] += 1
        out = [hourly[h] for h in range(24)]
        for item in out:
            item["reply_rate"] = self._safe_rate(item["replied"], item["messages"])
            item["question_rate"] = self._safe_rate(item["questions"], item["messages"])
        return out

    def get_dashboard_data(self, start_date=None, end_date=None):
        """
        返回报表看板数据（用于 dashboard 图表展示）。
        """
        end_date = end_date or date.today()
        start_date = start_date or (end_date - timedelta(days=6))
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        period_days = (end_date - start_date).days + 1
        prev_end = start_date - timedelta(days=1)
        prev_start = prev_end - timedelta(days=period_days - 1)

        all_events = self._iter_events()
        cur_start_dt = datetime.combine(start_date, dtime.min)
        cur_end_dt = datetime.combine(end_date + timedelta(days=1), dtime.min)
        prev_start_dt = datetime.combine(prev_start, dtime.min)
        prev_end_dt = datetime.combine(prev_end + timedelta(days=1), dtime.min)

        cur_events = self._events_between(cur_start_dt, cur_end_dt, events=all_events)
        prev_events = self._events_between(prev_start_dt, prev_end_dt, events=all_events)

        current = self._analyze(cur_events)
        previous = self._analyze(prev_events)
        daily_series = self._build_daily_series(cur_events, start_date, end_date)
        hourly_series = self._build_hourly_series(cur_events)

        return {
            "range": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "days": period_days,
            },
            "compare_range": {
                "start_date": prev_start.isoformat(),
                "end_date": prev_end.isoformat(),
                "days": period_days,
            },
            "current": current,
            "previous": previous,
            "daily_series": daily_series,
            "hourly_series": hourly_series,
            "intent_series": self._counter_to_series(current.get("intent_counts", {}), name_key="intent", limit=12),
            "user_type_series": self._counter_to_series(current.get("user_type_counts", {}), name_key="user_type", limit=10),
            "status_series": self._counter_to_series(current.get("status_counts", {}), name_key="status", limit=12),
            "language_series": self._counter_to_series(current.get("language_counts", {}), name_key="language", limit=8),
            "action_series": self._counter_to_series(current.get("action_counts", {}), name_key="action", limit=10),
            "top_questions": list(current.get("top_questions", [])),
            "top_users": list(current.get("top_users", [])),
            "recommendations": self._build_recommendations(current),
        }

    def _render_report(self, report_type, label, start_dt, end_dt, analysis):
        suggestions = self._build_recommendations(analysis)
        lines = []
        lines.append(f"# {label}{'日报' if report_type == 'daily' else '周报'}")
        lines.append("")
        lines.append(f"- 时间范围：{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}")
        lines.append("")
        lines.append("## 核心指标")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | ---: |")
        lines.append(f"| 弹幕总量 | {analysis['total_messages']} |")
        lines.append(f"| 独立用户 | {analysis['unique_users']} |")
        lines.append(f"| 问题弹幕占比 | {round(analysis['question_rate'] * 100, 2)}% |")
        lines.append(f"| 回复覆盖率 | {round(analysis['reply_rate'] * 100, 2)}% |")
        lines.append(f"| 平均处理耗时 | {analysis['avg_processing_ms']} ms |")
        lines.append(f"| P50 处理耗时 | {analysis.get('p50_processing_ms', 0)} ms |")
        lines.append(f"| P90 处理耗时 | {analysis.get('p90_processing_ms', 0)} ms |")
        lines.append(f"| LLM 候选占比 | {round(float(analysis.get('llm_candidate_rate', 0.0)) * 100, 2)}% |")
        lines.append("")
        lines.append("## 高频共性问题")
        if analysis["top_questions"]:
            for idx, item in enumerate(analysis["top_questions"], 1):
                lines.append(f"{idx}. {item['question']}（{item['count']}次）")
        else:
            lines.append("1. 本周期暂无明显重复问题。")
        lines.append("")
        lines.append("## 用户类型分布")
        if analysis["user_type_counts"]:
            for k, v in analysis["user_type_counts"].items():
                lines.append(f"- {k}：{v}人")
        else:
            lines.append("- 暂无用户类型数据。")
        lines.append("")
        lines.append("## 意图分布")
        if analysis.get("intent_counts"):
            for k, v in analysis["intent_counts"].items():
                lines.append(f"- {k}：{v}")
        else:
            lines.append("- 暂无意图数据。")
        lines.append("")
        lines.append("## 时段分布（Top 6）")
        hourly = analysis.get("hourly_distribution", {}) or {}
        top_hourly = sorted(hourly.items(), key=lambda x: x[1], reverse=True)[:6]
        if top_hourly and top_hourly[0][1] > 0:
            for h, c in top_hourly:
                lines.append(f"- {h}：{c}条")
        else:
            lines.append("- 本周期暂无有效时段数据。")
        lines.append("")
        lines.append("## 关键用户样本")
        if analysis["top_users"]:
            for item in analysis["top_users"][:8]:
                lines.append(
                    f"- {item['user']}：{item['messages']}条，提问{item['questions']}条，类型={item['type']}"
                )
        else:
            lines.append("- 暂无样本。")
        lines.append("")
        lines.append("## 直播优化建议")
        for idx, tip in enumerate(suggestions, 1):
            lines.append(f"{idx}. {tip}")
        lines.append("")
        lines.append("## 数据口径")
        lines.append("- 数据来源：系统监听到的弹幕事件（含状态与处理耗时）。")
        lines.append("- 本报告由规则分析生成，用于运营决策参考。")
        lines.append("")
        return "\n".join(lines)

    def generate_daily_report(self, target_date=None):
        target_date = target_date or date.today()
        start_dt = datetime.combine(target_date, dtime.min)
        end_dt = start_dt + timedelta(days=1)
        events = self._events_between(start_dt, end_dt)
        analysis = self._analyze(events)
        analysis["daily_series"] = self._build_daily_series(events, target_date, target_date)
        analysis["hourly_series"] = self._build_hourly_series(events)
        label = target_date.strftime("%Y-%m-%d")
        content = self._render_report("daily", label, start_dt, end_dt, analysis)
        output = self.reports_dir / "daily" / f"{label}.md"
        output.write_text(content, encoding="utf-8")
        logger.info(f"日报已生成: {output} | events={len(events)}")
        return {
            "type": "daily",
            "label": label,
            "path": str(output),
            "event_count": len(events),
            "analysis": analysis,
        }

    def generate_weekly_report(self, start_date, end_date):
        start_dt = datetime.combine(start_date, dtime.min)
        end_dt = datetime.combine(end_date + timedelta(days=1), dtime.min)
        events = self._events_between(start_dt, end_dt)
        iso = start_date.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
        analysis = self._analyze(events)
        analysis["daily_series"] = self._build_daily_series(events, start_date, end_date)
        analysis["hourly_series"] = self._build_hourly_series(events)
        content = self._render_report("weekly", label, start_dt, end_dt, analysis)
        output = self.reports_dir / "weekly" / f"{label}.md"
        output.write_text(content, encoding="utf-8")
        logger.info(f"周报已生成: {output} | events={len(events)}")
        return {
            "type": "weekly",
            "label": label,
            "path": str(output),
            "event_count": len(events),
            "analysis": analysis,
        }

    def generate_current_week_report(self):
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        return self.generate_weekly_report(week_start, today)

    def generate_last_full_week_report(self):
        today = date.today()
        this_week_start = today - timedelta(days=today.weekday())
        last_week_start = this_week_start - timedelta(days=7)
        last_week_end = this_week_start - timedelta(days=1)
        return self.generate_weekly_report(last_week_start, last_week_end)

    def maybe_generate_periodic_reports(self):
        created = []
        with self._lock:
            state = self._load_state()

        yesterday = date.today() - timedelta(days=1)
        y_key = yesterday.isoformat()
        if state.get("last_daily") != y_key:
            result = self.generate_daily_report(yesterday)
            state["last_daily"] = y_key
            created.append(result["path"])

        this_week_start = date.today() - timedelta(days=date.today().weekday())
        last_week_start = this_week_start - timedelta(days=7)
        week_key = f"{last_week_start.isocalendar().year}-W{last_week_start.isocalendar().week:02d}"
        if state.get("last_weekly") != week_key and date.today() >= this_week_start:
            result = self.generate_last_full_week_report()
            state["last_weekly"] = week_key
            created.append(result["path"])

        with self._lock:
            self._save_state(state)
        return created

    def list_reports(self, limit=30):
        files = []
        for p in (self.reports_dir / "daily").glob("*.md"):
            files.append(p)
        for p in (self.reports_dir / "weekly").glob("*.md"):
            files.append(p)
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return [str(p) for p in files[:limit]]

    def load_report_text(self, report_path):
        path = Path(report_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
