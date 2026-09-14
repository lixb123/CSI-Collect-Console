# ESP32S3 SoftAP CSI 发射端

## 目的

让已烧录 `router_ping` 接收固件的 ESP32S3 在不刷新接收端的情况下，既可连接普通路由器采集，也可连接专用 ESP32S3 SoftAP 采集。

## 工作方式

专用 ESP32S3 在 `softap_csi` 角色下创建 WPA2 SoftAP、DHCP 和 IPv4 网络。接收端 GUI 使用原有“路由器”区域填写该 SoftAP 的 SSID 和密码；接收端关联成功后仍可使用原有 `router_ping` 流程，同时 SoftAP 发射端会按配置间隔向已连接站点发送 UDP 触发包（端口 `3233`），这些连续的数据帧是更高 CSI 速率的来源。

这不是 ESP-NOW 模式。若需要 ESP-NOW 广播实验，请在发射端的 `idf.py menuconfig` 中关闭 SoftAP 角色，并使用匹配的 ESP-NOW 接收端固件。

接收端支持运行时选择触发方式，无需重新刷写：GUI 的“触发方式”下拉框可选择
`router_ping` 或 `null_data`；串口等效命令为 `radar --send_mode router_ping` 或
`radar --send_mode null_data`。默认仍为 `router_ping`。

## 接收端 GUI

无需新增控件。普通路由器和 SoftAP 都通过同一组 SSID、密码及“连接”按钮切换：先点击“断开”，再填写下一台 AP 的信息并点击“连接”。

建议为 SoftAP 使用至少 8 个字符的 WPA2 密码；现有 GUI 仅在密码长度不少于 8 时发送密码参数。发射端默认 `RESEARCH_TX_SEND_INTERVAL_MS=10`，约为 100 次发送/秒；可在 `menuconfig` 中改为 `5`，但最终 CSI 速率仍受 Wi-Fi 信道、帧聚合和串口带宽限制。

`null_data` 会主动发送 802.11 Null Data 帧并依靠 AP 的 ACK 产生 CSI。它会增加信道占用，可能影响同一 AP 下的其他流量，而且得到的是 ACK 链路 CSI；需要高频触发时再切换。

## 构建发射端

在 `csi_send` 目录下运行：

```powershell
idf.py menuconfig
idf.py build flash monitor
```

默认配置已经打开 `Run as SoftAP CSI transmitter`。在 `Research CSI transmitter` 菜单中可设置 SSID、密码和信道；关闭该选项会恢复 ESP-NOW 研究发射角色。

## 验证

连接后，接收端串口应先出现 `Wi-Fi got IP`，随后出现 `Send ping data to router`。这里的 `router` 是旧日志用语，SoftAP 场景下它指专用发射端。`SYSTEM_TELEMETRY` 的 `send_mode=router_ping` 是预期结果；发射端串口还应出现 `SoftAP UDP trigger`。
