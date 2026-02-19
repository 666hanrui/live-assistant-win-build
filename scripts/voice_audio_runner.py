#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "stress" / "voice" / "test_cases.json"


def load_cases(cases_path):
    data = json.loads(Path(cases_path).read_text(encoding="utf-8"))
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


def ensure_macos():
    if sys.platform != "darwin":
        raise RuntimeError("This command is for macOS only.")


def ensure_windows():
    if os.name != "nt":
        raise RuntimeError("This command is for Windows only.")


def cmd_generate_mac(profile, cases_path, out_dir, voice_zh, voice_en, rate):
    ensure_macos()
    cases = filter_cases(load_cases(cases_path), profile)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []

    for c in cases:
        cid = str(c.get("id", "")).strip()
        text = str(c.get("text", "")).strip()
        lang = str(c.get("lang", "")).strip()
        if not cid or not text:
            continue

        voice = voice_zh if lang.startswith("zh") else voice_en
        audio_file = out / f"{cid}.aiff"
        cmd = ["say", "-v", voice, "-r", str(rate), "-o", str(audio_file), text]
        subprocess.run(cmd, check=True)
        manifest.append({"id": cid, "text": text, "lang": lang, "voice": voice, "file": str(audio_file)})
        print(f"generated {cid} -> {audio_file}")

    mf = out / "manifest.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest={mf}")


def cmd_play_mac(profile, cases_path, audio_dir, rounds, gap_seconds):
    ensure_macos()
    cases = filter_cases(load_cases(cases_path), profile)
    audio_root = Path(audio_dir)
    if not audio_root.exists():
        raise RuntimeError(f"audio dir not found: {audio_root}")

    for r in range(1, rounds + 1):
        print(f"round {r}/{rounds}")
        for c in cases:
            cid = str(c.get("id", "")).strip()
            text = str(c.get("text", "")).strip()
            if not cid:
                continue
            aiff = audio_root / f"{cid}.aiff"
            wav = audio_root / f"{cid}.wav"
            if aiff.exists():
                audio = aiff
            elif wav.exists():
                audio = wav
            else:
                print(f"skip {cid}: no audio file")
                continue

            print(f"play [{cid}] {text}")
            subprocess.run(["afplay", str(audio)], check=True)
            if gap_seconds > 0:
                time.sleep(gap_seconds)


def cmd_generate_win(profile, cases_path, out_dir, rate):
    ensure_windows()
    script = ROOT / "stress" / "voice" / "windows_tts_generate.ps1"
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-CasesPath",
        str(cases_path),
        "-OutDir",
        str(out_dir),
        "-Profile",
        str(profile),
        "-Rate",
        str(int(rate)),
    ]
    subprocess.run(cmd, check=True)


def cmd_play_win(cases_path, audio_dir, rounds, gap_seconds):
    ensure_windows()
    script = ROOT / "stress" / "voice" / "windows_playback_loop.ps1"
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-CasesPath",
        str(cases_path),
        "-AudioDir",
        str(audio_dir),
        "-Rounds",
        str(int(rounds)),
        "-GapSeconds",
        str(max(0, int(gap_seconds))),
    ]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="Generate and play voice stress audio.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate-mac", help="Generate aiff test audio on macOS using say")
    p_gen.add_argument("--profile", default="quick", choices=["all", "quick", "positive", "negative", "zh", "en"])
    p_gen.add_argument("--cases-path", default=str(CASES_PATH))
    p_gen.add_argument("--out-dir", default=str(ROOT / "stress" / "voice" / "audio_mac"))
    p_gen.add_argument("--voice-zh", default="Ting-Ting")
    p_gen.add_argument("--voice-en", default="Samantha")
    p_gen.add_argument("--rate", type=int, default=185)

    p_play = sub.add_parser("play-mac", help="Play generated stress audio on macOS using afplay")
    p_play.add_argument("--profile", default="quick", choices=["all", "quick", "positive", "negative", "zh", "en"])
    p_play.add_argument("--cases-path", default=str(CASES_PATH))
    p_play.add_argument("--audio-dir", default=str(ROOT / "stress" / "voice" / "audio_mac"))
    p_play.add_argument("--rounds", type=int, default=1)
    p_play.add_argument("--gap-seconds", type=float, default=2.0)

    p_gen_win = sub.add_parser("generate-win", help="Generate wav test audio on Windows using PowerShell TTS")
    p_gen_win.add_argument("--profile", default="quick", choices=["all", "quick", "positive", "negative", "zh", "en"])
    p_gen_win.add_argument("--cases-path", default=str(CASES_PATH))
    p_gen_win.add_argument("--out-dir", default=str(ROOT / "stress" / "voice" / "audio"))
    p_gen_win.add_argument("--rate", type=int, default=0)

    p_play_win = sub.add_parser("play-win", help="Play generated stress audio on Windows")
    p_play_win.add_argument("--cases-path", default=str(CASES_PATH))
    p_play_win.add_argument("--audio-dir", default=str(ROOT / "stress" / "voice" / "audio"))
    p_play_win.add_argument("--rounds", type=int, default=1)
    p_play_win.add_argument("--gap-seconds", type=float, default=2.0)

    args = parser.parse_args()
    if args.cmd == "generate-mac":
        cmd_generate_mac(
            profile=args.profile,
            cases_path=args.cases_path,
            out_dir=args.out_dir,
            voice_zh=args.voice_zh,
            voice_en=args.voice_en,
            rate=args.rate,
        )
    elif args.cmd == "play-mac":
        cmd_play_mac(
            profile=args.profile,
            cases_path=args.cases_path,
            audio_dir=args.audio_dir,
            rounds=args.rounds,
            gap_seconds=args.gap_seconds,
        )
    elif args.cmd == "generate-win":
        cmd_generate_win(
            profile=args.profile,
            cases_path=args.cases_path,
            out_dir=args.out_dir,
            rate=args.rate,
        )
    elif args.cmd == "play-win":
        cmd_play_win(
            cases_path=args.cases_path,
            audio_dir=args.audio_dir,
            rounds=args.rounds,
            gap_seconds=args.gap_seconds,
        )


if __name__ == "__main__":
    main()
