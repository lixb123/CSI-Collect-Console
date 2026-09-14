# Wi-Fi CSI 跌倒检测补实验操作手册（给采集与测试协助人员）

版本：1.0（2026-09-01）

采集程序：[CSI-Collect-Console](https://github.com/lixb123/CSI-Collect-Console)

处理仓库：[esp-csi-fall-detection](https://github.com/lixb123/esp-csi-fall-detection)

## 1. 任务与证据边界

旧数据可以用于同语料模型比较，但缺少可靠的人员、session 信息且只有一个房间，不能回答“换人、换房、隔天还能否工作”。协助人员无需理解机器学习，只需按本手册安全地完成摆放、标签填写、动作执行、质量检查和整包交付。

新数据必须用 manifest 中的 `participant_id`、`session_id`、`date`、`room_id` 做完整分组。禁止事后从文件名猜人员，也禁止把同一 session 的相邻记录随机分到训练和测试两边。

## 2. 采集规模

### 最低可用修订

- 12 名参与者（`P001`–`P012`）；
- 3 个几何和家具明显不同的房间（`R01`–`R03`）；
- 每名参与者在每个房间采 1 个 session，且至少两个 session 位于不同日期；
- 每个 session 完成下文 14 个核心动作，每动作 3 次；
- 约 `12 × 3 × 14 × 3 = 1512` 条。

### 面向 Pattern Recognition 的推荐规模

- 15–20 名参与者、3 个房间、至少两个不同日期；
- 每名参与者在每个房间和日期各完成一个 session；
- 14 个核心动作，每动作每 session 5 次；
- 15 人时约 `15 × 3 × 2 × 14 × 5 = 6300` 条；
- 预留 1 名完整参与者和 1 个最终日期，在模型、阈值和表格冻结前不得查看其测试结果。

资源不足时优先保证人员、房间、日期真正分开，不要减少成一个房间，也不要把一次连续动作切成很多文件充数。

## 3. 人员分工

1. **负责人**：产生匿名编号、保管实名对应表、安排随机动作顺序。实名表不得进入数据包或代码仓库。
2. **设备操作员**：烧录、摆放、填写 GUI、开始采集、检查输出。
3. **观察记录员**：读口令、核对动作和 trial、记录偏差。
4. **安全保护员**：跌倒动作时站在垫子边保护参与者，不兼任电脑操作。

跌倒时推荐四人到场，至少保证操作员、参与者和独立安全保护员。

## 4. 安全与伦理底线

- 只招募自愿、知情同意且适合完成动作的成年人，并按所在单位要求办理伦理审批或豁免。
- 老年人、孕妇、有眩晕/心血管/骨骼肌肉疾病、近期受伤或当日不适者不做模拟跌倒。
- 使用厚实防滑体操垫，周围至少 1 m 无硬物；线缆不得进入动作区。
- 采用低重心、低冲击模拟，头部不得着地，不得跳起、用手腕硬撑或追求真实摔倒效果。
- 出现疼痛、头晕、气短、恐惧、垫子移位或设备进入动作区，立即停止。记录保留为 `aborted`。
- 四方向跌倒统一写 `demonstrated_fall`。只有正式获批且来源明确的真实事件才能写 `incident_fall`。
- 只使用 `P001` 之类假名。CSV、JSON、备注和文件夹不得出现姓名、学号、电话、邮箱或人脸照片。

## 5. 硬件清单

- Windows 笔记本、电源、Python 环境、ESP-IDF 5.5.5；
- ESP32-S3 接收端与 USB 数据线；
- 发射源：固定路由器，或烧录 `csi_send` 的 ESP32-S3；
- 路由器电源/网线、卷尺、地贴、设备支架；
- 防滑体操垫和安全保护用品；
- 现场记录表、加密备份盘；
- 若测能耗：INA219/INA226 或功率分析仪。

## 6. 路由器与 ESP32-S3 发射端如何选择

只做 AI 跨人/跨房间识别时，**默认使用普通路由器**，不要求 ESP32 发射端。固定型号、SSID、信道、带宽、位置和业务模式并记录 `router_id`；由接收端按固定间隔向路由器产生 ping/null-data 流量并采集返回/相关 Wi-Fi 帧。普通路由器通常不能提供应用包序号、发送计数和精确发送时间，因此主论文只报告接收侧 CSI 质量和 AI 指标，不声称发射侧链路时延或 goodput。

做 ISAC/ISCC 链路实验时使用 `csi_send` 研究发射端。它提供 experiment ID/hash、packet sequence、发射端单调时间戳、payload 长度、CRC 和本地回调计数；接收端提供包/字节/序号缺口、队列和堆内存遥测。

发送成功回调不等于对端收到。两块板的单调时钟未同步，也不能直接得出单向时延；单向时延需时钟同步或往返协议，能耗需外部功率计。同一 AI 对照组使用同一种发射方案；改变方案时必须新建 session。

## 7. 安装与烧录

```powershell
git clone https://github.com/lixb123/CSI-Collect-Console.git
cd CSI-Collect-Console
```

GUI 可在无 ESP32 时打开以练习参数，但离线模式不产生有效 CSI：

```powershell
cd tools
python esp_csi_tool.py
```

首次使用 GUI 时，按[ESP CSI 采集工具 GUI 使用说明](tools/GUI使用说明_CN.md)完成串口连接、Wi-Fi 接入、触发方式选择、曲线检查和 trial 保存。该说明还解释了 Raw CSI、雷达模型、统计区及曲线颜色；本手册只保留现场采集和实验一致性要求。

接收端烧录（把 COM5 换为实际端口）：

```powershell
idf.py set-target esp32s3
idf.py build
idf.py -p COM5 flash monitor
```

看到启动信息后退出 monitor，避免它与 GUI 同时占用串口。

研究发射端烧录：

```powershell
cd csi_send
idf.py set-target esp32s3
idf.py build
idf.py -p COM6 flash monitor
```

按 `csi_send/README.md` 配置。发射端 experiment ID 必须与接收配置 `transmitter_experiment_id` 完全一致，例如 `P001-R01-D01-S01`。

## 8. 编号规则

- 人员：`P001`、`P002`，永不转给另一个人；
- 房间：`R01`、`R02`，同一物理房间始终同一编号；
- 设备：`ESP32-RX-01`、`ESP32-TX-01`、`AP-01`；
- session：`P001-R01-D01-S01`；
- 日期：实际采集日 `YYYY-MM-DD`；
- trial ID 不得重复。

参与者、房间、日期、收发位置/朝向、信道、带宽、发射设备、发送间隔、干扰条件或固件大版本任一改变，都要新建 session。

## 9. 房间和设备摆放

在入口左侧地面角建立 `(0,0,0)`，x/y 沿两面墙，z 为高度，位置用米记录：

```json
"router_position": {"x_m": 0.0, "y_m": 1.0, "z_m": 1.2},
"receiver_position": {"x_m": 3.0, "y_m": 1.0, "z_m": 1.2}
```

- 记录房间长宽、家具材质、门窗、设备高度/距离、动作中心和朝向；
- 可保存不含人脸和个人物品的布局照，单独受控保管；
- session 内设备不得移动，碰撞后停止并新建 session；
- 除干扰实验外，旁观者不得在动作区移动；
- 记录空调、风扇、其他 AP、网络负载和门窗变化。

## 10. 标签整理与标准动作

英文机器标签不能翻译或自行改拼写。

| 机器标签 | 中文 | 二分类 | 标准动作 |
|---|---|---|---|
| `fall_forward` | 向前模拟跌倒 | fall | 低重心向前落垫，头不着地 |
| `fall_backward` | 向后模拟跌倒 | fall | 低重心后坐落垫，不直挺后仰 |
| `fall_left` | 向左侧模拟跌倒 | fall | 低冲击向左侧落垫 |
| `fall_right` | 向右侧模拟跌倒 | fall | 低冲击向右侧落垫 |
| `none` | 无人 | non_fall | 所有人离开动作区 |
| `static` | 有人静止 | non_fall | 站立、正常呼吸 |
| `walk_slow` | 慢走 | non_fall | 匀速穿过动作区 |
| `walk_fast` | 快走 | non_fall | 快走，禁止跑步 |
| `sit down` | 坐下 | non_fall | 自然坐到稳定椅子 |
| `stand up` | 站起 | non_fall | 从椅子自然站起 |
| `lie_down` | 主动躺下 | non_fall | 有控制地躺到垫上 |
| `bend` | 弯腰 | non_fall | 弯腰后恢复，不拿物品 |
| `pick_up` | 弯腰拾物 | non_fall | 捡轻物后站起 |
| `turn` | 转身 | non_fall | 原地转 180° 后停稳 |

`wave`、`move`、`front`、`after`、`go` 仅为旧数据兼容，语义不够细，新主实验不用。不得把旧 `wave` 文件批量改名为方向跌倒。`left/right/jump/climb up` 可做扩展动作；`someone/applause/train` 不进入二分类主结果。完整标签映射和下游审计规则见处理仓库 `esp-csi-fall-detection` 的 `paper/config/label_taxonomy.json`。

## 11. session 开始前

1. 核对排期中的人员、房间、日期和设备。
2. 完成垫子、防滑、硬物和参与者状态检查。
3. 复制配置模板并填写人员/session/日期/房间、设备、位置、朝向、距离、发送模式和备注。
4. 使用事先生成的随机动作顺序。为安全而分块完成跌倒时，记录该偏差。
5. 等待和采集时间由 GUI 设置。全项目固定同一主设置；默认 3 s 只是初始值，不能混用不同时长后当成同一条件。

## 12. 正式采集

### 12.1 每个 session 的复数 I/Q 校准顺序

1. 固定发射端、接收端和天线朝向，GUI 勾选“保存复数 I/Q”。
2. 选择“空房参考”，动作标签选 `none`，连续采 30 s；`calibration_id` 使用本 session 的唯一编号。
3. 参与者站在动作中心不动，选择“静止人员参考”，动作标签选 `static`，连续采 30 s。
4. 选择“动作采集”，再按动作清单采正式 trial。每条 trial 仍使用界面设定的等待时间和采集时长。
5. 同一 session 不得更换 Wi-Fi 信道、带宽、LTF、发射间隔、设备位置、滤波或缩放。任何一项变化都新建 session，并重采两条参考。
6. 每个采集日开始和结束各采一组 `none/static` 参考，用于检查当天相位漂移。

接收端必须使用 `radar --physics_mode`。普通路由器方案使用固定的 router-ping/null-data 间隔；只有可选的 ESP32 发射端对照才使用 `research_tx --physics_probe --probe_id N`。Probe 只确认包和实验条件，不提供已知真实信道相位。现场人员只需检查 manifest 中：52 个复数子载波、`complex_invalid_frames=0`、`phase_quality_status=usable_sanitized`、`absolute_phase_valid=false`。任一条件不满足时标记重采，不允许手工修改 CSV。

日常 GUI 使用中，普通路由器和 ESP32 SoftAP 都通过 Wi-Fi 接入点区域填写 SSID/密码后连接；切换两者不需要重刷接收端。`router_ping` 是默认兼容流程，`null_data` 是当前固件支持的运行时高频触发方式。二者不可在同一 session 混用；切换后必须新建 session 并重采两类参考数据。

### 12.2 动作 trial

```powershell
cd tools
python esp_csi_tool.py -p COM5 `
  --csi_output_type LLTF `
  --experiment-config config\experiment_config.json `
  --data-root data
```

每条严格照做：

1. 操作员读出屏幕人员、房间和动作，记录员核对。
2. 参与者回到起始标记后点击开始。
3. 等待倒计时只准备，不提前动作；屏幕提示“准备采集：动作名”。
4. 绿色框、红色文字显示“正在采集：动作名”及倒计时后再动作。
5. 一次窗口只完成一次动作，并在倒计时结束前停稳。
6. 提示关闭后由记录员判断通过、警告或重采。
7. 每个动作批次后确认 CSV、同名 `.meta.json` 和 `session_manifest.csv` 行均存在。

起步过早/过晚时，不修改旧记录标签，保留原因并新采一个 trial。

## 13. 通过、警告和重采标准

**通过：** `status=complete`；CSV/sidecar/manifest 一一对应；人员/session/房间/日期/动作正确；动作完整位于采集框内；设备未移动；hash 正确；帧数与时长大致一致。

**保留为警告：** 少量 sequence gap/帧率下降、远处有人经过、门突然打开、短时干扰、动作幅度存疑。不得删除，写入 `notes` 后交负责人判断。

**必须重采：** `aborted`；标签或身份填错；文件/sidecar 缺失；SHA-256 不一致；仅 0–1 帧；3 s 采集少于期望帧数 80%；动作越出窗口或一窗两次；设备被移动；实验 hash 不一致；研究包为零；发生安全问题。

错误文件保留，并写 `recollected_as=<新 recording_id>`。训练索引只使用审计通过的新记录。

## 14. session 后自动检查

先在采集仓库运行：

```powershell
python validate_session.py data\R01\2026-09-01
```

地点/日期目录可包含多个人和 session；严格覆盖检查必须按 `session_id` 分组，不能把多人的数量相加。

再在处理仓库运行：

```powershell
python paper\scripts\research_dataset_audit.py `
  C:\path\to\CSI-Collect-Console\tools\data `
  --output audit_2026-09-01 `
  --target-trials 5
```

查看：

- `audit_summary.json`：总体数和人员/房间/session/日期数；
- `audit_issues.csv`：逐条错误和警告；
- `coverage_by_session_action.csv`：每 session 每动作已有数量；
- `collection_gaps.csv`：还缺几条；
- `group_split_ledger.csv`：完整人员/房间/日期/session 留出表；
- `invalid_folds.csv`：不可用于评估的留出折。

返回码 1 表示有必须处理的错误。发表归档不得用 `--skip-hash`。

生成硬校验发布包：

```powershell
python paper\scripts\prepare_research_release.py `
  audit_2026-09-01\research_dataset_index.csv `
  --output release_new
```

它不做全局软筛选。PCA、LOF、KNN、标准化、概率校准、告警和 review 阈值必须在每个训练折内部拟合；不得用全数据先删“难样本”。

## 15. 每日交付内容

```text
handoff_2026-09-01_R01/
├── data/R01/2026-09-01/      # CSV、sidecar、manifest
├── audit_2026-09-01/         # 六个审计输出
├── room_layout_R01.md         # 坐标、家具、门窗、位置
├── schedule_completed.csv     # 计划与实际
├── deviation_log.md           # 重采、干扰、安全、设备异常
└── checksums.csv               # 整包 SHA-256
```

生成整包 hash：

```powershell
Get-ChildItem -File -Recurse -LiteralPath .\handoff_2026-09-01_R01 |
  Get-FileHash -Algorithm SHA256 |
  Select-Object Path, Hash |
  Export-Csv .\handoff_2026-09-01_R01\checksums.csv -NoTypeInformation -Encoding UTF8
```

原始数据至少两份备份，不上传公开 GitHub，按负责人指定方式加密传输。

## 16. 偏差记录模板

```text
时间：2026-09-01 14:35:20
session_id：P001-R01-D01-S01
recording_id：<从 manifest 复制>
计划动作：fall_left
发生情况：绿色框出现前开始动作
处置：保留原记录并重采
重采 recording_id：<新 ID>
设备是否移动：否
安全事件：无
记录员：OP02
```

## 17. 常见故障

- **GUI 离线：** 检查 `-p COMx`、USB 数据线和串口 monitor 是否占用；GUI 保持打开是正常的，但离线不采数据。
- **无 COM：** 查看设备管理器、换数据线/USB 口、检查驱动。
- **无 CSI：** 检查发射源、网络/信道、距离和发送间隔。诊断 `none` 不算正式数据。
- **hash 不一致：** 两端重新复制相同 experiment ID，旧条目标失败并重采。
- **空 CSV/帧少：** 查 queue drop、sequence gap、有效研究包和 free heap；修复后重采，禁止人工复制行。
- **序号缺口多：** 减少无关串口输出和电脑负载，每次只改变一个因素并新建 session。
- **日目录多人：** 允许；一切统计以 manifest 的 session 分组为准。

## 18. 负责人收到数据后的实验步骤

1. 冻结标签映射，旧标签和新标签分别报告。
2. 用文件 hash 和解析后信号 hash 去重，确保重复记录不跨训练/测试。
3. 运行 leave-one-session/date/person/room-out。
4. 每折只在训练侧拟合清洗统计、标准化、模型、校准和阈值；测试侧不按软质量删例。
5. 基线至少含 ExtraTrees、RBF-SVM、可复现 1-D CNN/TCN、时序 Transformer。
6. 比较 confidence-only、quality-only、启发式融合、learned joint，以及去 quality/去 ensemble disagreement 消融。
7. 预先固定 2.5%、5%、10%、20% review 预算，报告实际预算及 fall/non-fall review 率。
8. 主指标：fall recall、specificity、FN、FP、Brier、ECE、cost-weighted selective risk、error/cost capture、AURC；AUROC/AUPRC 为辅助。
9. 按人员/session 做 bootstrap CI，不按相邻窗口做独立 bootstrap。
10. 模型、阈值和报告格式冻结后只运行一次最终预留测试。

## 19. 论文主张的工程门槛

以下不是录用保证，而是决定是否继续首投 Pattern Recognition 的目标：

- 跨人 fall recall ≥90%，95% CI 下界 >80%；
- 跨房间 fall recall ≥85%；
- learned selector 在分组折上 AURC 显著优于 confidence-only；
- 10% review 预算使 cost-weighted automatic risk 相对下降 ≥40%，且不是主要把真实跌倒送审；
- 多数留出域实际 review 与目标相差 ≤2 个百分点，或触发明确的 shift fallback；
- 校准后的 Brier/ECE 在多数分组折优于原始概率；
- 所有跨人/跨房间折通过零身份重叠检查。

未达标时如实报告并定位原因。若 quality 特征不能稳定胜过 confidence-only，就把它降级为数据审计工具，不把“quality-aware method”写成核心算法贡献。

## 20. 每日结束检查

- [ ] 无实名或直接身份信息；
- [ ] session 元数据与排期一致；
- [ ] 跌倒均写 `demonstrated_fall`；
- [ ] 14 个核心动作达到本日计划；
- [ ] 顺序已随机化或偏差已记录；
- [ ] 异常记录保留且链接重采 ID；
- [ ] CSV、sidecar、manifest 一一对应；
- [ ] 完整 hash 审计无 error；
- [ ] `collection_gaps.csv` 已确认；
- [ ] 房间布局和坐标已记录；
- [ ] 两份备份完成且未上传公开仓库；
- [ ] 操作员、观察员、负责人完成交接。
