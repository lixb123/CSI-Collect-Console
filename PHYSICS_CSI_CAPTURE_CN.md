# 复数 I/Q 与物理引导网络采集说明

## 已经具备的能力

接收端原始 `data` 保存 ESP-IDF 的有符号 CSI 字节，顺序为 `[imag, real, imag, real, ...]`。本次增强把布局、LTF、滤波/缩放、校准角色和相位质量显式写入 manifest/sidecar，并在 GUI 提供“保存复数 I/Q”。校准角色由“采集项目”及其右侧的静态基线按钮自动映射，不再单独选择。

- 接收固件 `radar --physics_mode`：关闭 esp-radar 软件增益补偿、相邻子载波平滑和手动 bit shift，保留 signed I/Q，输出 base64；
- 普通路由器：AI 主实验的默认发射源；固定型号、信道、带宽、位置和 router-ping/null-data 间隔即可；
- 可选 ESP32 发射固件 `research_tx --physics_probe --probe_id N`：只用于需要包标记或通信遥测的 ISAC/ISCC 对照；
- GUI：`物理 CSI / Physics CSI` 开关与动作/空房/静止人员参考角色；
- sidecar/manifest：I/Q 顺序、复数子载波数、低幅值比例、去线性趋势相位统计和 `phase_quality_status`；
- `tools/complex_csi.py`：Im/Re 解析、相位展开、每帧线性相位去除和审计。

## AGC 与 DSP 约定

- 不把硬件 AGC 当作可关闭的采集选项。ESP32 CSI 回调给出的 `agc_gain`/`fft_gain` 是接收状态，应原样记录并用于质量诊断。
- `radar --physics_mode` 设置 `csi_compensate_en=0`、`channel_filter_en=0`、`manu_scale=0` 和 `shift=0`。采集端不做不可逆的增益补偿或平滑。
- 采集端不做滑动平均、异常值替换或相位“校正”。需要滤波时，在训练折内部离线应用，并保存窗口长度、截止频率和边界处理。
- 同时保留 raw amplitude 与逐帧 RMS 归一化 amplitude。归一化能减小 AGC/路径损耗的共同缩放，但会丢失绝对功率信息，不能替代原始质量字段。
- 每次采集后统计 `rate`、`sig_mode`、`mcs`、带宽、`channel_secondary`、`len`、`rssi`、`agc_gain`、包间隔和 `±127/128` 饱和率。先处理削顶和 PHY 切换，不用滤波掩盖它们。

## 相位限制

ESP32 单射频链的绝对相位受 CFO、SFO、包检测时延、AGC 和 RF 链偏置影响。`atan2(Q,I)` 的绝对值不能直接解释为传播距离。网络可使用同一帧去线性趋势残余相位、相邻包圆周相位差、动态多径/相位变化率/Doppler，以及同 session 参考差分。

## 每个 session 的顺序

1. 设备固定后选择“空房参考”，无人，采 30 s，标签 `none`。
2. 选择“静止人员参考”，参与者站在动作中心，采 30 s，标签 `static`。
3. 切换“动作采集”，完成正式 trial。
4. session 内不得更换 LTF、信道、带宽、位置、filter 或 scale。
5. 每个新房间/新摆位重新采两种参考。

## 发射源选择

Pattern Recognition 的 AI 主实验不需要 ESP32 发射端。复数 I/Q 是接收端对普通路由器 Wi-Fi 帧的信道估计；发射端不是 ESP32 不会让 I/Q 消失。普通路由器方案需固定路由器型号、信道、带宽、位置和流量间隔，并把 `send_mode` 设为 `router_ping`。

只有需要 packet/session marker、发送计数或链路层对照时才使用下面的 ESP32 发射端：

```text
research_tx --stop
research_tx --experiment_id P001-R01-D01-S01 --interval_ms 10 --payload_length 64 --physics_probe --probe_id 101 --reset_counters
research_tx --start
```

payload 至少 16 bytes。Probe 证明发射条件和包序列一致，但不会产生“已知真实信道相位”。

## 正式采集前 pilot

先用正式主实验的同一普通路由器做 `PILOT-PHASE-01`：1 个房间、1 人；空房/静止/慢走/快走各 5 条，四方向跌倒各 5 条；10 ms、LLTF、同一信道与摆位。检查所有记录 52 个复数子载波、invalid frame 为 0，再画去趋势相位和 temporal phase difference。隔日重复一次。

Pilot 通过后再开始主数据。如果相位跨日期完全不稳定，DQSNet 保留幅度物理前端，将相位分支作为失败消融报告，不把相位改善当成论文必要前提。
