# ESP32-S3 研究发射端

`csi_send` 默认编译为接收端无需刷新固件即可连接的 SoftAP CSI 发射端，也保留与
`console-csi-v6` ESP-NOW 接收固件配套的研究发射角色。ESP-NOW 角色使用固定源 MAC
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

### SoftAP 角色

默认配置已打开 `Run as SoftAP CSI transmitter`。可在 `idf.py menuconfig` 的
`Research CSI transmitter` 菜单中
设置 `SoftAP SSID` 和至少 8 个字符的 `SoftAP WPA2 password`，然后重新编译并烧录发射端。
SoftAP 角色会提供 DHCP，默认网关为 `192.168.4.1`；接收端 GUI 仍使用“Wi-Fi 接入点”区域填写
SSID 和密码。发射端会按 `RESEARCH_TX_SEND_INTERVAL_MS` 向 SoftAP 子网广播 UDP 触发包（端口
`3233`），接收端可以从这些连续的 AP 数据帧获取 CSI。SoftAP 角色不会启动 ESP-NOW 发送任务，
也不要求重新刷写接收端固件。

默认 `10 ms` 间隔对应约 100 次发送/秒，但实际 CSI 速率仍受 Wi-Fi 调度、聚合、信道占用和
接收端串口带宽影响；若仍需要更高频率，可将间隔改为 `5 ms` 后重新编译发射端。

## 串口配置

### ESP-NOW 研究发射角色

若接收端选择 `espnow_research`，发射端必须使用独立配置构建：

```powershell
idf.py -B build-espnow -D IDF_TARGET=esp32s3 -D SDKCONFIG=sdkconfig.espnow -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.espnow.defaults" build
```

该配置使用固定源 MAC `1a:00:00:00:00:00`、信道 11 和带序号/CRC 的研究帧。发射端的
`RESEARCH_TX_EXPERIMENT_ID` 或运行时 `research_tx --experiment_id` 必须与 GUI session 的
`transmitter_experiment_id` 相同。SoftAP 与 ESP-NOW 是互斥的编译期角色；SoftAP 发射端
继续配合 Router Ping，不会被这个模式改动。

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
