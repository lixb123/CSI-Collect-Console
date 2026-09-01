# ESP32-S3 研究发射端

`csi_send` 是与 `console-csi-v6` 接收固件配套的 ESP-NOW 研究发射端。它保留固定源 MAC
`1a:00:00:00:00:00`、默认信道 11，并在每个数据帧中携带可核验的实验元数据。

## 数据帧

每帧由 64 字节协议头和 0–186 字节应用 payload 组成，最大 250 字节。协议头包含：

- `experiment_id` 及其 32-bit FNV-1a hash；
- `packet_seq`；
- `tx_timestamp_us`（发射端启动后的单调时钟）；
- `payload_length`（不含 64 字节协议头）；
- protocol version、flags 和全帧 CRC-32。

接收端会独立校验 CRC、实验 hash 和 sequence，并在 `SYSTEM_TELEMETRY` 中累计有效包、
应用 payload 字节、CRC 错误及 sequence gap。

## 编译与烧录

在 ESP-IDF 5.5.5 终端中运行：

```powershell
cd CSI-Collect-Console\csi_send
idf.py -B build-research-s3 set-target esp32s3
idf.py -B build-research-s3 build
idf.py -B build-research-s3 -p COM端口 flash monitor
```

构建生成的应用固件位于：

```text
csi_send/build-research-s3/csi_send.bin
```

## 串口配置

固件默认自动发送。建议每个 session 开始前先停止、配置并清零：

```text
research_tx --stop
research_tx --experiment_id P001-R01-D01-S01 --interval_ms 10 --payload_length 32 --reset_counters
research_tx --start
research_tx --status
```

`experiment_id` 允许 1–31 个 ASCII 字符：字母、数字、`.`、`_`、`-`。运行中要先
`--stop` 才能修改配置，防止同一实验记录混入不同参数。

## 计数口径

- `attempted`：准备并尝试调用发送接口的帧数；
- `accepted` / `immediate_errors`：ESP-NOW API 接收或立即拒绝的次数；
- `callback_success` / `callback_fail` / `callback_timeouts`：本地 MAC callback 结果；
- `bytes_accepted`：被发送 API 接受的应用 payload 字节数；
- `orphan_callbacks`：无法归属到当前等待帧的迟到 callback。

ESP-NOW callback success 只是发射端本地 MAC 状态，不是接收端交付证明。接收 goodput 应按
接收端 `research_rx_bytes` 在记录窗口内的增量除以窗口时长计算。

`tx_timestamp_us` 没有和接收端时钟同步，不能单独用于声称端到端单向时延。若要测单向时延，
需要两端 PTP/GNSS/硬件触发等时钟同步并记录接收时间戳；否则只适合研究发送间隔和相对时序。
