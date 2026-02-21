#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from statistics import median


def _safe_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _extract_metrics(item):
    target = item.get("target") or {}
    row_rect = target.get("row_rect") or {}
    panel_rect = target.get("panel_rect") or {}
    target_ocr = target.get("target_ocr_point") or {}
    row_center = target.get("row_center") or {}

    tx = _safe_float(target_ocr.get("x"))
    ty = _safe_float(target_ocr.get("y"))
    rcx = _safe_float(row_center.get("x"))
    rcy = _safe_float(row_center.get("y"))
    rx1 = _safe_float(row_rect.get("x1"))
    ry1 = _safe_float(row_rect.get("y1"))
    rx2 = _safe_float(row_rect.get("x2"))
    ry2 = _safe_float(row_rect.get("y2"))
    px1 = _safe_float(panel_rect.get("x1"))
    py1 = _safe_float(panel_rect.get("y1"))
    px2 = _safe_float(panel_rect.get("x2"))
    py2 = _safe_float(panel_rect.get("y2"))
    if None in (tx, ty, rcx, rcy, rx1, ry1, rx2, ry2, px1, py1, px2, py2):
        return {}

    row_h = max(0.0, ry2 - ry1)
    panel_w = max(0.0, px2 - px1)
    if row_h <= 1.0 or panel_w <= 1.0:
        return {}

    panel_x_ratio = (tx - px1) / panel_w
    panel_right_padding_ratio = (px2 - tx) / panel_w
    offset_y_ratio = (ty - rcy) / row_h
    return {
        "panel_x_ratio": panel_x_ratio,
        "panel_right_padding_ratio": panel_right_padding_ratio,
        "offset_y_ratio": offset_y_ratio,
        "x_source": str(target.get("x_source") or ""),
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize fixed-row pin click calibration logs.")
    parser.add_argument(
        "--log",
        default="data/reports/pin_click_calibration.jsonl",
        help="Path to calibration log jsonl.",
    )
    parser.add_argument(
        "--min-success",
        type=int,
        default=5,
        help="Minimum successful samples before printing recommended env values.",
    )
    args = parser.parse_args()

    path = Path(args.log)
    rows = _load_jsonl(path)
    if not rows:
        print(f"No calibration data: {path}")
        return

    total = len(rows)
    success = [r for r in rows if bool(r.get("ok"))]
    failed = total - len(success)

    print(f"Log file: {path}")
    print(f"Total records: {total}")
    print(f"Success: {len(success)}")
    print(f"Failed: {failed}")
    print(f"Success rate: {(len(success) / total * 100.0):.1f}%")

    by_source = {}
    for r in rows:
        source = str(((r.get("target") or {}).get("x_source") or "unknown")).strip() or "unknown"
        s = by_source.setdefault(source, {"total": 0, "ok": 0})
        s["total"] += 1
        if r.get("ok"):
            s["ok"] += 1
    if by_source:
        print("\nBy x_source:")
        for k in sorted(by_source.keys()):
            v = by_source[k]
            rate = (v["ok"] / v["total"] * 100.0) if v["total"] else 0.0
            print(f"  - {k}: {v['ok']}/{v['total']} ({rate:.1f}%)")

    metrics = []
    for r in success:
        m = _extract_metrics(r)
        if m:
            metrics.append(m)

    if len(metrics) < max(1, int(args.min_success)):
        print(
            f"\nNot enough successful samples for recommendation: "
            f"{len(metrics)} < {int(args.min_success)}"
        )
        return

    panel_x_ratio_med = median([m["panel_x_ratio"] for m in metrics])
    right_pad_ratio_med = median([m["panel_right_padding_ratio"] for m in metrics])
    offset_y_ratio_med = median([m["offset_y_ratio"] for m in metrics])

    panel_x_ratio_rec = _clip(panel_x_ratio_med, 0.55, 0.98)
    right_pad_ratio_rec = _clip(right_pad_ratio_med, 0.01, 0.45)
    offset_y_ratio_rec = _clip(offset_y_ratio_med, -0.60, 0.60)

    print("\nRecommended env values:")
    print(f"  OCR_PIN_FIXED_ROW_CLICK_PANEL_X_RATIO={panel_x_ratio_rec:.4f}")
    print(f"  OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_RATIO={right_pad_ratio_rec:.4f}")
    print(f"  OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_RATIO={offset_y_ratio_rec:.4f}")


if __name__ == "__main__":
    main()

