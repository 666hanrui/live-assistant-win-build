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

若需要把本地 Embedding 模型一并打进发布包，可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1 `
  -EmbeddingModelDir "D:\models\embedding_model"
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

## GitHub Actions 自动发布

工作流文件：

`/.github/workflows/build-win-exe.yml`

触发规则：

1. 推送到 `main`
   - 自动构建 Windows EXE
   - 自动上传 zip 包到 Actions artifact
   - 不创建 GitHub Release
2. 推送标签 `v*`
   - 自动构建 Windows EXE
   - 自动打 zip
   - 自动创建或更新对应 GitHub Release
3. 手动触发 `workflow_dispatch`
   - 可选 `publish_release=true`
   - 发布时需要提供 `tag_name`

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

## 语音与 OCR 配置
- 当前语音链路固定为浏览器 `web_speech`
- 当前 OCR 链路固定为 Qwen 云端 OCR
- EXE 打包时会在 `dist/AI_Live_Assistant/.env` 缺省补齐：
  - `VOICE_COMMAND_INPUT_MODE=web_speech`
  - `QWEN_OCR_ENABLED=true`
  - `QWEN_OCR_MODEL=qwen-vl-ocr-latest`
  - `QWEN_OCR_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`
- 若语音不可用，先检查：
  1) Windows 隐私设置中的麦克风权限
  2) 浏览器站点权限中的麦克风授权
  3) 运行时 `.env` 中 `VOICE_COMMAND_ENABLED=true`
  4) 运行时 `.env` 中 `QWEN_OCR_API_KEY` 已填写

## 常见问题
1. `web_speech_unavailable`
   - 说明浏览器 Web Speech 不可用，优先检查 Chrome/Edge 版本和站点权限。

2. 启动后访问不到控制台
   - 检查端口占用：`8501`
   - 若双击无反应，先运行 `run_assistant_debug.bat` 查看日志。
   - 若是源码模式运行，再使用：
     - `python scripts/dashboard_service.py restart --force-port`

3. 页面提示 `Connection error / Streamlit server is not responding`
   - 该提示通常是本地 `127.0.0.1:<端口>` 未连通，不是外网故障。
   - 先运行 `run_assistant_debug.bat`，检查：
     - `exe_boot.log`
     - `logs/launcher_boot.log`
   - 确认 `.env` 中 `DASHBOARD_HOST` 与实际访问地址一致（推荐 `127.0.0.1`）。
   - 若机器较慢可增大：`DASHBOARD_OPEN_BROWSER_TIMEOUT_SECONDS=90`

4. 启动慢
   - 已默认启用 Embedding 本地优先 + 快速降级策略；
   - 首次加载仍可能受机器性能影响。
