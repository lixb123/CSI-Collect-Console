#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#

# WARNING: we don't check for Python build-time dependencies until
# check_environment() function below. If possible, avoid importing
# any external libraries here - put in external script, or import in
# their specific function instead.

import sys
import csv
import json
import re
import os
import argparse
import pandas as pd
import numpy as np

import serial
from os import path
from os import mkdir
from io import StringIO

from PyQt5.Qt import *
from pyqtgraph import PlotWidget
from PyQt5 import QtCore
import pyqtgraph as pq

from PyQt5.QtCore import QDate, QDate, QTime, QDateTime

import threading
import base64
import time
from datetime import datetime
from multiprocessing import Process, Queue

from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox
from PyQt5.QtGui import QFontDatabase
# from PyQt5.QtChart import QChart, QLineSeries,QValueAxis
from esp_csi_tool_gui import Ui_MainWindow

from scipy import signal
import signal as signal_key
import socket
from pathlib import Path

from experiment_capture import ExperimentLedger, load_experiment_config, sha256_file, validate_action_label


CSI_SAMPLE_RATE = 100

# Remove invalid subcarriers
# secondary channel : below, HT, 40 MHz, non STBC, v, HT-LTF: 0~63, -64~-1, 384
CSI_VAID_SUBCARRIER_INTERVAL = 5
csi_vaid_subcarrier_index = []
csi_vaid_subcarrier_color = []
color_step = 255 // (28 // CSI_VAID_SUBCARRIER_INTERVAL + 1)

# LLTF: 52
# 26  red
csi_vaid_subcarrier_index += [i for i in range(
    0, 26, CSI_VAID_SUBCARRIER_INTERVAL)]
