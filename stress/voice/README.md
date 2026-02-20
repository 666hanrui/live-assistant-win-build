# 语音触发压测包（Windows）

这个压测包用于在没有真实助播账号时，验证“麦克风 -> 语音识别 -> 口令解析 -> 运营动作触发”链路。

## 文件说明

- `test_cases.json`: 中英文压测样本（正样本/负样本/干扰样本）
- `windows_tts_generate.ps1`: 按样本生成 `wav` 语音
- `windows_playback_loop.ps1`: 循环播放 `wav` 做真实麦克风压测
- `scripts/voice_stress_pack.py`: 离线校验与日志复盘脚本
- `scripts/loopback_asr_real_test.py`: 回采模式真实链路压测（音频播放 -> loopback 识别 -> 命令执行链）

## 一、离线逻辑校验（不依赖麦克风）

在项目根目录执行：

```bash
python scripts/voice_stress_pack.py offline --profile quick --json
python scripts/voice_stress_pack.py offline --profile all --json
```

输出目录：

- `data/reports/voice_stress/offline_*.md`
- `data/reports/voice_stress/offline_*.json`

## 二、真实麦克风压测（Windows）

1. 先准备音频路由（推荐）：
  - 安装 `VB-CABLE`
  - Chrome 麦克风设备选择虚拟输入（CABLE Output）

2. 生成测试音频：

```powershell
cd <项目根目录>\stress\voice
powershell -ExecutionPolicy Bypass -File .\windows_tts_generate.ps1 -Profile quick
```

3. 启动项目监听后，播放测试音频：

```powershell
cd <项目根目录>\stress\voice
powershell -ExecutionPolicy Bypass -File .\windows_playback_loop.ps1 -Rounds 2 -GapSeconds 2
```

4. 复盘最近日志：

```bash
python scripts/voice_stress_pack.py log-scan --minutes 30
```

输出目录：

- `data/reports/voice_stress/log_scan_*.md`

## 三、回采模式真实链路压测（system_loopback_asr/tab_audio_asr）

在项目根目录执行（建议先完成音频生成与虚拟声卡路由）：

```bash
python scripts/loopback_asr_real_test.py --profile quick --mode system_loopback_asr --json
```

或测试 tab 回采别名：

```bash
python scripts/loopback_asr_real_test.py --profile quick --mode tab_audio_asr --json
```

输出目录：

- `data/reports/voice_stress/loopback_real_*.md`
- `data/reports/voice_stress/loopback_real_*.json`

## 四、真实麦克风压测（macOS）

1. 先做离线校验（建议）：

```bash
python scripts/voice_stress_pack.py offline --profile quick --json
```

2. 生成 mac 测试音频（内置 `say`）：

```bash
bash stress/voice/macos_tts_generate.sh quick
```

3. 启动主监听后，播放压测音频：

```bash
bash stress/voice/macos_playback_loop.sh quick 2 2
```

4. 复盘日志：

```bash
python scripts/voice_stress_pack.py log-scan --minutes 30
```

说明：
- 若你还没装虚拟声卡（BlackHole/Loopback），可先用“外放+麦克风”做近真实测试。
- 装了虚拟声卡后，将系统播放音路由到 Chrome 选中的麦克风输入，测试更稳定。

## 五、判定标准（建议）

- 关键正样本命中率（critical）>= 95%
- 关键负样本误触发率 <= 2%
- 日志中 `收到口令候选[python_loopback]` / `收到口令候选[python_mic]` 和 `运营动作触发` 数量稳定
- `语音口令监听启动失败` 为 0 或显著下降

## 六、人工朗读脚本导出

```bash
python scripts/voice_stress_pack.py export-script --profile quick
```

生成：

- `stress/voice/manual_script_quick.txt`
