# CSI-Collect-Console

[English](README.md) · [研究采集设计](RESEARCH_CAPTURE.md) · [数据格式详解](tools/data/README.md)

`CSI-Collect-Console` 是面向 AI、ISAC 和 ISCC 实验的 ESP32-S3 Wi-Fi CSI 采集系统，包括：

- ESP-NOW 研究发射端 `csi_send`；
- 仓库根目录下的 CSI 接收与串口遥测固件；
- PyQt5 桌面采集工具 `tools/esp_csi_tool.py`；
- 中英文界面、参数记忆、等待/采集双倒计时和规范化实验元数据；
- 按地点和日期归档的 CSV、逐记录 sidecar 与日清单。

采集后的清洗、质量控制、模型训练、基准、图片和论文处理结果位于 [lixb123/esp-csi-fall-detection](https://github.com/lixb123/esp-csi-fall-detection)。本仓库负责采集，后者负责数据处理与实验结果。后者的早期发布使用 `wave/move/static/none/front/after` 旧标签，新采集的细粒度动作必须显式映射。

## 环境和设备

- 两块 ESP32-S3 开发板，分别作为 TX 与 RX；
- 两路 USB/UART，接收端 GUI 使用 2,000,000 波特率；
- ESP-IDF 5.5.5；依赖锁定的 `esp-radar` 至少要求 ESP-IDF 5.4；
- Python 3.10+，依赖见 `tools/requirements.txt`。

研究发射端与接收端默认使用源 MAC `1a:00:00:00:00:00` 和 Wi-Fi 信道 11，两端信道必须一致。

## 克隆项目

```powershell
git clone https://github.com/lixb123/CSI-Collect-Console.git
cd CSI-Collect-Console
```

编译前请打开 ESP-IDF 5.5.5 终端。

## 编译并烧录发射端

```powershell
cd csi_send
idf.py -B build-research-s3 set-target esp32s3
idf.py -B build-research-s3 build
idf.py -B build-research-s3 -p COM_TX flash monitor
```

把 `COM_TX` 替换为发射端 UART 端口。在 monitor 中为每个 session 配置实验 ID：

```text
research_tx --stop
research_tx --experiment_id P001-R01-D01-S01 --interval_ms 10 --payload_length 32 --reset_counters
research_tx --start
research_tx --status
```

实验 ID 必须与接收端实验 JSON 中的 `transmitter_experiment_id` 一致。协议字段及计数口径见 [csi_send/README.md](csi_send/README.md)。

## 编译并烧录接收端

回到仓库根目录：

```powershell
idf.py -B build-research-rx set-target esp32s3
idf.py -B build-research-rx build
idf.py -B build-research-rx -p COM_RX flash
```

把 `COM_RX` 替换为接收端 UART 端口。运行 GUI 前关闭占用同一串口的 `idf.py monitor`。

如果需要直接烧录已有构建产物，应使用构建目录中的 `flasher_args.json`，不要自行猜测偏移。接收端当前构建布局为：

```powershell
cd build-research-rx
python -m esptool --chip esp32s3 -p COM_RX -b 921600 write_flash `
  0x0 bootloader/bootloader.bin `
  0x8000 partition_table/partition-table.bin `
  0x1d000 ota_data_initial.bin `
  0x20000 console-csi-v6.bin
```

构建目录不会上传到 GitHub，因此推荐用户在本机重新编译并使用 `idf.py flash`。

## 启动界面

```powershell
cd tools
python -m pip install -r requirements.txt
python esp_csi_tool.py
```

不指定串口时进入离线界面模式，可先设置布局和采集参数。连接接收端时运行：

```powershell
python esp_csi_tool.py -p COM_RX
```

需要研究级元数据、manifest 和 sidecar 时：

```powershell
Copy-Item config\experiment_config.example.json config\experiment_config.json
# 编辑复制出的文件后：
python esp_csi_tool.py -p COM_RX `
  --csi_output_type LLTF `
  --experiment-config config\experiment_config.json `
  --data-root data
```

人员字段请使用 `P001` 等匿名编号，不要填写姓名、手机号或学号。界面会记忆语言、人员、地点、动作、等待时间、采集时长和次数。个人配置可能包含 Wi-Fi 密码，因此不会提交到 GitHub。

## 采集流程

1. 确认 TX/RX 信道和实验 ID 一致；
2. 填写人员标签与地点标签；
3. 选择动作；中英文切换只改变显示，不改变英文机器标签；
4. 设置每次等待、采集时长和次数；
5. 点击开始，每轮执行“等待倒计时 → 采集倒计时”，然后重复；
6. 采集后检查 manifest 并在训练前校验。

输出示例：

```text
tools/data/
└── R01/
    └── 2026-09-01/
        ├── 14-32-08-125_R01_P001_fall_forward_1.csv
        ├── 14-32-08-125_R01_P001_fall_forward_1.meta.json
        └── session_manifest.csv
```

CSV 内含 PC 时间、接收端单调时间、射频字段和解码后的 CSI 字节。每种文件用途、字段解释、标签体系和 Python 读取示例见 [tools/data/README.md](tools/data/README.md)。

## 校验及下游处理

```powershell
cd tools
python validate_session.py data\R01\2026-09-01
```

同一地点同一天的 manifest 可以索引多个 session。做严格 session 级检查或核心动作覆盖检查时，应先按 `session_id` 筛选。

清洗和模型基准请继续使用：

- [esp-csi-fall-detection](https://github.com/lixb123/esp-csi-fall-detection)

## 公开仓库范围

Git 会排除构建目录、managed components、运行日志、CSI 数据和个人配置。可复制 `tools/config/gui_config.example.json` 预先建立本地配置；如果不复制，GUI 首次运行会自动生成 `gui_config.json`。

## 许可证

项目特有代码和文档使用 MIT License。ESP-IDF、Espressif 示例代码及 managed components 仍遵循各自的上游许可证与声明。