csi_vaid_subcarrier_color += [(i * color_step, 0, 0)
                              for i in range(1,  26 // CSI_VAID_SUBCARRIER_INTERVAL + 2)]
# 26  green
csi_vaid_subcarrier_index += [i for i in range(
    26, 52, CSI_VAID_SUBCARRIER_INTERVAL)]
csi_vaid_subcarrier_color += [(0, i * color_step, 0)
                              for i in range(1,  26 // CSI_VAID_SUBCARRIER_INTERVAL + 2)]

DEVICE_INFO_COLUMNS_NAMES = ['type', 'timestamp', 'compile_time', 'chip_name', 'chip_revision',
                             'app_revision', 'idf_revision', 'total_heap', 'free_heap', 'router_ssid', 'ip', 'port']
SYSTEM_TELEMETRY_COLUMNS_NAMES = ['type', 'device_uptime_ms', 'csi_received', 'queue_drops',
                                  'queue_depth', 'queue_high_water', 'free_heap', 'minimum_free_heap',
                                  'send_interval_ms', 'send_mode', 'research_rx_valid',
                                  'research_rx_crc_errors', 'research_rx_other_errors',
                                  'research_rx_seq_gaps', 'research_rx_bytes', 'research_rx_last_seq',
                                  'research_rx_experiment_hash']
g_device_info_series = None

CSI_DATA_INDEX = 500  # buffer size
CSI_DATA_COLUMNS = len(csi_vaid_subcarrier_index)
CSI_DATA_COLUMNS_NAMES = ['type', 'seq', 'timestamp', 'taget_seq', 'taget', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs',
                          'cwb', 'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding','sgi', 'noise_floor',
                          'ampdu_cnt', 'channel_primary', 'channel_secondary', 'local_timestamp', 'ant', 'sig_len',
                          'rx_state', 'agc_gain', 'fft_gain', 'len', 'first_word_invalid', 'data']
ACTION_LABELS = [
    ('unknown', '未选择', 'Not selected'),
    ('train', '环境基线（兼容）', 'Environment baseline (legacy)'),
    ('none', '无人环境', 'Empty room'),
    ('someone', '有人存在（辅助）', 'Occupied (auxiliary)'),
    ('static', '站立静止', 'Standing still'),
    ('move', '一般移动（辅助）', 'General movement (auxiliary)'),
    ('front', '向前行走（兼容 front）', 'Walk forward (legacy front)'),
    ('after', '向后行走（兼容 after）', 'Walk backward (legacy after)'),
    ('left', '向左移动', 'Move left'),
    ('right', '向右移动', 'Move right'),
    ('go', '正常行走（兼容 go）', 'Normal walking (legacy go)'),
    ('jump', '跳跃', 'Jumping'),
    ('sit down', '坐下', 'Sitting down'),
    ('stand up', '站起', 'Standing up'),
    ('climb up', '上楼／攀爬', 'Climbing up'),
    ('wave', '模拟跌倒（兼容 wave）', 'Demonstrated fall (legacy wave)'),
    ('applause', '鼓掌（辅助）', 'Clapping (auxiliary)'),
    ('fall_forward', '向前跌倒', 'Forward fall'),
    ('fall_backward', '向后跌倒', 'Backward fall'),
    ('fall_left', '向左侧跌倒', 'Left-side fall'),
    ('fall_right', '向右侧跌倒', 'Right-side fall'),
    ('bend', '弯腰', 'Bending'),
    ('pick_up', '拾取物品', 'Picking up an object'),
    ('lie_down', '主动躺下', 'Lying down intentionally'),
    ('turn', '转身', 'Turning'),
    ('walk_slow', '慢走', 'Slow walking'),
    ('walk_fast', '快走', 'Fast walking'),
]
CSI_DATA_TARGETS = [item[0] for item in ACTION_LABELS]
CSI_DATA_TARGET_LABELS_ZH = [item[1] for item in ACTION_LABELS]
CSI_DATA_TARGET_LABELS_EN = [item[2] for item in ACTION_LABELS]
STATISTICS_MODES = [('minute', '分钟'), ('hour', '小时'), ('day', '天')]
COMMAND_OPTIONS = [
    ('', '无'),
    ('restart', '重启设备'),
    ('radar --output LLFT', '设置雷达输出 LLFT'),
    ('version', '查看版本'),
    ('wifi_config --info', '查看 Wi-Fi 信息'),
    ('wifi_scan', '扫描 Wi-Fi'),
    ('ping --abort', '停止 Ping'),
]
COMMAND_LABELS_EN = [
    'None', 'Restart device', 'Set radar output to LLFT', 'Show version',
    'Show Wi-Fi information', 'Scan Wi-Fi', 'Stop ping'
]
DEVICE_INFO_LABELS_ZH = {
    'type': '类型', 'timestamp': '时间戳', 'compile_time': '编译时间',
    'chip_name': '芯片型号', 'chip_revision': '芯片版本', 'app_revision': '固件版本',
    'idf_revision': 'ESP-IDF 版本', 'total_heap': '总堆内存', 'free_heap': '可用堆内存',
    'router_ssid': '路由器 SSID', 'ip': 'IP 地址', 'port': '端口',
}
RADIO_HEADER_LABELS_ZH = {
    'seq': '序号', 'timestamp': '主机时间', 'taget_seq': '动作序号', 'taget': '动作',
    'mac': 'MAC 地址', 'rssi': 'RSSI', 'rate': '速率', 'sig_mode': '信号模式',
    'mcs': 'MCS', 'cwb': '信道带宽', 'smoothing': '平滑', 'not_sounding': '非探测帧',
    'aggregation': '聚合方式', 'stbc': 'STBC', 'fec_coding': 'FEC 编码', 'sgi': '保护间隔',
    'noise_floor': '噪声底', 'ampdu_cnt': 'AMPDU 计数', 'channel_primary': '主信道',
    'channel_secondary': '次信道', 'local_timestamp': '设备时间戳', 'ant': '天线',
    'sig_len': '信号长度', 'rx_state': '接收状态', 'agc_gain': 'AGC 增益',
    'fft_gain': 'FFT 增益', 'len': 'CSI 长度', 'first_word_invalid': '首字无效',
}
RADAR_DATA_COLUMNS_NAMES = ['type', 'seq', 'timestamp',
                            'waveform_wander', 'wander_average', 'waveform_wander_threshold', 'someone_status',
                            'waveform_jitter', 'jitter_midean', 'waveform_jitter_threshold', 'move_status']

g_csi_amplitude_array = np.zeros(
    [CSI_DATA_INDEX, CSI_DATA_COLUMNS], dtype=np.int32)
g_rssi_array = np.zeros(CSI_DATA_INDEX, dtype=np.int8)
# 使用 object 类型以支持混合数据类型（字符串、数字、时间戳等）
g_radio_header_pd = pd.DataFrame(np.empty([10, len(
    CSI_DATA_COLUMNS_NAMES[1:-1])], dtype=object), columns=CSI_DATA_COLUMNS_NAMES[1:-1])

RADAR_STATUS_RECORD = ['room', 'human', 'spend_time', 'start_time', 'stop_time']
g_status_record_pd = pd.DataFrame(np.zeros(
    [20, len(RADAR_STATUS_RECORD)], dtype=np.str_), columns=RADAR_STATUS_RECORD)

RADAR_MOVE_RECORD = ['date', 'hour', 'minute', 'second', 'count']
g_move_record_pd = pd.DataFrame(columns=RADAR_MOVE_RECORD)

RADAR_DATA = ['status', 'threshold', 'value', 'max', 'min', 'mean', 'std']
g_radar_data_room_pd = pd.DataFrame(
    np.zeros([10, len(RADAR_DATA)], dtype=np.float32), columns=RADAR_DATA)
g_radar_data_human_pd = pd.DataFrame(
    np.zeros([10, len(RADAR_DATA)], dtype=np.float32), columns=RADAR_DATA)

g_current_time = QDateTime.currentDateTime()

RADAR_DATA_INDEX = 100  # buffer size

ROOM_STATUS_NAMES = ['none', 'someone']
HUMAN_STATUS_NAMES = ['static', 'move']
RADAR_WAVEFORM_NAMES = ['wander', 'jitter']
RADAR_targetS_NAMES = ['someone', 'move']
RADAR_targetS_LEN = len(RADAR_targetS_NAMES)
g_radar_eigenvalue_color = [(0, 0, 255), (0, 255, 0)]
g_radar_eigenvalue_threshold_color = [(255, 255, 0), (255, 0, 255)]
g_radar_eigenvalue_array = np.zeros(
    [RADAR_DATA_INDEX, RADAR_targetS_LEN], dtype=np.float32)
g_radar_eigenvalue_threshold_array = np.zeros(
    [RADAR_DATA_INDEX, RADAR_targetS_LEN], dtype=np.float32)
g_radar_status_array = np.zeros(
    [RADAR_DATA_INDEX, RADAR_targetS_LEN], dtype=np.int32)
g_evaluate_statistics_array = np.zeros(
    [RADAR_targetS_LEN, RADAR_targetS_LEN], dtype=np.int32)

g_display_raw_data = False
g_display_radar_model = False
g_display_eigenvalues_table = False

def base64_decode_bin(str_data):
    try:
        # 清理数据：移除换行符、回车符、空格等无效字符
        str_data = str(str_data).strip().replace('\n', '').replace('\r', '').replace(' ', '')

        # 如果数据包含日志信息（如 "->valid_len: 104"），只取 base64 部分
        # 使用更智能的方式检测日志信息：查找常见的日志模式
        # 例如：">valid_len", ": 104", "(120933)" 等
        log_patterns = [
            r'->valid_len',
            r':\s*\d+',  # 冒号后跟数字（如 ": 104"）
            r'\(\d+\)',  # 括号中的数字（如 "(120933)"）
            r'[DIWE]\s*\(',  # 日志级别（如 "I ("）
        ]

        # 查找日志模式的起始位置，只保留 base64 部分
        min_log_start = len(str_data)
        for pattern in log_patterns:
            import re
            match = re.search(pattern, str_data)
            if match:
                min_log_start = min(min_log_start, match.start())

        # 如果找到日志信息，只保留 base64 部分（至少保留一定长度，避免误判）
        if min_log_start < len(str_data) and min_log_start > 10:
            str_data = str_data[:min_log_start]

        # 如果数据长度不是4的倍数，尝试添加填充
        missing_padding = len(str_data) % 4
        if missing_padding:
            str_data += '=' * (4 - missing_padding)

        bin_data = base64.b64decode(str_data, validate=True)
    except Exception as e:
        print(f'Exception: {e}, data: {str_data[:100]}')
        # 解码失败时返回空列表，避免后续错误
        return []

    list_data = list(bin_data)

    for i in range(len(list_data)):
        if list_data[i] > 127:
            list_data[i] = list_data[i] - 256

    return list_data


def base64_encode_bin(list_data):
    for i in range(len(list_data)):
        if list_data[i] < 0:
            list_data[i] = 256 + list_data[i]
    # print(list_data)

    str_data = 'test'
    try:
        str_data = base64.b64encode(bytes(list_data)).decode('utf-8')
    except Exception as e:
        print(f'Exception: {e}, data: {list_data}')
    return str_data


def get_label(folder_path):
    parts = str.split(folder_path, os.path.sep)
    return parts[-1]


def evaluate_data_send(serial_queue_write, folder_path):
    label = get_label(folder_path)
    if label == 'train':
        command = f'radar --train_start'
        serial_queue_write.put(command)

    tcpCliSock = socket.socket()
    device_info_series = pd.read_csv('log/device_info.csv').iloc[-1]

    print(f"connect:{device_info_series['ip']},{device_info_series['port']}")
    tcpCliSock.connect((device_info_series['ip'], device_info_series['port']))

    file_name_list = sorted(os.listdir(folder_path))
    print(file_name_list)
    for file_name in file_name_list:
        file_path = folder_path + os.path.sep + file_name
        data_pd = pd.read_csv(file_path)
        for index, data_series in enumerate(data_pd.iloc):
            csi_raw_data = json.loads(data_series['data'])
            data_pd.loc[index, 'data'] = base64_encode_bin(csi_raw_data)
            temp_list = base64_decode_bin(data_pd.loc[index, 'data'])
            # print(f"temp_list: {temp_list}")
            data_str = ','.join(str(value)
                                for value in data_pd.loc[index]) + '\n'
            data_str = data_str.encode('utf-8')
            # print(f"data_str: {data_str}")
            tcpCliSock.send(data_str)

    tcpCliSock.close()
    time.sleep(1)

    if label == 'train':
        command = 'radar --train_stop'
        serial_queue_write.put(command)

    sys.exit(0)


class DataGraphicalWindow(QMainWindow, Ui_MainWindow):
    def __init__(self, serial_queue_write, csi_output_type='LLTF', experiment_config=None, data_root='data', parent=None):
        super(DataGraphicalWindow, self).__init__(parent)
        self.setupUi(self)
        self.gui_config_path = Path('./config/gui_config.json')
        try:
            self.gui_config = json.loads(self.gui_config_path.read_text(encoding='utf-8'))
        except Exception:
            example_path = Path('./config/gui_config.example.json')
            try:
                self.gui_config = json.loads(example_path.read_text(encoding='utf-8'))
            except Exception:
                self.gui_config = {}
            self.gui_config_path.parent.mkdir(parents=True, exist_ok=True)
            self.gui_config_path.write_text(
                json.dumps(self.gui_config, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        self.language = self.gui_config.get('language', 'zh')
        for index, (command, label) in enumerate(COMMAND_OPTIONS):
            self.comboBox_command.setItemText(index, label)
            self.comboBox_command.setItemData(index, command)
        for index, (mode, label) in enumerate(STATISTICS_MODES):
            self.comboBox_statistics_mode.setItemText(index, label)
            self.comboBox_statistics_mode.setItemData(index, mode)
        self.comboBox_collect_target.clear()
        for action, label in zip(CSI_DATA_TARGETS, CSI_DATA_TARGET_LABELS_ZH):
            self.comboBox_collect_target.addItem(label, action)
        self.csi_output_type = csi_output_type
        self.experiment_config = experiment_config
        self.data_root = data_root
        participant_id = ''
        room_id = ''
        if self.experiment_config:
            participant_id = str(self.experiment_config.get('participant_id', ''))
            room_id = str(self.experiment_config.get('room_id', ''))
            self.setWindowTitle(
                f"ESP CSI 研究采集 - {self.experiment_config['session_id']} - "
                f"{participant_id} - {room_id}"
            )
            self.pushButton_collect_clean.setEnabled(False)
            self.pushButton_collect_clean.setToolTip(
                "研究采集模式下禁用清空功能，以保护会话清单和已完成记录。"
            )
        self.lineEdit_participant_id.setText(participant_id)
        self.lineEdit_room_id.setText(room_id)
        self.lineEdit_participant_id.setAccessibleName('人员标签 participant_id')
        self.lineEdit_room_id.setAccessibleName('地点标签 room_id')
        id_validator = QRegExpValidator(QRegExp(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'), self)
        self.lineEdit_participant_id.setValidator(id_validator)
        self.lineEdit_room_id.setValidator(
            QRegExpValidator(QRegExp(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'), self)
        )
        self.lineEdit_participant_id.setStyleSheet(
            'QLineEdit { background-color: #eaf3ff; border: 1px solid #4f8edc; '
            'border-radius: 4px; padding: 4px 8px; font-weight: 600; }'
        )
        self.lineEdit_room_id.setStyleSheet(
            'QLineEdit { background-color: #eef9ed; border: 1px solid #5a9f59; '
            'border-radius: 4px; padding: 4px 8px; font-weight: 600; }'
        )
        self.label_complex_csi = QLabel(self.groupBox_18)
        self.checkBox_complex_csi = QCheckBox(self.groupBox_18)
        self.checkBox_complex_csi.setChecked(bool(
            (self.experiment_config or {}).get('complex_csi_enabled',
                self.gui_config.get('complex_csi_enabled', True))))
        self.comboBox_calibration_role = QComboBox(self.groupBox_18)
        self.comboBox_calibration_role.addItem('Activity', 'activity')
        self.comboBox_calibration_role.addItem('Empty-room reference', 'empty_room_reference')
        self.comboBox_calibration_role.addItem('Static-person reference', 'static_reference')
        saved_role = (self.experiment_config or {}).get(
            'calibration_role', self.gui_config.get('calibration_role', 'activity'))
        role_index = self.comboBox_calibration_role.findData(saved_role)
        if role_index >= 0:
            self.comboBox_calibration_role.setCurrentIndex(role_index)
        # Keep identity/location on their own row.  The physics controls have
        # longer bilingual labels and were too compressed at 1366x768 when
        # appended to the metadata row.
        self.horizontalLayout_physics_csi = QHBoxLayout()
        self.horizontalLayout_physics_csi.setSpacing(10)
        self.horizontalLayout_physics_csi.addWidget(self.label_complex_csi)
        self.horizontalLayout_physics_csi.addWidget(self.checkBox_complex_csi)
        self.horizontalLayout_physics_csi.addWidget(self.comboBox_calibration_role, 1)
        self.horizontalLayout_physics_csi.addStretch(2)
        self.verticalLayout_3.insertLayout(1, self.horizontalLayout_physics_csi)
        self.groupBox_18.setMinimumHeight(270)
        self.collect_overlay = QFrame(self.centralwidget)
        self.collect_overlay.setObjectName('collect_overlay')
        self.collect_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay_layout = QVBoxLayout(self.collect_overlay)
        overlay_layout.setContentsMargins(28, 24, 28, 24)
        self.collect_overlay_label = QLabel(self.collect_overlay)
        self.collect_overlay_label.setAlignment(Qt.AlignCenter)
        self.collect_overlay_label.setWordWrap(True)
        overlay_layout.addWidget(self.collect_overlay_label)
        self.collect_overlay.hide()
        self.collect_overlay_state = None
        global g_display_raw_data
        global g_display_radar_model
        global display_eigenvalues_table

        gui_config = self.gui_config
        if gui_config:
            # print(f"gui_config: {gui_config}")
            if len(gui_config.get('router_ssid', '')) > 0:
                self.lineEdit_router_ssid.setText(gui_config['router_ssid'])
            if len(gui_config.get('router_password', '')) >= 8:
                self.lineEdit_router_password.setText(
                    gui_config['router_password'])
            if not self.experiment_config:
                self.lineEdit_participant_id.setText(gui_config.get('participant_id', ''))
                self.lineEdit_room_id.setText(gui_config.get('room_id', ''))

            g_display_raw_data = gui_config.get('display_raw_data', True)
            if g_display_raw_data:
                self.checkBox_raw_data.setChecked(True)
            else:
                self.checkBox_raw_data.setChecked(False)

            g_display_radar_model = gui_config.get('display_radar_model', True)
            if g_display_radar_model:
                self.checkBox_radar_model.setChecked(True)
            else:
                self.checkBox_radar_model.setChecked(False)

            g_display_eigenvalues_table = gui_config.get('display_eigenvalues_table', False)
            if g_display_eigenvalues_table:
                self.checkBox_display_eigenvalues_table.setChecked(True)
            else:
                self.checkBox_display_eigenvalues_table.setChecked(False)

            if gui_config.get('router_auto_connect', False):
                self.checkBox_router_auto_connect.setChecked(True)
            else:
                self.checkBox_router_auto_connect.setChecked(False)

        saved_action = gui_config.get('collect_action', 'none')
        saved_action_index = self.comboBox_collect_target.findData(saved_action)
        if saved_action_index >= 0:
            self.comboBox_collect_target.setCurrentIndex(saved_action_index)
        self.timeEdit_collect_delay.setValue(max(0, int(gui_config.get('collect_wait_seconds', 3))))
        self.spinBox_collect_duration.setValue(max(1, int(gui_config.get('collect_duration_seconds', 3))))
        self.spinBox_collect_number.setValue(max(1, int(gui_config.get('collect_number', 1))))
        language_index = 1 if self.language == 'en' else 0
        self.comboBox_language.setCurrentIndex(language_index)
        self.comboBox_language.currentIndexChanged.connect(self.change_language)

        self.curve_subcarrier_range = np.array([10, 20])
        self.graphicsView_subcarrier.setYRange(
            self.curve_subcarrier_range[0], self.curve_subcarrier_range[1])
        self.graphicsView_subcarrier.addLegend()

        self.curve_subcarrier = []
        self.serial_queue_write = serial_queue_write

        for i in range(CSI_DATA_COLUMNS):
            curve = self.graphicsView_subcarrier.plot(
                g_csi_amplitude_array[:, i], name=str(i), pen=csi_vaid_subcarrier_color[i])
            self.curve_subcarrier.append(curve)

        self.curve_rssi = self.graphicsView_rssi.plot(
            g_rssi_array, name='rssi', pen=(255, 255, 255))

        self.wave_filtering_flag = self.checkBox_wave_filtering.isCheckable()
        self.checkBox_wave_filtering.released.connect(
            self.show_curve_subcarrier_filter)
        # self.graphicsView_rssi.setYRange(-10, -65)
        self.checkBox_router_auto_connect.released.connect(
            self.show_router_auto_connect)

        self.curve_radar_eigenvalue = []
        self.curve_radar_eigenvalue_threshold = []
        # self.graphicsView_eigenvalues.setYRange(0.95, 1.0)
        self.graphicsView_eigenvalues.addLegend()
        self.graphicsView_eigenvalues.showGrid(x=True, y=True)

        self.dateTimeEdit_statistics_time.setDateTime(g_current_time)
        self.statistic_config = pd.Series(index=['time', 'mode', 'auto_update'],
                                          data=[
            self.dateTimeEdit_statistics_time.dateTime().toPyDateTime(),
                                            self.comboBox_statistics_mode.currentData(),
            self.checkBox_statistics_auto_update.isChecked()
        ])

        for i in range(RADAR_targetS_LEN):
            curve_eigenvalue = self.graphicsView_eigenvalues.plot(
                g_radar_eigenvalue_array[:, i], name=RADAR_WAVEFORM_NAMES[i], pen=g_radar_eigenvalue_color[i])
            curve_eigenvalue_threshold = self.graphicsView_eigenvalues.plot(
                g_radar_eigenvalue_threshold_array[:, i],
                name=RADAR_WAVEFORM_NAMES[i] + '_' + RADAR_targetS_NAMES[i] + '_threshold',
                pen=g_radar_eigenvalue_threshold_color[i])
            self.curve_radar_eigenvalue.append(curve_eigenvalue)
            self.curve_radar_eigenvalue_threshold.append(
                curve_eigenvalue_threshold)

        self.model_radio_header = QStandardItemModel(
            1, len(g_radio_header_pd.columns.values))
        self.model_radio_header.setHorizontalHeaderLabels(
            [RADIO_HEADER_LABELS_ZH.get(name, name) for name in g_radio_header_pd.columns.values])
        self.tableView_radioHeader.setModel(self.model_radio_header)
        self.tableView_radioHeader.horizontalHeader(
        ).setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tableView_values = {
            'sig_mode': ['非 HT（11bg）', 'HT（11n）', 'VHT（11ac）'],
            'mcs': [f'MCS{i}' for i in range(76)],
            'cwb': ['20MHz', '40MHz'],
            'aggregation': ['MPDU', 'AMPDU'],
            'channel_secondary': ['无', '上方', '下方'],
            'stbc': ['非 STBC', 'STBC'],
            'sgi': ['长 GI', '短 GI'],
        }

        self.model_device_info = QStandardItemModel(
            len(DEVICE_INFO_COLUMNS_NAMES), 2)
        self.tableView_device_info.setModel(self.model_device_info)
        self.tableView_device_info.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        self.tableView_device_info.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self.model_status_record = QStandardItemModel(
            1, len(g_status_record_pd.columns.values))
        self.model_status_record.setHorizontalHeaderLabels(
            ['房间状态', '人员状态', '持续时间', '开始时间', '结束时间'])
        self.tableView_status_record.setModel(self.model_status_record)
        self.tableView_status_record.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        self.tableView_status_record.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self.model_radar_data_room = QStandardItemModel(len(
            g_radar_data_room_pd.columns.values), len(g_radar_data_room_pd.columns.values))
        self.model_radar_data_room.setHorizontalHeaderLabels(
            ['状态', '阈值', '数值', '最大值', '最小值', '平均值', '标准差'])
        self.tableView_radar_data_room.setModel(self.model_radar_data_room)
        self.tableView_radar_data_room.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self.model_radar_data_human = QStandardItemModel(len(
            g_radar_data_human_pd.columns.values), len(g_radar_data_human_pd.columns.values))
        self.model_radar_data_human.setHorizontalHeaderLabels(
            ['状态', '阈值', '数值', '最大值', '最小值', '平均值', '标准差'])
        self.tableView_radar_data_human.setModel(self.model_radar_data_human)
        self.tableView_radar_data_human.horizontalHeader(
        ).setSectionResizeMode(QHeaderView.ResizeToContents)

        evaluate_STATISTICS_COLUMNS_NAMES = ['类型', '数量', '占比']
        self.model_evaluate_statistics = QStandardItemModel(
            len(evaluate_STATISTICS_COLUMNS_NAMES), 3)
        self.model_evaluate_statistics.setHorizontalHeaderLabels(
            evaluate_STATISTICS_COLUMNS_NAMES)
        self.model_evaluate_statistics.setItem(
            0, 0, QStandardItem('无人 / 静止'))
        self.model_evaluate_statistics.setItem(1, 0, QStandardItem('无人 / 移动'))
        self.model_evaluate_statistics.setItem(
            2, 0, QStandardItem('有人 / 静止'))
        self.model_evaluate_statistics.setItem(
            3, 0, QStandardItem('有人 / 移动'))

        self.pushButton_predict_config.released.connect(self.command_predict_config)
        self.pushButton_statistics_config.released.connect(
            self.show_statistics_status_record_move)
        self.comboBox_statistics_mode.activated.connect(
            self.show_statistics_status_record_move)
        # self.pushButton_evaluate_config.released.connect(self.command_evaluate_config)

        self.pushButton_router_connect.released.connect(
            self.command_router_connect)
        self.pushButton_command.released.connect(self.command_custom)

        self.comboBox_command.activated.connect(self.comboBox_command_show)

        self.pushButton_collect_start.released.connect(
            self.pushButton_collect_show)
        self.pushButton_train_start.released.connect(
            self.pushButton_train_show)

        self.pushButton_collect_clean.released.connect(
            self.pushButton_collect_clean_show)

        self.checkBox_raw_data.released.connect(self.checkBox_raw_data_show)
        self.checkBox_radar_model.released.connect(self.checkBox_radar_model_show)
        self.checkBox_display_eigenvalues_table.released.connect(
            self.checkBox_display_eigenvalues_table_show)

        self.splitter_eigenvalues.setStretchFactor(0, 8)
        self.splitter_eigenvalues.setStretchFactor(1, 1)

        self.splitter_status_record.setStretchFactor(0, 10)
        self.splitter_status_record.setStretchFactor(1, 1)

        self.splitter_raw_data.setStretchFactor(0, 1)
        self.splitter_raw_data.setStretchFactor(1, 8)
        self.splitter_raw_data.setStretchFactor(2, 2)

        # Give the collection controls enough vertical space for the separate
        # participant and location metadata row at common laptop resolutions.
        self.splitter_3.setStretchFactor(0, 5)
        self.splitter_3.setStretchFactor(1, 3)
        self.splitter_3.setStretchFactor(2, 3)
        self.splitter_3.setSizes([350, 220, 230])

        self.textBrowser_log.setStyleSheet('background:black')

        self.timer_boot_command = QTimer()
        self.timer_boot_command.timeout.connect(self.command_boot)
        self.timer_boot_command.setInterval(3000)
        self.timer_boot_command.start()

        self.timer_curve_subcarrier = QTimer()
        self.timer_curve_subcarrier.timeout.connect(self.show_curve_subcarrier)
        self.timer_curve_subcarrier.setInterval(300)

        self.timer_curve_radar = QTimer()
        self.timer_curve_radar.timeout.connect(self.show_curve_eigenvalue)
        self.timer_curve_radar.setInterval(100)

        self.timer_eigenvalues_table = QTimer()
        self.timer_eigenvalues_table.timeout.connect(
            self.show_eigenvalue_table)
        self.timer_eigenvalues_table.setInterval(100)

        self.collect_saved_number = self.spinBox_collect_number.value()
        self.timer_collect_duration = QTimer()
        self.timer_collect_duration.setInterval(1000)
        self.timer_collect_duration.timeout.connect(self.collect_duration_tick)

        self.collect_saved_delay = self.timeEdit_collect_delay.value()
        self.timer_collect_delay = QTimer()
        self.timer_collect_delay.setInterval(1000)
        self.timer_collect_delay.timeout.connect(self.collect_delay_tick)
        self.collect_total_trials = 0
        self.collect_current_trial = 0
        self.collect_action_label = ''
        self.collect_delay_remaining = 0
        self.collect_duration_remaining = 0
        self.checkBox_complex_csi.stateChanged.connect(self.update_physics_capture_config)
        self.comboBox_calibration_role.currentIndexChanged.connect(self.update_physics_capture_config)
        self.update_physics_capture_config()

        self.train_duration = self.timeEdit_train_duration.time()
        self.timer_train_duration = QTimer()
        self.timer_train_duration.setInterval(1000)
        self.timer_train_duration.timeout.connect(
            self.spinBox_train_duration_show)

        self.train_delay = self.timeEdit_train_delay.time()
        self.timer_train_delay = QTimer()
        self.timer_train_delay.setInterval(1000)
        self.timer_train_delay.timeout.connect(self.timeEdit_train_delay_show)

        self.timer_status_record = QTimer()
        self.timer_status_record.timeout.connect(self.show_statistics_status_record)
        self.timer_status_record.setInterval(1000)

        self.timer_evaluate_statistics = QTimer()
        self.timer_evaluate_statistics.timeout.connect(self.show_evaluate_statistics)
        self.timer_evaluate_statistics.setInterval(1000)

        self.checkBox_raw_data_show()
        self.checkBox_radar_model_show()
        self.checkBox_display_eigenvalues_table_show()
        self.apply_language()

    def tr_text(self, zh, en):
        return en if self.language == 'en' else zh

    def save_gui_config(self, **updates):
        self.gui_config.update(updates)
        try:
            self.gui_config_path.write_text(
                json.dumps(self.gui_config, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        except Exception as e:
            print(f'Failed to save GUI config: {e}')

    def change_language(self, index):
        self.language = 'en' if index == 1 else 'zh'
        self.save_gui_config(language=self.language)
        self.apply_language()

    def apply_language(self):
        en = self.language == 'en'
        self.label_language.setText('Language' if en else '界面语言')
        self.label_2.setText('Display:' if en else '显示内容：')
        self.checkBox_raw_data.setText('Raw data' if en else '原始数据')
        self.checkBox_radar_model.setText('Radar model' if en else '雷达模型')
        self.groupBox_16.setTitle('Device commands' if en else '设备命令')
        self.groupBox_11.setTitle('Router' if en else '路由器')
        self.label_duration_2.setText('Password' if en else '密码')
        self.checkBox_router_auto_connect.setText('Auto connect' if en else '自动连接')
        self.label.setText('Custom command' if en else '自定义命令')
        self.groupBox_13.setTitle('Runtime log' if en else '运行日志')
        self.groupBox_radioHeader.setTitle('Receiver information' if en else '接收信息')
        self.groupBox_radar_model.setTitle('Radar model' if en else '雷达模型')
        self.groupBox_15.setTitle('Environment calibration' if en else '环境校准')
        self.label_delay_5.setText('Delay' if en else '延迟')
        self.label_delay_6.setText('Duration' if en else '持续时间')
        self.checkBox_train_add.setText('Append calibration' if en else '追加校准')
        self.groupBox_14.setTitle('Status prediction' if en else '状态预测')
        self.label_number_3.setText('Occupancy sensitivity' if en else '有人灵敏度')
        self.label_number_5.setText('Movement sensitivity' if en else '移动灵敏度')
        self.label_duration_7.setText('Outlier count' if en else '异常值数量')
        self.checkBox_display_eigenvalues_table.setText('Show data table' if en else '显示数据表')
        self.pushButton_predict_config.setText('Apply' if en else '应用配置')
        self.groupBox_19.setTitle('Statistics' if en else '统计信息')
        self.label_target_5.setText('Granularity' if en else '统计粒度')
        self.label_delay_9.setText('Time' if en else '时间')
        self.checkBox_statistics_auto_update.setText('Auto update' if en else '自动更新')
        self.pushButton_statistics_config.setText('Update' if en else '更新')
        self.groupBox_18.setTitle('Collection controls and experiment labels' if en else '采集控制与实验标签')
        self.label_participant_id.setText('Participant label (participant_id)' if en else '人员标签（participant_id）')
        self.label_room_id.setText('Location label (room_id)' if en else '地点标签（room_id）')
        self.label_complex_csi.setText('Physics CSI' if en else '物理 CSI')
        self.checkBox_complex_csi.setText('Save complex I/Q' if en else '保存复数 I/Q')
        role = self.comboBox_calibration_role.currentData()
        role_labels = (['Activity', 'Empty-room reference', 'Static-person reference'] if en else
                       ['动作采集', '空房参考', '静止人员参考'])
        for index, label in enumerate(role_labels):
            self.comboBox_calibration_role.setItemText(index, label)
        role_index = self.comboBox_calibration_role.findData(role)
        if role_index >= 0:
            self.comboBox_calibration_role.setCurrentIndex(role_index)
        self.label_target_4.setText('Activity label' if en else '采集动作')
        self.label_delay.setText('Wait before each trial' if en else '每次等待')
        self.label_duration.setText('Collection duration' if en else '采集时长')
        self.label_number.setText('Number of trials' if en else '采集次数')
        self.timeEdit_collect_delay.setSuffix(' s')
        self.spinBox_collect_duration.setSuffix(' s')
        current_action = self.comboBox_collect_target.currentData()
        labels = CSI_DATA_TARGET_LABELS_EN if en else CSI_DATA_TARGET_LABELS_ZH
        for index, label in enumerate(labels):
            self.comboBox_collect_target.setItemText(index, label)
        action_index = self.comboBox_collect_target.findData(current_action)
        if action_index >= 0:
            self.comboBox_collect_target.setCurrentIndex(action_index)
        command_labels = COMMAND_LABELS_EN if en else [item[1] for item in COMMAND_OPTIONS]
        for index, label in enumerate(command_labels):
            self.comboBox_command.setItemText(index, label)
        modes = ['Minute', 'Hour', 'Day'] if en else ['分钟', '小时', '天']
        for index, label in enumerate(modes):
            self.comboBox_statistics_mode.setItemText(index, label)
        self.lineEdit_participant_id.setPlaceholderText('e.g. P001' if en else '例如 P001')
        self.lineEdit_room_id.setPlaceholderText('e.g. R01 / LAB-A' if en else '例如 R01 / LAB-A')
        collecting = self.pushButton_collect_start.text() in {'停止', 'Stop'}
        self.pushButton_collect_start.setText(('Stop' if en else '停止') if collecting else ('Start' if en else '开始'))
        self.pushButton_collect_clean.setText('Clear data' if en else '清空数据')
        self.pushButton_command.setText('Send' if en else '发送')
        router_connected = self.pushButton_router_connect.text() in {'断开', 'Disconnect'}
        self.pushButton_router_connect.setText(
            ('Disconnect' if en else '断开') if router_connected else ('Connect' if en else '连接')
        )
        training = self.pushButton_train_start.text() in {'停止', 'Stop'}
        self.pushButton_train_start.setText(
            ('Stop' if en else '停止') if training else ('Start' if en else '开始')
        )
        self.model_radio_header.setHorizontalHeaderLabels(
            list(g_radio_header_pd.columns.values) if en else
            [RADIO_HEADER_LABELS_ZH.get(name, name) for name in g_radio_header_pd.columns.values]
        )
        status_headers = (['Room status', 'Person status', 'Duration', 'Start time', 'End time']
                          if en else ['房间状态', '人员状态', '持续时间', '开始时间', '结束时间'])
        self.model_status_record.setHorizontalHeaderLabels(status_headers)
        radar_headers = (['Status', 'Threshold', 'Value', 'Maximum', 'Minimum', 'Mean', 'Std. dev.']
                         if en else ['状态', '阈值', '数值', '最大值', '最小值', '平均值', '标准差'])
        self.model_radar_data_room.setHorizontalHeaderLabels(radar_headers)
        self.model_radar_data_human.setHorizontalHeaderLabels(radar_headers)
        self.model_evaluate_statistics.setHorizontalHeaderLabels(
            ['Type', 'Count', 'Ratio'] if en else ['类型', '数量', '占比']
        )
        title = 'ESP CSI Collection Tool' if en else 'ESP CSI 采集工具'
        if self.experiment_config:
            title += f" - {self.experiment_config['session_id']} - {self.lineEdit_participant_id.text()} - {self.lineEdit_room_id.text()}"
        self.setWindowTitle(title)

    def checkBox_raw_data_show(self):
        global g_display_raw_data
        g_display_raw_data = self.checkBox_raw_data.isChecked()

        if self.checkBox_raw_data.isChecked():
            self.groupBox_raw_data.show()
            self.timer_curve_subcarrier.start()
        else:
            self.groupBox_raw_data.hide()
            self.timer_curve_subcarrier.stop()

        self.save_gui_config(display_raw_data=self.checkBox_raw_data.isChecked())

    def checkBox_radar_model_show(self):
        global g_display_radar_model

        g_display_radar_model = self.checkBox_radar_model.isChecked()

        if self.checkBox_radar_model.isChecked():
            self.groupBox_radar_model.show()
            self.timer_status_record.start()
            self.timer_curve_radar.start()
        else:
            self.groupBox_radar_model.hide()
            self.timer_status_record.stop()
            self.timer_curve_radar.stop()

        self.save_gui_config(display_radar_model=self.checkBox_radar_model.isChecked())

    def checkBox_display_eigenvalues_table_show(self):
        global g_display_eigenvalues_table
        g_display_eigenvalues_table = self.checkBox_display_eigenvalues_table.isChecked()

        if self.checkBox_display_eigenvalues_table.isChecked():
            self.tableView_eigenvalues.show()
            self.timer_eigenvalues_table.start()
        else:
            self.tableView_eigenvalues.hide()
            self.timer_eigenvalues_table.stop()

        self.save_gui_config(
            display_eigenvalues_table=self.checkBox_display_eigenvalues_table.isChecked()
        )

    def show_router_auto_connect(self):
        self.save_gui_config(router_auto_connect=self.checkBox_router_auto_connect.isChecked())

    def median_filtering(self, waveform):
        tmp = waveform
        for i in range(1, waveform.shape[0] - 1):
            outliers_count = 0
            for j in range(waveform.shape[1]):
                if ((waveform[i - 1, j] - waveform[i, j] > 2 and waveform[i + 1, j] - waveform[i, j] > 2)
                or (waveform[i - 1, j] - waveform[i, j] < -2 and waveform[i + 1, j] - waveform[i, j] < -2)):
                    outliers_count += 1
                    continue

            if outliers_count > 16:
                for x in range(1, waveform.shape[1] - 1):
                    tmp[i, x] = (waveform[i - 1, x] + waveform[i + 1, x]) / 2
        waveform = tmp

    def show_curve_subcarrier(self):
        wn = 20 / (CSI_SAMPLE_RATE / 2)
        b, a = signal.butter(8, wn, 'lowpass')

        if self.wave_filtering_flag:
            self.median_filtering(g_csi_amplitude_array)
            csi_filtfilt_data = signal.filtfilt(b, a, g_csi_amplitude_array.T).T
        else:
            csi_filtfilt_data = g_csi_amplitude_array

        if self.curve_subcarrier_range[0] > csi_filtfilt_data.min() or self.curve_subcarrier_range[1] < csi_filtfilt_data.max():
            if csi_filtfilt_data.min() > 0 and self.curve_subcarrier_range[0] > csi_filtfilt_data.min():
                self.curve_subcarrier_range[0] = csi_filtfilt_data.min() - 1
            if self.curve_subcarrier_range[1] < csi_filtfilt_data.max():
                self.curve_subcarrier_range[1] = csi_filtfilt_data.max() + 1
            self.graphicsView_subcarrier.setYRange(
                self.curve_subcarrier_range[0], self.curve_subcarrier_range[1])

        for i in range(CSI_DATA_COLUMNS):
            self.curve_subcarrier[i].setData(csi_filtfilt_data[:, i])

        if self.wave_filtering_flag:
            csi_filtfilt_rssi = signal.filtfilt(
                b, a, g_rssi_array).astype(np.int32)
        else:
            csi_filtfilt_rssi = g_rssi_array
        self.curve_rssi.setData(csi_filtfilt_rssi)

        for i in range(g_radio_header_pd.shape[0]):
            for j in range(g_radio_header_pd.shape[1]):
                cell_value = g_radio_header_pd.iloc[i, j]
                if cell_value is None or (isinstance(cell_value, float) and np.isnan(cell_value)):
                    item = QStandardItem('')
                elif g_radio_header_pd.columns.values[j] in self.tableView_values.keys():
                    try:
                        str_values = self.tableView_values[g_radio_header_pd.columns.values[j]][int(cell_value)]
                        item = QStandardItem(str_values)
                    except (ValueError, TypeError, KeyError):
                        item = QStandardItem(str(cell_value))
                else:
                    # print(j, g_radio_header_pd.columns.values[j])
                    item = QStandardItem(str(cell_value) if cell_value is not None else '')
                self.model_radio_header.setItem(i, j, item)

    def show_curve_subcarrier_filter(self):
        self.wave_filtering_flag = self.checkBox_wave_filtering.isChecked()

    def show_curve_eigenvalue(self):
        for i in range(RADAR_targetS_LEN):
            self.curve_radar_eigenvalue[i].setData(
                g_radar_eigenvalue_array[:, i])
            self.curve_radar_eigenvalue_threshold[i].setData(
                g_radar_eigenvalue_threshold_array[:, i])

        curve_radar_title = f'{ROOM_STATUS_NAMES[g_radar_status_array[-1,0]]} {HUMAN_STATUS_NAMES[g_radar_status_array[-1,1]]}'
        self.graphicsView_eigenvalues.setTitle(curve_radar_title)

    def show_eigenvalue_table(self):
        for i in range(g_radar_data_room_pd.shape[0]):
            for j in range(g_radar_data_room_pd.shape[1]):
                # data_str = "%.5f" % 0.01
                # print(f"{g_radar_data_room_pd.iloc[i,j]}, {type(g_radar_data_room_pd.iloc[i,j])}")
                data_str = '%.5f' % g_radar_data_room_pd.iloc[i, j]
                item = QStandardItem(data_str)
                self.model_radar_data_room.setItem(i, j, item)

        for i in range(g_radar_data_human_pd.shape[0]):
            for j in range(g_radar_data_human_pd.shape[1]):
                # print(f"{g_radar_data_human_pd.iloc[i,j]}, {type(g_radar_data_human_pd.iloc[i,j])}")
                data_str = '%.5f' % g_radar_data_human_pd.iloc[i, j]
                item = QStandardItem(data_str)
                self.model_radar_data_human.setItem(i, j, item)

    def show_device_info(self, device_info_series):
        # print(device_info_series)
        for i in range(len(device_info_series)):
            self.model_device_info.setItem(
                i, 0, QStandardItem(DEVICE_INFO_LABELS_ZH.get(
                    device_info_series.index[i], device_info_series.index[i])))
            self.model_device_info.setItem(
                i, 1, QStandardItem(str(device_info_series.values[i])))
        # self.tableView_device_info.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

    def get_statistic_config(self):
        self.statistic_config['time'] = self.dateTimeEdit_statistics_time.dateTime().toPyDateTime()
        self.statistic_config['mode'] = self.comboBox_statistics_mode.currentData()
        self.statistic_config['auto_update'] = self.checkBox_statistics_auto_update.isChecked()

    def show_statistics_status_record_move(self):
        self.get_statistic_config()

        if self.statistic_config['mode'] == 'day':
            statistic_move_array = np.zeros(24, dtype=np.int32)
            statistic_move_title = datetime.strftime(
                self.statistic_config['time'], '%Y-%m-%d')
            statistic_move_pd = g_move_record_pd[g_move_record_pd['date']
                                                 == self.statistic_config['time'].date()]

            if len(statistic_move_pd):
                statistic_move_hour = statistic_move_pd.groupby(by='hour')[
                    'count'].sum()

                for index, value in statistic_move_hour.items():
                    statistic_move_array[index] = value

        elif self.statistic_config['mode'] == 'hour':
            statistic_move_array = np.zeros(60, dtype=np.int32)
            statistic_move_title = datetime.strftime(
                self.statistic_config['time'], '%Y-%m-%d %H')

            statistic_move_pd = g_move_record_pd[(g_move_record_pd['date'] == self.statistic_config['time'].date()) & (
                g_move_record_pd['hour'] == self.statistic_config['time'].time().hour)]

            if len(statistic_move_pd):
                statistic_move_minute = statistic_move_pd.groupby(by='minute')[
                    'count'].sum()
                for index, value in statistic_move_minute.items():
                    statistic_move_array[index] = value

        elif self.statistic_config['mode'] == 'minute':
            statistic_move_array = np.zeros(60, dtype=np.int32)
            statistic_move_title = datetime.strftime(
                self.statistic_config['time'], '%Y-%m-%d %H:%M')

            statistic_move_pd = g_move_record_pd[(g_move_record_pd['date'] == self.statistic_config['time'].date())
                                                 & (g_move_record_pd['hour'] == self.statistic_config['time'].time().hour)
                                                      & (g_move_record_pd['hour'] == self.statistic_config['time'].time().hour)
                                                 & (g_move_record_pd['hour'] == self.statistic_config['time'].time().hour)
                                                 & (g_move_record_pd['minute'] == self.statistic_config['time'].time().minute)]

            if len(statistic_move_pd):
                statistic_move_second = statistic_move_pd.groupby(by='second')[
                    'count'].sum()
                for index, value in statistic_move_second.items():
                    statistic_move_array[index] = value
        else:
            print(f"fail mode: {self.statistic_config['mode']}")

        try:
            self.graphicsView_status_record.removeItem(self.bg)
        except Exception as e:
            # print(e)
            pass

        try:
            self.bg = pq.BarGraphItem(x=range(
                len(statistic_move_array)), height=statistic_move_array, width=0.3, brush='g')
            self.graphicsView_status_record.addItem(self.bg)

            self.graphicsView_status_record.showGrid(x=True, y=True)
            self.graphicsView_status_record.setXRange(
                0, len(statistic_move_array))
            # print(statistic_move_title)
            self.graphicsView_status_record.setTitle(
                statistic_move_title + '  Move Count')
        except Exception as e:
            print(e)

    def show_statistics_status_record(self):
        if self.checkBox_statistics_auto_update.isChecked():
            self.dateTimeEdit_statistics_time.setDateTime(g_current_time)
            self.show_statistics_status_record_move()

        # print(f"g_status_record table: {g_status_record_pd.shape}")

        for i in range(g_status_record_pd.shape[0]):
            for j in range(g_status_record_pd.shape[1]):
                item = QStandardItem(g_status_record_pd.iloc[i, j])
                self.model_status_record.setItem(i, j, item)

    def show_evaluate_statistics(self):
        if not g_evaluate_statistics_array.sum():
            return

        for i in range(g_evaluate_statistics_array.shape[0]):
            for j in range(g_evaluate_statistics_array.shape[1]):
                row = i * 2 + j

                item = QStandardItem(str(g_evaluate_statistics_array[i][j]))
                self.model_evaluate_statistics.setItem(row, 1, item)

                data_str = '%.2f%%' % (
                    g_evaluate_statistics_array[i][j] * 100 / g_evaluate_statistics_array.sum())
                item = QStandardItem(data_str)
                self.model_evaluate_statistics.setItem(row, 2, item)

    def command_boot(self):
        command = f'radar --csi_output_type {self.csi_output_type} --csi_output_format base64'
        self.serial_queue_write.put(command)
        if self.experiment_config and self.experiment_config.get('send_interval_ms') is not None:
            self.serial_queue_write.put(
                f"radar --send_data_interval {int(self.experiment_config['send_interval_ms'])}"
            )

        if self.checkBox_router_auto_connect.isChecked() and len(self.lineEdit_router_ssid.text()) > 0:
            self.command_router_connect()

        self.timer_boot_command.stop()

    def command_predict_config(self):
        command = (f'radar --predict_someone_sensitivity {self.doubleSpinBox_predict_someone_sensitivity.value()}' +
                   f' --predict_move_sensitivity {self.doubleSpinBox_predict_move_sensitivity.value()}')
        self.serial_queue_write.put(command)
        command = (f'radar --predict_buff_size {self.spinBox_predict_buffer_size.text()}' +
                   f' --predict_outliers_number {self.spinBox_predict_outliers_number.text()}')
        self.serial_queue_write.put(command)

    def command_router_connect(self):
        if self.pushButton_router_connect.text() in {'连接', 'Connect'}:
            self.pushButton_router_connect.setText(self.tr_text('断开', 'Disconnect'))
            self.pushButton_router_connect.setStyleSheet('color: red')

            command = 'wifi_config --ssid ' + ("\"%s\"" % self.lineEdit_router_ssid.text())
            if len(self.lineEdit_router_password.text()) >= 8:
                command += ' --password ' + self.lineEdit_router_password.text()
            self.serial_queue_write.put(command)
        else:
            self.pushButton_router_connect.setText(self.tr_text('连接', 'Connect'))
            self.pushButton_router_connect.setStyleSheet('color: black')
            command = 'ping --abort'
            self.serial_queue_write.put(command)
            command = 'wifi_config --disconnect'
            self.serial_queue_write.put(command)
            time.sleep(3)
            command = 'radar --csi_output_type ' + self.csi_output_type + ' --csi_output_format base64'
            self.serial_queue_write.put(command)

        self.save_gui_config(
            router_ssid=self.lineEdit_router_ssid.text(),
            router_password=self.lineEdit_router_password.text(),
        )

    def command_custom(self):
        command = self.lineEdit_command.text()
        if self.experiment_config and re.search(r'--(?:send_data_interval|csi_output_type)\b', command):
            QMessageBox.warning(
                self, '研究采集',
                '请在实验配置中修改 send_interval_ms 或 csi_output_type，并使用新的 session_id 启动采集。'
            )
            return
        self.serial_queue_write.put(command)

    def command_collect_target_start(self):
        action = self.comboBox_collect_target.currentData()
        duration_ms = self.spinBox_collect_duration.value() * 1000
        # Send one acquisition at a time so the configured waiting period can
        # be inserted between every two trials.
        command = (f'radar --collect_number 1' +
                   f' --collect_tagets {action}' +
                   f' --collect_duration {duration_ms}')
        self.serial_queue_write.put(command)

    def command_collect_target_stop(self):
        command = 'radar --collect_number 0 --collect_tagets unknown'
        self.serial_queue_write.put(command)

    def position_collect_overlay(self):
        width = min(max(680, self.centralwidget.width() * 5 // 6), self.centralwidget.width() - 24)
        height = min(max(360, self.centralwidget.height() * 3 // 5), self.centralwidget.height() - 24)
        self.collect_overlay.setGeometry(
            (self.centralwidget.width() - width) // 2,
            (self.centralwidget.height() - height) // 2,
            width,
            height,
        )

    def show_collect_overlay(self, state, text):
        self.collect_overlay_state = state
        if state == 'countdown':
            self.collect_overlay.setStyleSheet(
                'QFrame#collect_overlay { background-color: rgba(25, 34, 48, 238); '
                'border: 14px solid #ffcc00; border-radius: 36px; } '
                'QLabel { color: #ffffff; font-size: 78px; font-weight: 900; }'
            )
        else:
            self.collect_overlay.setStyleSheet(
                'QFrame#collect_overlay { background-color: rgba(86, 214, 104, 245); '
                'border: 16px solid #087522; border-radius: 36px; } '
                'QLabel { color: #b00000; font-size: 72px; font-weight: 900; }'
            )
        self.collect_overlay_label.setText(text)
        self.position_collect_overlay()
        self.collect_overlay.show()
        self.collect_overlay.raise_()

    def hide_collect_overlay(self):
        self.collect_overlay_state = None
        self.collect_overlay.hide()

    def set_collect_inputs_enabled(self, enabled):
        for widget in (
            self.lineEdit_participant_id, self.lineEdit_room_id,
            self.comboBox_collect_target, self.timeEdit_collect_delay,
            self.spinBox_collect_duration, self.spinBox_collect_number,
            self.checkBox_complex_csi, self.comboBox_calibration_role,
        ):
            widget.setEnabled(enabled)

    def update_physics_capture_config(self):
        enabled = self.checkBox_complex_csi.isChecked()
        role = self.comboBox_calibration_role.currentData() or 'activity'
        if self.experiment_config is not None:
            self.experiment_config['complex_csi_enabled'] = enabled
            self.experiment_config['csi_component_order'] = 'imag_real_interleaved'
            self.experiment_config['channel_filter_enabled'] = False
            self.experiment_config['csi_scale_shift'] = 0
            self.experiment_config['calibration_role'] = role
        self.serial_queue_write.put({
            'type': 'update_physics_capture',
            'complex_csi_enabled': enabled,
            'calibration_role': role,
        })
        self.save_gui_config(complex_csi_enabled=enabled, calibration_role=role)

    def apply_collection_metadata(self):
        participant_id = self.lineEdit_participant_id.text().strip()
        room_id = self.lineEdit_room_id.text().strip()
        if not participant_id or not room_id:
            QMessageBox.warning(self, '采集标签缺失', '请输入人员标签和地点标签后再开始采集。')
            return False
        compact_id = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
        if not compact_id.fullmatch(participant_id) or not compact_id.fullmatch(room_id):
            QMessageBox.warning(
                self, '采集标签格式错误',
                "人员和地点标签只能包含英文字母、数字、'.'、'_'、'-'，最长 64 个字符。"
            )
            return False
        if self.experiment_config:
            old_participant = str(self.experiment_config.get('participant_id', ''))
            old_room = str(self.experiment_config.get('room_id', ''))
            if participant_id != old_participant or room_id != old_room:
                updated_config = dict(self.experiment_config)
                updated_config['participant_id'] = participant_id
                updated_config['room_id'] = room_id
                updated_config['date'] = datetime.now().strftime('%Y-%m-%d')
                try:
                    ExperimentLedger(
                        Path(self.data_root) / room_id / updated_config['date'], updated_config
                    )
                except Exception as e:
                    QMessageBox.warning(
                        self, '采集标签冲突',
                        f'当前会话不能使用这组人员/地点标签：\n{e}\n\n请更换 session_id 后重新启动。'
                    )
                    return False
                updated_config['config_source'] = 'GUI participant/room override'
                updated_config['config_sha256'] = ''
                self.experiment_config.update(updated_config)
                self.setWindowTitle(
                    f"ESP CSI 研究采集 - {self.experiment_config['session_id']} - "
                    f"{participant_id} - {room_id}"
                )
        self.serial_queue_write.put({
            'type': 'update_collection_metadata',
            'participant_id': participant_id,
            'room_id': room_id,
            'complex_csi_enabled': self.checkBox_complex_csi.isChecked(),
            'calibration_role': self.comboBox_calibration_role.currentData() or 'activity',
        })
        try:
            self.save_gui_config(participant_id=participant_id, room_id=room_id)
        except Exception as e:
            self.show_textBrowser_log(f'<font color="yellow">无法保存人员/地点默认值：{e}</font>')
        return True

    def collect_delay_tick(self):
        self.collect_delay_remaining -= 1
        if self.collect_delay_remaining <= 0:
            self.timer_collect_delay.stop()
            self.begin_current_collection_trial()
            return
        self.show_collection_wait()

    def start_collection_wait(self):
        self.collect_delay_remaining = self.collect_saved_delay
        if self.collect_delay_remaining <= 0:
            self.begin_current_collection_trial()
            return
        self.show_collection_wait()
        self.timer_collect_delay.start()

    def show_collection_wait(self):
        if self.language == 'en':
            text = (f'GET READY: {self.collect_action_label}\n'
                    f'TRIAL {self.collect_current_trial}/{self.collect_total_trials}\n'
                    f'{self.collect_delay_remaining} s')
        else:
            text = (f'准备采集：{self.collect_action_label}\n'
                    f'第 {self.collect_current_trial}/{self.collect_total_trials} 次\n'
                    f'{self.collect_delay_remaining} s')
        self.show_collect_overlay('countdown', text)

    def begin_current_collection_trial(self):
        self.command_collect_target_start()
        self.spinBox_collect_number.setStyleSheet('color: red')
        self.timeEdit_collect_delay.setStyleSheet('color: black')
        self.collect_duration_remaining = self.spinBox_collect_duration.value()
        self.show_current_collection_trial()
        self.timer_collect_duration.start()

    def show_current_collection_trial(self):
        if self.language == 'en':
            text = (f'RECORDING: {self.collect_action_label}\n'
                    f'TRIAL {self.collect_current_trial}/{self.collect_total_trials}\n'
                    f'{self.collect_duration_remaining} s')
        else:
            text = (f'正在采集：{self.collect_action_label}\n'
                    f'第 {self.collect_current_trial}/{self.collect_total_trials} 次\n'
                    f'剩余 {self.collect_duration_remaining} s')
        self.show_collect_overlay('collecting', text)

    def collect_duration_tick(self):
        self.collect_duration_remaining -= 1
        if self.collect_duration_remaining <= 0:
            self.timer_collect_duration.stop()
            self.collect_trial_finished()
            return
        self.show_current_collection_trial()

    def collect_trial_finished(self):
        self.command_collect_target_stop()
        self.hide_collect_overlay()
        if self.collect_current_trial < self.collect_total_trials:
            self.collect_current_trial += 1
            self.start_collection_wait()
            return
        self.finish_collection(send_stop=False)

    def finish_collection(self, send_stop=True):
        self.timer_collect_delay.stop()
        self.timer_collect_duration.stop()
        if send_stop:
            self.command_collect_target_stop()
        self.hide_collect_overlay()
        self.spinBox_collect_number.setValue(self.collect_saved_number)
        self.timeEdit_collect_delay.setValue(self.collect_saved_delay)
        self.spinBox_collect_number.setStyleSheet('color: black')
        self.timeEdit_collect_delay.setStyleSheet('color: black')
        self.pushButton_collect_start.setStyleSheet('color: black')
        self.pushButton_collect_start.setText(self.tr_text('开始', 'Start'))
        self.set_collect_inputs_enabled(True)

    def pushButton_collect_show(self):
        if self.pushButton_collect_start.text() in {'开始', 'Start'}:
            if self.comboBox_collect_target.currentIndex() == 0 or self.spinBox_collect_number.value() == 0:
                err = QErrorMessage(self)
                err.setWindowTitle('采集参数错误')
                err.showMessage(
                    '请选择采集动作，并确认采集次数大于 0。')
                err.show()
                return

            if not self.apply_collection_metadata():
                return

            self.collect_saved_number = self.spinBox_collect_number.value()
            self.collect_saved_delay = self.timeEdit_collect_delay.value()
            self.collect_total_trials = self.collect_saved_number
            self.collect_current_trial = 1
            self.collect_action_label = self.comboBox_collect_target.currentText()
            self.save_gui_config(
                participant_id=self.lineEdit_participant_id.text().strip(),
                room_id=self.lineEdit_room_id.text().strip(),
                collect_action=self.comboBox_collect_target.currentData(),
                collect_wait_seconds=self.timeEdit_collect_delay.value(),
                collect_duration_seconds=self.spinBox_collect_duration.value(),
                collect_number=self.spinBox_collect_number.value(),
                complex_csi_enabled=self.checkBox_complex_csi.isChecked(),
                calibration_role=self.comboBox_calibration_role.currentData() or 'activity',
            )
            self.pushButton_collect_start.setText(self.tr_text('停止', 'Stop'))
            self.pushButton_collect_start.setStyleSheet('color: red')
            self.timeEdit_collect_delay.setStyleSheet('color: red')
            self.set_collect_inputs_enabled(False)
            self.pushButton_collect_start.setEnabled(True)
            self.start_collection_wait()
        else:
            self.finish_collection(send_stop=True)

    def pushButton_collect_clean_show(self):
        folder_path = 'data'

        message = QMessageBox.warning(
            self, '警告', "将删除 data 目录中的全部文件，是否继续？",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
        if message == QMessageBox.Cancel:
            return

        for root, dirs, files in os.walk(folder_path, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))

    def command_train_start(self):
        command = f'radar --train_start'

        if self.checkBox_train_add.isChecked():
            command += ' --train_add'

        self.serial_queue_write.put(command)

    def command_train_stop(self):
        command = 'radar --train_stop'
        self.serial_queue_write.put(command)

    def spinBox_train_duration_show(self):
        time_temp = self.timeEdit_train_duration.time()
        second = time_temp.hour() * 3600 + time_temp.minute() * 60 + time_temp.second()
        if second > 0:
            self.timeEdit_train_duration.setTime(time_temp.addSecs(-1))
            self.timer_train_duration.start()
        else:
            self.timer_train_duration.stop()
            self.command_train_stop()
            self.timeEdit_train_delay.setTime(self.train_delay)
            self.timeEdit_train_duration.setTime(self.train_duration)
            self.timeEdit_train_duration.setStyleSheet('color: black')
            self.pushButton_train_start.setText(self.tr_text('开始', 'Start'))
            self.pushButton_train_start.setStyleSheet('color: black')

    def timeEdit_train_delay_show(self):
        time_temp = self.timeEdit_train_delay.time()
        second = time_temp.hour() * 3600 + time_temp.minute() * 60 + time_temp.second()
        if second > 0:
            self.timeEdit_train_delay.setTime(time_temp.addSecs(-1))
            self.timer_train_delay.start()
        else:
            self.timer_train_delay.stop()
            self.command_train_start()
            self.spinBox_train_duration_show()
            self.timeEdit_train_duration.setStyleSheet('color: red')
            self.timeEdit_train_delay.setStyleSheet('color: black')

    def pushButton_train_show(self):
        if self.pushButton_train_start.text() in {'开始', 'Start'}:
            reply = QMessageBox.question(self, '提示', '环境校准期间必须确保房间内无人，是否继续？',
                                         QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
            if reply == QMessageBox.Cancel:
                return

            self.train_delay = self.timeEdit_train_delay.time()
            self.train_duration = self.timeEdit_train_duration.time()
            self.pushButton_train_start.setText(self.tr_text('停止', 'Stop'))
            self.pushButton_train_start.setStyleSheet('color: red')
            self.timeEdit_train_delay.setStyleSheet('color: red')

            self.timeEdit_train_delay_show()
        else:
            self.timer_train_delay.stop()
            self.timer_train_duration.stop()
            self.command_train_stop()
            self.timeEdit_train_delay.setTime(self.train_delay)
            self.timeEdit_train_duration.setTime(self.train_duration)
            self.pushButton_train_start.setText(self.tr_text('开始', 'Start'))
            self.pushButton_train_start.setStyleSheet('color: black')

    def comboBox_command_show(self):
        self.lineEdit_command.setText(self.comboBox_command.currentData() or '')

    def show_textBrowser_log(self, str):
        self.textBrowser_log.append(str)
        # self.textBrowser_log.moveCursor(self.textBrowser_log.textCursor().End)

    def closeEvent(self, event):
        self.save_gui_config(
            participant_id=self.lineEdit_participant_id.text().strip(),
            room_id=self.lineEdit_room_id.text().strip(),
            collect_action=self.comboBox_collect_target.currentData() or 'none',
            collect_wait_seconds=self.timeEdit_collect_delay.value(),
            collect_duration_seconds=self.spinBox_collect_duration.value(),
            collect_number=self.spinBox_collect_number.value(),
            language=self.language,
            complex_csi_enabled=self.checkBox_complex_csi.isChecked(),
            calibration_role=self.comboBox_calibration_role.currentData() or 'activity',
        )
        self.serial_queue_write.put('exit')
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'collect_overlay') and self.collect_overlay.isVisible():
            self.position_collect_overlay()


def csi_data_handle(self, data):
    # Rotate data
    g_csi_amplitude_array[:-1] = g_csi_amplitude_array[1:]
    g_rssi_array[:-1] = g_rssi_array[1:]
    g_radio_header_pd.iloc[1:] = g_radio_header_pd.iloc[:-1]

    csi_raw_data = data['data']
    data_len = int(data['len']) if 'len' in data else len(csi_raw_data)

    if data_len == 104:
        for i in range(CSI_DATA_COLUMNS):
            idx = csi_vaid_subcarrier_index[i]

            if idx * 4 + 3 >= len(csi_raw_data):
                return
            real_high = csi_raw_data[idx * 4 + 1]
            imag_high = csi_raw_data[idx * 4 + 3]
            real_low = csi_raw_data[idx * 4] & 0xFF
            imag_low = csi_raw_data[idx * 4 + 2] & 0xFF

            real = (((int(real_high) << 12) >> 4) | real_low)
            imag = (((int(imag_high) << 12) >> 4) | imag_low)
            if real > 32767:
                real = real - 65536
            if imag > 32767:
                imag = imag - 65536

            g_csi_amplitude_array[-1][i] = np.abs(complex(real, imag))
    else:
        for i in range(CSI_DATA_COLUMNS):
            if csi_vaid_subcarrier_index[i]*2+1 >= len(csi_raw_data):
                return
            g_csi_amplitude_array[-1][i] = np.abs(complex(csi_raw_data[csi_vaid_subcarrier_index[i]*2], csi_raw_data[csi_vaid_subcarrier_index[i]*2+1]))

    g_rssi_array[-1] = data['rssi']
    g_radio_header_pd.loc[0] = data[1:len(CSI_DATA_COLUMNS_NAMES)-1]
    # print(g_radio_header_pd.loc[0])

    return


def radar_data_handle(self, data):
    g_radar_eigenvalue_array[:-1] = g_radar_eigenvalue_array[1:]
    g_radar_eigenvalue_threshold_array[:-
                                       1] = g_radar_eigenvalue_threshold_array[1:]
    g_radar_status_array[:-1] = g_radar_status_array[1:]

    global g_current_time

    current_time_str = data['timestamp']
    g_current_time = datetime.strptime(
        data['timestamp'], '%Y-%m-%d %H:%M:%S.%f')

    for taget_index in range(RADAR_targetS_LEN):
        g_radar_eigenvalue_array[-1][taget_index] = data[f'waveform_{RADAR_WAVEFORM_NAMES[taget_index]}']
        g_radar_eigenvalue_threshold_array[-1][taget_index] = data[
            f'waveform_{RADAR_WAVEFORM_NAMES[taget_index]}_threshold']
        g_radar_status_array[-1][taget_index] = data[f'{RADAR_targetS_NAMES[taget_index]}_status']

    if g_radar_status_array[-1][0] != g_radar_status_array[-2][0] or g_radar_status_array[-1][1] != g_radar_status_array[-2][1]:
        g_status_record_pd[1:] = g_status_record_pd[:-1]

        g_status_record_pd.loc[0, 'start_time'] = current_time_str
        g_status_record_pd.loc[0, 'room'] = ROOM_STATUS_NAMES[g_radar_status_array[-1][0]]
        g_status_record_pd.loc[0, 'human'] = HUMAN_STATUS_NAMES[g_radar_status_array[-1][1]]

        if len(g_status_record_pd.loc[1, 'start_time']):
            g_status_record_pd.loc[1, 'stop_time'] = current_time_str

    if len(g_status_record_pd.loc[0, 'start_time']):
        temp_time = g_current_time - \
            datetime.strptime(
                g_status_record_pd.loc[0, 'start_time'], '%Y-%m-%d %H:%M:%S.%f')
        g_status_record_pd.loc[0, 'spend_time'] = f'{temp_time}'[:-3]

    if g_radar_status_array[-1][1]:
        index = len(g_move_record_pd)
        g_move_record_pd.loc[index] = [g_current_time.date(), g_current_time.time(
        ).hour, g_current_time.time().minute, g_current_time.time().second, 1]

    g_evaluate_statistics_array[g_radar_status_array[-1,
                                                   0], g_radar_status_array[-1, 1]] += 1
    if (g_radar_eigenvalue_threshold_array[-1, 0] != g_radar_eigenvalue_threshold_array[-2, 0]
        or g_radar_eigenvalue_threshold_array[-1, 1] != g_radar_eigenvalue_threshold_array[-2, 1]):
        self.signal_wareform_threshold.emit()

    if g_display_eigenvalues_table:
        try:
            g_radar_data_room_pd[1:] = g_radar_data_room_pd[:-1]
            g_radar_data_room_pd.loc[0]['status'] = g_radar_status_array[-1, 0]
            g_radar_data_room_pd.loc[0]['threshold'] = g_radar_eigenvalue_threshold_array[-1, 0]
            g_radar_data_room_pd.loc[0]['value'] = g_radar_eigenvalue_array[-1, 0]

            value_buff = g_radar_eigenvalue_array[-10:, 0]
            g_radar_data_room_pd.loc[0]['max'] = np.max(value_buff)
            g_radar_data_room_pd.loc[0]['min'] = np.min(value_buff)
            g_radar_data_room_pd.loc[0]['mean'] = np.mean(value_buff)
            g_radar_data_room_pd.loc[0]['std'] = np.std(value_buff)

            g_radar_data_human_pd[1:] = g_radar_data_human_pd[:-1]
            g_radar_data_human_pd.loc[0]['status'] = g_radar_status_array[-1, 1]
            g_radar_data_human_pd.loc[0]['threshold'] = g_radar_eigenvalue_threshold_array[-1, 1]
            g_radar_data_human_pd.loc[0]['value'] = g_radar_eigenvalue_array[-1, 1]

            value_buff = g_radar_eigenvalue_array[-10:, 1]
            g_radar_data_human_pd.loc[0]['max'] = np.max(value_buff)
            g_radar_data_human_pd.loc[0]['min'] = np.min(value_buff)
            g_radar_data_human_pd.loc[0]['mean'] = np.mean(value_buff)
            g_radar_data_human_pd.loc[0]['std'] = np.std(value_buff)
        except Exception as e:
            print(e)
    return

class DataHandleThread(QThread):
    signal_log_msg = pyqtSignal(str)
    signal_radar_time_domain = pyqtSignal(str)
    signal_exit = pyqtSignal()
    signal_device_info = pyqtSignal(pd.Series)
    signal_wareform_threshold = pyqtSignal()

    def __init__(self, serial_queue_read):
        super().__init__()

        self.serial_queue_read = serial_queue_read
        self.taget_count = 0

    def run(self):
        while True:
            # print(f"g_display_raw_data: {g_display_raw_data}")
            series = self.serial_queue_read.get()
            if series['type'] == 'EXIT_EVENT':
                break
            elif series['type'] == 'DEVICE_INFO' and g_display_raw_data:
                g_device_info_series = series.copy()
                self.signal_device_info.emit(series)
            elif series['type'] == 'CSI_DATA' and g_display_raw_data:
                csi_data_handle(self, series)
            elif series['type'] == 'RADAR_DADA' and g_display_radar_model:
                radar_data_handle(self, series)
            elif series['type'] == 'LOG_DATA' and g_display_raw_data:
                if series['tag'] == 'I':
                    color = 'green'
                elif series['tag'] == 'W':
                    color = 'yellow'
                elif series['tag'] == 'E':
                    color = 'red'
                else:
                    color = 'white'

                data = "<font color=\'%s\'>%s (%s) %s <font>" % (
                    color, series['tag'], series['timestamp'], series['data'])
                self.signal_log_msg.emit(data)
            elif series['type'] == 'FAIL_EVENT':
                print(f"Failed: {series['data']}")

                self.signal_exit.emit()
                time.sleep(1)
                sys.exit(0)
            else:
                # print("error: ", series)
                pass

            QApplication.processEvents()

def serial_handle(queue_read, queue_write, port, experiment_config=None, data_root='data'):
    def dated_collection_root(config):
        room_id = str(config.get('room_id', '')).strip()
        capture_date = datetime.now().strftime('%Y-%m-%d')
        return Path(data_root) / room_id / capture_date

    def run_offline(message):
        print(message)
        queue_read.put(pd.Series(
            index=['type', 'tag', 'timestamp', 'data'],
            data=['LOG_DATA', 'W', '离线', message]
        ))
        while True:
            command = queue_write.get()
            if command == 'exit':
                return
            if isinstance(command, dict) and command.get('type') == 'update_collection_metadata':
                continue
            if isinstance(command, dict) and command.get('type') == 'update_physics_capture':
                continue
            queue_read.put(pd.Series(
                index=['type', 'tag', 'timestamp', 'data'],
                data=['LOG_DATA', 'W', '离线', f'未连接 ESP32，已忽略命令：{command}']
            ))

    if not port:
        run_offline('当前为离线界面模式；使用 -p COMx 重新启动后可连接 ESP32。')
        return

    try:
        set = serial.Serial(port=port, baudrate=2000000,
                            bytesize=8, parity='N', stopbits=1, timeout=0.1)
    except Exception as e:
        run_offline(f'无法打开串口 {port}：{e}。界面已切换到离线模式。')
        return

    print('串口已打开：', port)

    # Wait a second to let the port initialize
    set.flushInput()

    ledger = None
    session_data_root = Path(data_root)
    if experiment_config:
        try:
            experiment_config = dict(experiment_config)
            experiment_config['date'] = datetime.now().strftime('%Y-%m-%d')
            session_data_root = dated_collection_root(experiment_config)
            ledger = ExperimentLedger(session_data_root, experiment_config)
            print(f"研究采集已启用：会话={experiment_config['session_id']}，目录={session_data_root}")
        except Exception as e:
            data_series = pd.Series(index=['type', 'data'], data=['FAIL_EVENT', f'Invalid experiment config: {e}'])
            queue_read.put(data_series)
            set.close()
            return
    else:
        print('警告：兼容采集模式不记录参与者、会话和房间信息，不能用于跨域泛化结论。')
    active_participant_id = str(experiment_config.get('participant_id', 'PERSON')) if experiment_config else 'PERSON'
    active_room_id = str(experiment_config.get('room_id', 'ROOM')) if experiment_config else 'ROOM'

    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_folder = Path('log') / (f"{experiment_config['session_id']}_{run_stamp}" if experiment_config else run_stamp)
    folder_list = [log_folder, session_data_root]
    for folder in folder_list:
        Path(folder).mkdir(parents=True, exist_ok=True)

    data_valid_list = pd.DataFrame(columns=['type', 'columns_names', 'file_name', 'file_fd', 'file_writer'],
                                   data=[['CSI_DATA', CSI_DATA_COLUMNS_NAMES, str(log_folder / 'csi_data.csv'), None, None],
                                         ['RADAR_DADA', RADAR_DATA_COLUMNS_NAMES, str(log_folder / 'radar_data.csv'), None, None],
                                         ['DEVICE_INFO', DEVICE_INFO_COLUMNS_NAMES, str(log_folder / 'device_info.csv'), None, None],
                                         ['SYSTEM_TELEMETRY', SYSTEM_TELEMETRY_COLUMNS_NAMES, str(log_folder / 'system_telemetry.csv'), None, None]])

    for i in range(len(data_valid_list)):
        fd = open(data_valid_list.at[i, 'file_name'], 'w')
        writer = csv.writer(fd)
        writer.writerow(data_valid_list.at[i, 'columns_names'])
        data_valid_list.at[i, 'file_fd'] = fd
        data_valid_list.at[i, 'file_writer'] = writer

    log_data_writer = open(log_folder / 'log_data.txt', 'w+', encoding='utf-8')
    taget_last = 'unknown'
    taget_seq_last = 0
    csi_target_data_file_fd = None
    taget_data_writer = None
    current_collect_duration_ms = None

    def close_target_recording(status='complete'):
        nonlocal csi_target_data_file_fd, taget_data_writer
        if csi_target_data_file_fd is not None:
            csi_target_data_file_fd.flush()
            os.fsync(csi_target_data_file_fd.fileno())
            csi_target_data_file_fd.close()
            csi_target_data_file_fd = None
            taget_data_writer = None
        if ledger is not None and ledger.active is not None:
            result = ledger.finish(status=status)
            if result:
                print(f"recording finalized: {result['recording_id']}, frames={result['frame_count']}, gaps={result['sequence_gap_count']}")

    resources_closed = False

    def close_serial_resources(recording_status='aborted'):
        """Close every output exactly once and preserve an auditable partial recording."""
        nonlocal resources_closed
        if resources_closed:
            return
        resources_closed = True
        close_target_recording(status=recording_status)
        for fd in data_valid_list['file_fd']:
            if fd is not None and not fd.closed:
                fd.flush()
                fd.close()
        if not log_data_writer.closed:
            log_data_writer.flush()
            log_data_writer.close()
        if set.is_open:
            set.close()

    set.write('restart\r\n'.encode('utf-8'))
    time.sleep(0.01)

    while True:
        if not queue_write.empty():
            command = queue_write.get()

            if command == 'exit':
                close_serial_resources(recording_status='aborted')
                return

            if isinstance(command, dict) and command.get('type') == 'update_collection_metadata':
                participant_id = str(command.get('participant_id', '')).strip()
                room_id = str(command.get('room_id', '')).strip()
                if ledger is None or experiment_config is None:
                    session_data_root = Path(data_root) / room_id / datetime.now().strftime('%Y-%m-%d')
                    session_data_root.mkdir(parents=True, exist_ok=True)
                    active_participant_id = participant_id
                    active_room_id = room_id
                    print(f'兼容采集标签已更新：人员={participant_id}，地点={room_id}，目录={session_data_root}')
                    continue
                if ledger.active is not None:
                    queue_read.put(pd.Series(
                        index=['type', 'tag', 'timestamp', 'data'],
                        data=['LOG_DATA', 'E', '采集', '采集进行中，不能修改人员或地点标签。']
                    ))
                    continue
                try:
                    updated_config = dict(experiment_config)
                    updated_config['participant_id'] = participant_id
                    updated_config['room_id'] = room_id
                    updated_config['date'] = datetime.now().strftime('%Y-%m-%d')
                    updated_root = dated_collection_root(updated_config)
                    # Re-open the ledger so an existing manifest is checked
                    # before metadata can be used for a new recording.
                    updated_ledger = ExperimentLedger(updated_root, updated_config)
                except Exception as e:
                    queue_read.put(pd.Series(
                        index=['type', 'tag', 'timestamp', 'data'],
                        data=['LOG_DATA', 'E', '采集标签', f'人员或地点标签更新失败：{e}']
                    ))
                    continue
                experiment_config.update(updated_config)
                session_data_root = updated_root
                ledger = updated_ledger
                active_participant_id = participant_id
                active_room_id = room_id
                print(f'采集标签已更新：人员={participant_id}，地点={room_id}')
                continue

            if isinstance(command, dict) and command.get('type') == 'update_physics_capture':
                if ledger is not None and ledger.active is not None:
                    queue_read.put(pd.Series(
                        index=['type', 'tag', 'timestamp', 'data'],
                        data=['LOG_DATA', 'E', '物理 CSI', '采集进行中，不能修改复数 CSI 或校准角色。']
                    ))
                    continue
                enabled = bool(command.get('complex_csi_enabled', True))
                role = str(command.get('calibration_role', 'activity'))
                if role not in {'activity', 'empty_room_reference', 'static_reference'}:
                    continue
                if experiment_config is not None:
                    experiment_config['complex_csi_enabled'] = enabled
                    experiment_config['csi_component_order'] = 'imag_real_interleaved'
                    experiment_config['channel_filter_enabled'] = False
                    experiment_config['csi_scale_shift'] = 0
                    experiment_config['calibration_role'] = role
                    if ledger is not None:
                        ledger.update_capture_role(
                            complex_csi_enabled=enabled, calibration_role=role)
                # Disable channel smoothing and manual scaling so the stored
                # I/Q bytes remain suitable for physics-guided processing.
                set.write(b'radar --physics_mode\r\n')
                print(f'物理 CSI 配置已更新：complex={enabled}, role={role}')
                continue

            collect_match = re.search(r'--collect_duration\s+(\d+)', command)
            if collect_match:
                current_collect_duration_ms = int(collect_match.group(1))

            command = command + '\r\n'
            set.write(command.encode('utf-8'))
            print(f'{datetime.now()}, serial write: {command}')
            continue

        try:
            raw_bytes = set.readline()
            host_received_ns = time.monotonic_ns()
            if not raw_bytes:
                continue
            try:
                strings = raw_bytes.decode('utf-8').strip()
            except UnicodeDecodeError:
                strings = raw_bytes.decode('utf-8', errors='replace').strip()
        except Exception as e:
            data_series = pd.Series(index=['type', 'data'],
                                    data=['FAIL_EVENT', f'Failed to read serial: {e}'])
            queue_read.put(data_series)
            close_serial_resources(recording_status='aborted')
            return

        if not strings:
            continue

        # print(f"fail: {strings}")
        for i in range(len(data_valid_list)):
            index = strings.find(data_valid_list.at[i, 'type'])
            if index >= 0:
                strings = strings[index:]
                # 只取第一行，避免日志信息混入
                first_line_end = strings.find('\n')
                if first_line_end > 0:
                    strings = strings[:first_line_end]
                # 移除可能的回车符
                strings = strings.rstrip('\r')

                csv_reader = csv.reader(StringIO(strings))
                try:
                    data = next(csv_reader)
                except StopIteration:
                    continue

                if len(data) == len(data_valid_list.at[i, 'columns_names']):
                    # 清理 base64 数据字段（最后一个字段），移除可能的日志信息
                    if data_valid_list.at[i, 'type'] == 'CSI_DATA' and len(data) > 0:
                        data_field = data[-1]
                        # 使用正则表达式查找日志模式，只保留 base64 部分
                        log_patterns = [
                            r'->valid_len',
                            r':\s*\d+',  # 冒号后跟数字
                            r'\(\d+\)',  # 括号中的数字
                            r'[DIWE]\s*\(',  # 日志级别
                        ]

                        min_log_start = len(data_field)
                        for pattern in log_patterns:
                            match = re.search(pattern, data_field)
                            if match:
                                min_log_start = min(min_log_start, match.start())

                        # 如果找到日志信息且位置合理（避免误判），只保留 base64 部分
                        if min_log_start < len(data_field) and min_log_start > 10:
                            data[-1] = data_field[:min_log_start]

                    data_series = pd.Series(
                        data, index=data_valid_list.at[i, 'columns_names'], dtype=object)

                    if 'timestamp' in data_series.index:
                        try:
                            datetime.strptime(
                                data_series['timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                        except Exception as e:
                            data_series['timestamp'] = datetime.now().strftime(
                                '%Y-%m-%d %H:%M:%S.%f')[:-3]
                            # print(e)

                    # print(data_series)
                    if data_series['type'] == 'CSI_DATA':
                        try:
                            # csi_raw_data = json.loads(data_series['data'])
                            csi_raw_data = base64_decode_bin(
                                data_series['data'])
                            # 如果解码失败返回空列表，跳过这条数据
                            if len(csi_raw_data) == 0:
                                print(f"CSI_DATA decode failed, skipping data: {data_series['data'][:50]}...")
                                break
                            if len(csi_raw_data) != int(data_series['len']):
                                # if len(csi_raw_data) != 104 and len(csi_raw_data) != 216 and len(csi_raw_data) != 328 and len(csi_raw_data) != 552:
                                print(
                                    f"CSI_DATA, expected: {data_series['len']}, actual: {len(csi_raw_data)}")
                                break
                        except Exception as e:
                            print(
                                f"json.JSONDecodeError: {e}, data: {data_series['data']}")
                            break

                        data_series['data'] = csi_raw_data

                        if data_series['taget'] != 'unknown':
                            if data_series['taget'] != taget_last or data_series['taget_seq'] != taget_seq_last:
                                close_target_recording(status='complete')
                                try:
                                    action_label = validate_action_label(data_series['taget'])
                                except ValueError as e:
                                    queue_read.put(pd.Series(index=['type', 'data'], data=['FAIL_EVENT', str(e)]))
                                    close_serial_resources(recording_status='aborted')
                                    return
                                folder = session_data_root
                                folder.mkdir(parents=True, exist_ok=True)

                                capture_time = datetime.now().strftime('%H-%M-%S-%f')[:-3]
                                if experiment_config:
                                    room_id = str(experiment_config.get('room_id', 'ROOM'))
                                    participant_id = str(experiment_config.get('participant_id', 'PERSON'))
                                else:
                                    room_id = active_room_id
                                    participant_id = active_participant_id
                                csi_target_data_file_name = folder / (
                                    f"{capture_time}_{room_id}_{participant_id}_{action_label}_"
                                    f"{data_series['taget_seq']}.csv"
                                )
                                print(csi_target_data_file_name)
                                csi_target_data_file_fd = open(
                                    csi_target_data_file_name, 'w+', newline='', encoding='utf-8')
                                taget_data_writer = csv.writer(
                                    csi_target_data_file_fd)
                                taget_data_writer.writerow(data_series.index)
                                if ledger is not None:
                                    ledger.begin(csi_target_data_file_name, action_label,
                                                 str(data_series['taget_seq']), current_collect_duration_ms)

                            taget_data_writer.writerow(
                                data_series.astype(str))
                            if ledger is not None:
                                ledger.add_csi(data_series.to_dict(), host_received_ns, time.monotonic_ns())

                        elif taget_last != 'unknown':
                            close_target_recording(status='complete')

                        taget_last = data_series['taget']
                        taget_seq_last = data_series['taget_seq']

                        if queue_read.full():
                            # print('============== queue_full ==========')
                            pass
                        else:
                            # print("data_series", len(data_series), type(data_series), data_series)
                            queue_read.put(data_series)
                    elif data_series['type'] == 'SYSTEM_TELEMETRY':
                        if ledger is not None:
                            ledger.note_telemetry(data_series.to_dict())
                        queue_read.put(data_series)
                    else:
                        if data_series['type'] == 'DEVICE_INFO' and ledger is not None:
                            ledger.note_device_info(data_series.to_dict())
                        queue_read.put(data_series)

                    data_valid_list.at[i, 'file_writer'].writerow(data_series.astype(str))
                    data_valid_list.at[i, 'file_fd'].flush()
                    break
        else:
            strings = re.sub(r'\\x1b.*?m', '', strings)
            log_data_writer.writelines(strings + '\n')

            log = re.match(r'.*([DIWE]) \((\d+)\) (.*)', strings, re.I)

            if not log:
                continue

            data_series = pd.Series(index=['type', 'tag', 'timestamp', 'data'],
                                    data=['LOG_DATA', log.group(1), log.group(2), log.group(3)])
            if not queue_read.full():
                queue_read.put(data_series)


def quit(signum, frame):
    print('Exit the system')
    app = QApplication.instance()
    if app is not None:
        app.quit()
    else:
        sys.exit()


if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print(' Python version should >= 3.6')
        exit()

    parser = argparse.ArgumentParser(
        description='读取串口 CSI 数据并显示图形界面；不指定串口时以离线模式打开界面')
    parser.add_argument('-p', '--port', dest='port', action='store', default=None,
                        help='ESP32 接收端串口，例如 COM3；省略时进入离线界面模式')
    parser.add_argument('-t', '--csi_output_type', dest='csi_output_type', action='store',
                        choices=['LLTF', 'HT_LTF', 'HE_LTF', 'STBC-HT-LTF', 'STBC-HE-LTF'], default='LLTF',
                        help='CSI 输出类型（默认：LLTF）')
    parser.add_argument('--experiment-config', dest='experiment_config', action='store', default=None,
                        help='实验元数据 JSON；启用研究会话清单和单次记录 sidecar')
    parser.add_argument('--data-root', dest='data_root', action='store', default='data',
                        help='采集数据根目录（默认：data）')

    args = parser.parse_args()
    serial_port = args.port
    csi_output_type = args.csi_output_type
    experiment_config = None
    if args.experiment_config:
        try:
            experiment_config = load_experiment_config(args.experiment_config)
            experiment_config['csi_output_type'] = csi_output_type
            experiment_config['collector_sha256'] = sha256_file(Path(__file__).resolve())
        except Exception as e:
            parser.error(str(e))

    serial_queue_read = Queue(maxsize=64)
    serial_queue_write = Queue(maxsize=64)

    signal_key.signal(signal_key.SIGINT, quit)
    signal_key.signal(signal_key.SIGTERM, quit)

    serial_handle_process = Process(target=serial_handle, args=(
        serial_queue_read, serial_queue_write, serial_port, experiment_config, args.data_root))
    serial_handle_process.start()

    app = QApplication(sys.argv)
    available_font_families = set(QFontDatabase().families())
    for font_family in ('Microsoft YaHei UI', 'Microsoft YaHei', 'SimHei'):
        if font_family in available_font_families:
            app.setFont(QFont(font_family, 9))
            break
    app.setWindowIcon(QIcon('../../../docs/_static/icon.png'))

    window = DataGraphicalWindow(serial_queue_write, csi_output_type, experiment_config, args.data_root)
    data_handle_thread = DataHandleThread(serial_queue_read)
    data_handle_thread.signal_device_info.connect(window.show_device_info)
    data_handle_thread.signal_log_msg.connect(window.show_textBrowser_log)
    data_handle_thread.signal_exit.connect(window.close)
    data_handle_thread.start()

    window.show()
    exit_code = app.exec()

    # Ensure a GUI close, Ctrl+C, or serial failure all produce the same
    # recoverable shutdown path. The serial worker marks any active trial as
    # aborted so partial data remain visible to the session validator.
    if serial_handle_process.is_alive():
        serial_queue_write.put('exit')
        serial_handle_process.join(timeout=5)
    if serial_handle_process.is_alive():
        print('WARNING: serial worker did not stop within 5 seconds; terminating it')
        serial_handle_process.terminate()
        serial_handle_process.join(timeout=2)
    serial_queue_read.put(pd.Series(index=['type'], data=['EXIT_EVENT']))
    data_handle_thread.wait(2000)
    sys.exit(exit_code)
