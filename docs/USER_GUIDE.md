# AI 直播助手使用说明书

## 1. 使用前准备

- 安装依赖：`pip install -r requirements.txt`
- 本地优先（推荐）：`pip install -r requirements.txt -r requirements-local.txt`
- 配置环境变量：在项目根目录准备 `.env`
- 启动浏览器调试模式（Chrome/Edge）并登录 TikTok 直播页（系统会按 Chrome -> Edge 自动回退）

建议在 `.env` 开启本地优先：

- `LOCAL_FIRST_MODE=true`
- `VOICE_COMMAND_INPUT_MODE=python_asr`
- `VOICE_PYTHON_ASR_PROVIDER=whisper_local`
- `VOICE_ASR_ALLOW_GOOGLE_FALLBACK=false`
- `VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK=false`
- `EMBEDDING_LOCAL_FILES_ONLY=true`
- `EMBEDDING_ENABLE_ONLINE_FALLBACK=false`

## 2. 启动方式（推荐）

- 启动：`python scripts/dashboard_service.py start`
- 状态：`python scripts/dashboard_service.py status`
- 重启：`python scripts/dashboard_service.py restart --force-port`
- 日志：`python scripts/dashboard_service.py logs`
- 停止：`python scripts/dashboard_service.py stop`

默认访问地址：`http://127.0.0.1:8501`

## 3. 控制台主流程

在“主功能面板”按顺序执行：

1. 连接浏览器
2. 申请麦克风
3. 启动主监听
4. 运行自检

## 4. 统一语言与回复设置

在左侧“统一语言与回复设置”中可配置：

- 统一语言（回复/暖场/知识库问答）
- 语气模板（手动编写）
- 是否启用自动回复弹幕
- 是否启用自动暖场话术
- 是否启用语音口令监听

点击“应用回复设置”后会持久化，下次启动仍生效。

## 5. 语音功能说明

当前支持“本地麦克风采集 + Python ASR”模式：

- 需要系统麦克风权限
- 不依赖 TikTok 网页的麦克风权限
- 若语音状态异常，先检查系统默认输入设备是否存在
- 推荐 `ASR Provider=whisper_local`，可减少云端语音请求
- 若要阿里云兜底：设置 `VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK=true` 并配置 `VOICE_DASHSCOPE_API_KEY`

常见状态解释：

- `idle`：可用，等待输入
- `running`：监听中
- `denied/not-allowed`：系统权限未允许
- `unsupported`：本地语音依赖缺失

## 6. 常见故障排查

### 浏览器连接慢或失败

- 确认调试端口（默认 `9222`）未冲突
- 确认当前标签页是 TikTok 直播页（`.../@xxx/live`）
- 使用“强制重载系统”后重试

### 启动监听慢

- 先连接浏览器，再启动监听
- 在“运行监控”查看是否有重试日志
- 网络较差时会导致首轮连接延迟

### 没有自动回复/暖场

- 确认“启用自动回复弹幕/启用自动暖场话术”已开启
- 确认系统处于“运行中”
- 在“运行监控”查看是否命中去重或冷却策略

### 语音权限一直异常

- macOS：系统设置 -> 隐私与安全性 -> 麦克风，允许运行本项目的程序
- Windows：设置 -> 隐私和安全性 -> 麦克风，开启桌面应用访问麦克风

## 7. 调试页功能

- 运行监控：实时日志与状态
- 知识库调试：问答与导入知识文件
- 视觉调试：浏览器连接与页面元素识别
- 场控调试：模拟弹幕触发完整链路
- 数据报表：日报/周报生成与历史查看
- 本地语音压测（一键）：Win/mac 一键执行离线校验、语音生成、播放与日志复盘
- 全局功能测试：一条命令串行验证主链路能力并输出测试报告
- 系统自检：一键检查关键链路

## 8. 全局功能测试（上线前建议）

```bash
python scripts/global_feature_test.py --profile full
```

- `full`：包含浏览器 + Mock + 麦克风能力检查，未通过即视为不可上线。
- `full`：在 `VOICE_COMMAND_INPUT_MODE=system_loopback_asr/tab_audio_asr` 时，会额外执行回采真实链路压测。
- `offline`：仅离线能力检查（适合本地快速回归）。
- 报告目录：`data/reports/global_feature_test/`

## 9. 回采模式真实链路压测（推荐）

```bash
python scripts/loopback_asr_real_test.py --profile quick --mode system_loopback_asr --json
```

- 覆盖路径：音频播放 -> loopback 回采 -> ASR -> `_local_push_text()` -> 命令执行链。
- 报告目录：`data/reports/voice_stress/`

## 10. 数据与报告路径

- 弹幕事件：`data/analytics/danmu_events.jsonl`
- 日报：`data/reports/daily/`
- 周报：`data/reports/weekly/`
