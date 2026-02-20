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
或（无控制台窗口）
- `run_assistant_silent.vbs`

## 首次上线配置
1. 编辑运行时 `.env`（程序会在首次启动时自动生成）
   - 优先：`%LOCALAPPDATA%\AI_Live_Assistant\.env`（当 EXE 目录不可写时）
   - 便携目录可写时：`dist/AI_Live_Assistant/.env`
2. 至少填写：
   - `LLM_API_KEY`
   - `LLM_BASE_URL`
   - `LLM_MODEL_NAME`
3. 根据机器修改浏览器路径（如需）：
   - `CHROME_EXECUTABLE=...`

## 音频输入说明（麦克风/系统回采）
- 当前语音模式支持：
  - `python_asr`：物理麦克风采集
  - `system_loopback_asr`：系统回采（直接识别浏览器播放声音）
- EXE 打包时会在 `dist/AI_Live_Assistant/.env` 缺省补齐：
  - `VOICE_COMMAND_INPUT_MODE=system_loopback_asr`
  - `VOICE_LOOPBACK_DEVICE_INDEX=-1`
  - `VOICE_LOOPBACK_DEVICE_NAME_HINT=blackhole,stereo mix,loopback,vb-cable`
  - `VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK=false`
  - `VOICE_DASHSCOPE_MODEL=paraformer-realtime-v2`
  - `VOICE_DASHSCOPE_SAMPLE_RATE=16000`
- 若要启用阿里云 FunASR 兜底，再补充：
  - `VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK=true`
  - `VOICE_DASHSCOPE_API_KEY=<你的阿里云 Key>`
- 若语音不可用，先检查：
  1) Windows 隐私设置中的麦克风/桌面应用音频权限
  2) 是否安装并正确路由回采设备（Stereo Mix / VB-CABLE / BlackHole 等）
  3) 运行时 `.env` 中 `VOICE_COMMAND_ENABLED=true`

## 常见问题
1. `unsupported / missing_speech_recognition`
   - 说明依赖未安装完全，重新运行打包脚本。

2. `missing_pyaudio`
   - 语音本地采集需要 PyAudio，已在 `requirements.txt` 的 Windows 条件依赖中声明。
   - 若网络/镜像异常导致安装失败，手动执行：
     - `.\.venv\Scripts\python.exe -m pip install PyAudio`

3. 启动后访问不到控制台
   - 检查端口占用：`8501`
   - 若双击无反应，先运行 `run_assistant_debug.bat` 查看日志。
   - 若是源码模式运行，再使用：
     - `python scripts/dashboard_service.py restart --force-port`

4. 页面提示 `Connection error / Streamlit server is not responding`
   - 该提示通常是本地 `127.0.0.1:<端口>` 未连通，不是外网故障。
   - 先运行 `run_assistant_debug.bat`，检查：
     - `exe_boot.log`
     - `logs/launcher_boot.log`
   - 确认 `.env` 中 `DASHBOARD_HOST` 与实际访问地址一致（推荐 `127.0.0.1`）。
   - 若机器较慢可增大：`DASHBOARD_OPEN_BROWSER_TIMEOUT_SECONDS=90`

5. 启动慢
   - 已默认启用 Embedding 本地优先 + 快速降级策略；
   - 首次加载仍可能受机器性能影响。
