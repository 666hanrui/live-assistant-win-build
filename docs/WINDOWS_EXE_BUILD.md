# Windows EXE 打包与上线说明

## 目标
将项目打成可直接运行的 Windows 可执行包：
- `dist/AI_Live_Assistant/AI_Live_Assistant.exe`

## 前置条件（Windows 机器）
1. 安装 Python 3.9+（建议 3.10/3.11）
2. 已安装 Chrome
3. 能联网安装依赖（首次打包必须）

## 一键打包
在项目根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1
```

若需要把本地模型和本地增强依赖一并打进发布包，可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1 `
  -IncludeLocalDeps `
  -EmbeddingModelDir "D:\models\embedding_model" `
  -WhisperModelDir "D:\models\whisper_cache"
```

或双击：

`scripts/build_windows_exe.bat`

脚本会自动执行：
1. 创建/复用 `.venv`
2. 安装 `requirements.txt`
3. 安装 `pyinstaller`
4. 使用 `windows_exe.spec` 打包
5. 在 `dist/AI_Live_Assistant/` 下生成可运行目录
6. 自动复制配置文件：优先复制根目录 `.env`，否则复制 `.env.example`
7. 自动包含模型目录（会根据 `.env` 中模型路径和命令行参数复制到 `dist/AI_Live_Assistant/models/`）

## 运行
进入：

`dist/AI_Live_Assistant/`

运行：
- `AI_Live_Assistant.exe`
或
- `启动助手.bat`

## 首次上线配置
1. 编辑 `dist/AI_Live_Assistant/.env`
2. 至少填写：
   - `LLM_API_KEY`
   - `LLM_BASE_URL`
   - `LLM_MODEL_NAME`
3. 根据机器修改浏览器路径（如需）：
   - `CHROME_EXECUTABLE=...`

## 麦克风说明
- 当前语音模式已支持 `python_asr`（本地采集 + 本地/云 ASR）。
- 上线机器只需给系统层面麦克风权限（Chrome 页面权限不是必须）。
- 若语音不可用，先检查：
  1) Windows 隐私设置中的麦克风权限
  2) 声卡输入设备是否正确
  3) `.env` 中 `VOICE_COMMAND_ENABLED=true`

## 常见问题
1. `unsupported / missing_speech_recognition`
   - 说明依赖未安装完全，重新运行打包脚本。

2. `missing_pyaudio`
   - 语音本地采集需要 PyAudio，已在 `requirements.txt` 的 Windows 条件依赖中声明。
   - 若网络/镜像异常导致安装失败，手动执行：
     - `.\.venv\Scripts\python.exe -m pip install PyAudio`

3. 启动后访问不到控制台
   - 检查端口占用：`8501`
   - EXE 运行目录下没有 `scripts/dashboard_service.py`，请先关闭已运行的 `AI_Live_Assistant.exe` 后重启。
   - 若是源码模式运行，再使用：
     - `python scripts/dashboard_service.py restart --force-port`

4. 启动慢
   - 已默认启用 Embedding 本地优先 + 快速降级策略；
   - 首次加载仍可能受机器性能影响。
