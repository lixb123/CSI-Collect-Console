# CSI 采集数据说明 / CSI Capture Data Guide

本目录是采集程序的默认数据根目录。仓库只跟踪本说明文档，实际采集数据默认被 `.gitignore` 排除，避免误传受试者数据和体量较大的 CSI 文件。

This is the collector's default data root. Git tracks this guide only; generated recordings are ignored by default to prevent accidental publication of participant data and large CSI files.

## 1. 目录和文件名 / Layout and file names

```text
data/
└── <room_id>/
    └── <YYYY-MM-DD>/
        ├── <HH-MM-SS-ms>_<room_id>_<participant_id>_<action>_<target_seq>.csv
        ├── <same-name>.meta.json
        └── session_manifest.csv
```

示例：

```text
data/R01/2026-09-01/14-32-08-125_R01_P001_fall_forward_1.csv
```

- `room_id`：地点/房间的稳定标识，例如 `R01` 或 `LAB-A`。
- `YYYY-MM-DD`：主机开始采集时的本地日期；同一地点同一天的数据放在同一个目录。
- `HH-MM-SS-ms`：主机创建该 CSV 时的本地时间，精确到毫秒。
- `participant_id`：匿名人员标识。请使用 `P001` 之类的假名，不要写姓名、手机号或学号。
- `action`：稳定的英文机器标签；中英文界面只改变显示文本，不改变数据标签。
- `target_seq`：接收固件给出的动作/轮次序号，用于避免文件名冲突并核对顺序。

## 2. 每种文件的作用 / File roles

### 动作 CSV (`*.csv`)

每个文件对应一次动作采集窗口，一行对应一个 CSI 帧。它是信号处理、特征提取和模型训练的原始输入。

Each file is one action-recording window; each row is one CSI frame. This is the primary input for signal processing, feature extraction, and model training.

### 记录 sidecar (`*.meta.json`)

与 CSV 同名，保存该次采集的实验配置、人员/地点/session、动作语义、质量统计、链路遥测、固件版本和 CSV SHA-256。训练或发表结果时，使用 sidecar/manifest 追溯数据来源，不要只依赖文件名。

The sidecar stores experiment metadata, participant/room/session IDs, action semantics, quality summaries, link telemetry, firmware identity, and the CSV SHA-256. Use it and the manifest for provenance rather than inferring metadata only from file names.

### 日清单 (`session_manifest.csv`)

它是 `<地点>/<日期>` 目录中所有采集记录的追加式索引。一个目录可以包含同地点同日的多个 session 和多名匿名参与者。关键字段包括：

- `recording_id`, `relative_csv_path`, `sidecar_path`, `sha256`：记录身份、相对路径和完整性。
- `participant_id`, `session_id`, `date`, `room_id`：用于按人、会话、日期和地点划分数据。
- `action`, `binary_label`, `fall_type`, `trial_id`, `activity_order`：动作及跌倒语义。
- `configured_duration_ms`, `frame_count`, `sequence_gap_count`, `effective_sample_rate_hz`：采集长度和结构质量。
- `rssi_mean`, `rssi_std`, `noise_floor_mean`, `agc_gain_mean`：射频摘要。
- `send_mode`, `configured_send_interval_ms`, `observed_*`：发送配置与实际观察值。
- `receiver_*`, `device_*`, `host_*`：接收包、队列、内存、主机管线时延等 ISAC/ISCC 指标。
- `started_at_utc`, `ended_at_utc`, `status`：标准化的实验采集时间与完成状态。

`status=aborted` 表示中途停止或异常退出；不要直接混入训练集。`sequence_gap_count>0` 表示设备序号存在缺口，应检查丢包或串口/写盘压力。

## 3. 动作 CSV 字段怎么理解 / CSI row fields

主要字段如下（固件历史拼写 `taget`/`taget_seq` 为兼容旧数据而保留）：

