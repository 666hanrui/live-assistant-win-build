# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve().parent


def add_path(rel_path: str, dst: str = "."):
    src = PROJECT_ROOT / rel_path
    if src.exists():
        return [(str(src), dst)]
    return []


datas = []
datas += add_path("dashboard.py")
datas += add_path(".env")
datas += add_path(".env.example")
datas += add_path("README.md")
datas += add_path("文档.txt")

for folder in ("docs", "assets", "data", "config", "models"):
    datas += add_path(folder, folder)

datas += collect_data_files("streamlit")
datas += collect_data_files("altair")
datas += collect_data_files("pydeck")
datas += collect_data_files("pygments")
datas += collect_data_files("tiktoken")

hiddenimports = [
    "dashboard",
    "main",
    "config.settings",
]
for pkg in (
    "agents",
    "utils",
    "config",
    "streamlit",
    "langchain",
    "langchain_community",
    "langchain_openai",
    "chromadb",
    "speech_recognition",
    "DrissionPage",
    "openpyxl",
):
    try:
        hiddenimports.extend(collect_submodules(pkg))
    except Exception:
        hiddenimports.append(pkg)

hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    ["app_launcher.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI_Live_Assistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="AI_Live_Assistant",
)