| 字段 | 含义 |
| --- | --- |
| `type` | 行类型，正常为 `CSI_DATA`。 |
| `seq` | 接收端输出的连续 CSI 行序号。 |
| `timestamp` | PC 接收到并解析该行时写入的本地墙钟时间。 |
| `taget_seq` | 动作/采集轮次序号（历史拼写）。 |
| `taget` | 英文动作机器标签（历史拼写）。 |
| `mac` | 触发 CSI 的发送端 MAC 地址。 |
| `rssi` | 接收信号强度，单位通常为 dBm；数值越接近 0 通常越强。 |
| `rate`, `sig_mode`, `mcs`, `cwb`, `sgi` | PHY 速率、信号制式、MCS、带宽和保护间隔。 |
| `noise_floor` | 接收端估计噪声底，通常为 dBm。 |
| `channel_primary`, `channel_secondary` | 主信道和副信道设置。 |
| `local_timestamp` | ESP32 接收端的单调时间戳；适合帧间时序，不等同于 PC 墙钟。 |
| `agc_gain`, `fft_gain` | 接收端 AGC/FFT 增益。 |
| `len` | 解码后 CSI 原始字节数。 |
| `rx_state`, `first_word_invalid` | 接收状态和首字异常标志；非零值需要质量检查。 |
| `data` | CSI 的有符号原始字节数组。GUI 已从串口 Base64 解码后写入 CSV。 |

`data` 不是直接可训练的 `(time, subcarrier)` 浮点矩阵。ESP-IDF 顺序为 `[imag, real, ...]`，即复数应写为 `real + j·imag`；再计算幅度 `sqrt(real²+imag²)` 和相位 `atan2(imag,real)`。具体字节宽度和 LTF 选择应结合 `csi_output_type`、芯片及 `len` 核对。不要在未验证布局时假设所有设备都有相同子载波数量。普通路由器同样可以产生这些接收端 CSI，不要求 ESP32 发射端。

当前研究模式将布局明确记录为 `imag_real_interleaved`，即 `data[0::2]` 为虚部、`data[1::2]` 为实部。`complex_csi.py` 会生成去子载波线性趋势的相位诊断。原始绝对相位受 CFO/SFO、包检测时延、AGC 和 RF 链偏置影响，不能直接解释为距离；详见仓库根目录 `PHYSICS_CSI_CAPTURE_CN.md`。

## 4. 标签解释 / Label interpretation

### 跌倒类 (`binary_label=fall`)

- `wave`：旧版兼容的“模拟跌倒”标签。
- `fall_forward`, `fall_backward`, `fall_left`, `fall_right`：前、后、左、右侧跌倒。

### 推荐非跌倒类 (`binary_label=non_fall`)

- 环境/静态：`none`, `static`。
- 日常转移：`sit down`, `stand up`, `lie_down`, `bend`, `pick_up`, `turn`。
- 移动：`left`, `right`, `walk_slow`, `walk_fast`, `jump`, `climb up`。
- 兼容旧标签：`front`, `after`, `go`, `move`。

### 辅助/其他标签

`someone`, `applause`, `train` 主要用于占用、辅助动作或旧版校准，不建议直接当作跌倒二分类的核心动作集合。

## 5. 如何读取 / Reading examples

```python
from pathlib import Path
import ast
import pandas as pd

day_root = Path("data/R01/2026-09-01")
manifest = pd.read_csv(day_root / "session_manifest.csv")

# 先按完整 session/人员/地点划分，避免相邻帧泄漏。
usable = manifest[(manifest["status"] == "complete") &
                  (manifest["sequence_gap_count"] == 0)]

row = usable.iloc[0]
csi = pd.read_csv(day_root / row["relative_csv_path"])
csi_bytes = csi["data"].map(ast.literal_eval)
print(row["action"], row["binary_label"], len(csi), len(csi_bytes.iloc[0]))
```

训练/验证/测试应按完整 `participant_id`、`session_id`、`room_id` 或 `date` 分组，而不是随机拆分同一个 CSV 的相邻帧。对跨人或跨房间结论，应把完整人员或地点留作测试域。

## 6. 校验与下游处理 / Validation and downstream processing

研究模式会生成 manifest 和 sidecar。采集后可运行：

```powershell
cd tools
python validate_session.py data\R01\2026-09-01
```

同地点同日目录可能包含多个 session；此时日清单用于归档索引。需要严格 session 级验证或核心动作覆盖检查时，应先按 `session_id` 筛选清单到独立目录/清单，避免把不同人员或 session 误当成一个实验会话。

本项目负责采集和实验元数据；清洗、质量控制、模型训练、基准和论文处理结果见：

- [esp-csi-fall-detection](https://github.com/lixb123/esp-csi-fall-detection)

下游仓库的早期数据使用旧版 `wave/move/static/none/front/after` 标签。新方向跌倒和细粒度日常动作需要在下游代码中显式加入映射，不能静默合并。
