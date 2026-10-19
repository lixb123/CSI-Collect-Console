"""Source identity and timing policy shared by the serial worker and GUI."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from queue import Full
from typing import Any

POLICY = 'target-link-v1'
PHY_FIELDS = ('rate', 'sig_mode', 'mcs', 'cwb', 'channel_primary', 'channel_secondary', 'len', 'stbc')
ESPNOW_SOURCE_MAC = '1a:00:00:00:00:00'


def experiment_hash(value: str) -> int:
    result = 2166136261
    for byte in value.encode('ascii'):
        result = ((result ^ byte) * 16777619) & 0xffffffff
    return result


def acquisition_defaults(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for key, value in {
        'acquisition_gate_enabled': True, 'target_only_recordings': True,
        'minimum_target_rate_hz': 50.0, 'maximum_target_gap_ms': 100.0,
        'mode_settle_seconds': 3.0, 'allow_null_data_collection': False,
        'quality_policy_version': POLICY,
    }.items():
        result.setdefault(key, value)
    return result


def baseline_fingerprint(config: dict[str, Any]) -> str:
    keys = ('participant_id', 'session_id', 'room_id', 'receiver_id', 'sender_id',
            'router_id', 'target_mac', 'send_mode', 'send_interval_ms', 'distance_m',
            'router_position', 'receiver_position', 'sender_position', 'orientation',
            'scenario', 'csi_output_type', 'complex_csi_enabled', 'channel_filter_enabled',
            'csi_scale_shift', 'quality_policy_version', 'minimum_target_rate_hz',
            'maximum_target_gap_ms', 'target_only_recordings')
    payload = {key: config.get(key, '') for key in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def offer_preview(queue: Any, value: Any) -> bool:
    """Never let an optional preview slow the raw writer."""
    try:
        queue.put_nowait(value)
        return True
    except Full:
        return False


@dataclass
class ConnectionIdentity:
    ssid: str = ''
    bssid: str = ''
    channel: int = 0
    connected: bool = False
    mode: str = ''
    settled_since: float = 0.0
    telemetry_at: float = 0.0
    pending_association: bool = False
    association_at: float = 0.0
    target_times: deque = field(default_factory=deque)
    expected_target_mac: str = ''
    research_hash: int = 0
    research_valid: int = 0

    @property
    def source_mac(self) -> str:
        return self.expected_target_mac if self.mode == 'espnow_research' else self.bssid

    def set_expected_target(self, value: str) -> None:
        value = str(value or '').lower()
        if value != self.expected_target_mac:
            self.expected_target_mac = value
            self.target_times.clear()

    def invalidate(self) -> None:
        self.ssid = self.bssid = ''
        self.connected = False
        self.pending_association = False
        self.target_times.clear()
        self.settled_since = time.monotonic()

    def note_log(self, text: str) -> None:
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        match = re.search(r'Connected to (.*?) \(bssid: ([0-9a-fA-F:]{17}), channel: (\d+)\)', text)
        if match:
            self.ssid, self.bssid = match[1], match[2].lower()
            self.channel = int(match[3])
            # Association precedes DHCP/IP readiness. Preserve its verified
            # identity through transitional wifi_connected=0 telemetry, but do
            # not enable preview/capture until the ready telemetry arrives.
            self.connected = False
            self.pending_association = True
            self.association_at = time.monotonic()
            self.target_times.clear()
            self.settled_since = time.monotonic()
        elif re.search(r'(?i)(sta disconnected|wifi.*disconnected|rebooting|rst:0x)', text):
            self.invalidate()

    def note_telemetry(self, row: dict[str, Any]) -> None:
        now = time.monotonic()
        self.telemetry_at = now
        mode = str(row.get('send_mode', ''))
        if mode == 'espnow_research':
            if self.mode != mode:
                self.invalidate()
                self.mode = mode
            self.research_hash = int(row.get('research_rx_experiment_hash', 0))
            self.research_valid = int(row.get('research_rx_valid', 0))
            return
        if self.pending_association and now - self.association_at > 30:
            self.invalidate()
        if str(row.get('wifi_connected', '0')) == '1':
            if self.bssid and self.pending_association:
                self.connected = True
                self.pending_association = False
                self.settled_since = now
        elif not self.pending_association:
            self.invalidate()
        mode = str(row.get('send_mode', ''))
        if mode != self.mode:
            self.mode, self.settled_since = mode, now
            self.target_times.clear()

    def note_csi(self, row: dict[str, Any], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        source_mac = str(row.get('mac', '')).lower()
        expected = self.source_mac
        if (self.connected or self.mode == 'espnow_research') and source_mac == expected:
            self.target_times.append(now)
        while self.target_times and self.target_times[0] < now - 2.0:
            self.target_times.popleft()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        recent = [t for t in self.target_times if t >= now - 2.0]
        return {'ssid': self.ssid, 'bssid': self.bssid, 'channel': self.channel,
                'connected': self.connected, 'mode': self.mode,
                'settled_seconds': now - self.settled_since,
                'telemetry_age_s': now - self.telemetry_at,
                'target_mac': self.source_mac,
                'research_hash': self.research_hash, 'research_valid': self.research_valid,
                'target_rate_hz': len(recent) / 2.0,
                'target_age_ms': (now - recent[-1]) * 1000 if recent else None}


def preflight_error(config: dict[str, Any], link: dict[str, Any]) -> str:
    if not config.get('acquisition_gate_enabled', False):
        return ''
    mode = config.get('send_mode', 'router_ping')
    if mode == 'null_data' and not config.get('allow_null_data_collection', False):
        return 'Null Data 尚未验证目标 AP 的 CSI 接收；正式采集请使用 Router Ping。'
    if mode == 'espnow_research':
        if link.get('mode') != mode:
            return '接收端未启用 ESP-NOW 研究模式；请刷写 RESEARCH_RX_ESPNOW_MODE 固件。'
        if link.get('telemetry_age_s', 99) > 3:
            return '尚未收到 ESP-NOW 接收端遥测；请等待设备状态稳定。'
        target = str(config.get('target_mac', '')).lower()
        if target != ESPNOW_SOURCE_MAC or link.get('target_mac') != target:
            return f'ESP-NOW 固件固定来源为 {ESPNOW_SOURCE_MAC}；请核对目标 MAC。'
        if link.get('settled_seconds', 0) < float(config.get('mode_settle_seconds', 3)):
            return 'ESP-NOW 模式尚未稳定，请等待至少 3 秒。'
        experiment_id = str(config.get('transmitter_experiment_id', config.get('session_id', '')))
        if not experiment_id or link.get('research_hash') != experiment_hash(experiment_id):
            return 'ESP-NOW 实验 ID 不匹配；请在发射端设置当前 session 的 transmitter_experiment_id。'
        if link.get('research_valid', 0) <= 0:
            return '尚未收到协议校验通过的 ESP-NOW 数据。'
        if link.get('target_rate_hz', 0) < float(config.get('minimum_target_rate_hz', 50)):
            return f"ESP-NOW 目标帧率不足：{link.get('target_rate_hz', 0):.1f} Hz。"
        age = link.get('target_age_ms')
        if age is None or age > float(config.get('maximum_target_gap_ms', 100)):
            return 'ESP-NOW 目标数据已间断，请检查发射端、信道和目标 MAC。'
        return ''
    if not link.get('connected') or not link.get('bssid') or link.get('telemetry_age_s', 99) > 3:
        return '尚未核验当前连接 AP；请连接 Wi-Fi 并等待连接信息与新遥测。'
    if str(config.get('target_mac', '')).lower() != link['bssid']:
        return f"目标 MAC 与已连接 AP 不一致；请使用已连接 AP：{link['bssid']}。"
    if link.get('mode') != mode or link.get('settled_seconds', 0) < float(config.get('mode_settle_seconds', 3)):
        return '触发模式尚未稳定，请等待至少 3 秒后再开始。'
    if link.get('target_rate_hz', 0) < float(config.get('minimum_target_rate_hz', 50)):
        return f"目标 AP 有效帧率不足：{link.get('target_rate_hz', 0):.1f} Hz；请检查触发流量。"
    age = link.get('target_age_ms')
    if age is None or age > float(config.get('maximum_target_gap_ms', 100)):
        return '目标 AP 数据已间断，请等待恢复后再开始。'
    return ''


@dataclass
class LinkTiming:
    timestamps: list[float] = field(default_factory=list)
    signatures: list[tuple[str, ...]] = field(default_factory=list)
    clock_errors: int = 0
    missing_timestamps: int = 0
    _previous: int | None = None
    _unwrapped: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        try:
            current = int(row['local_timestamp'])
        except (KeyError, TypeError, ValueError):
            self.missing_timestamps += 1
            return
        if self._previous is None:
            self._unwrapped = float(current)
        else:
            delta = current - self._previous
            if self._previous > 0xF0000000 and current < 0x0FFFFFFF:
                delta += 2**32
            if delta <= 0:
                self.clock_errors += 1
                self._previous = current
                return
            self._unwrapped += delta
        self._previous = current
        self.timestamps.append(self._unwrapped)
        self.signatures.append(tuple(str(row.get(k, '')) for k in PHY_FIELDS))

    def summary(self, duration_s: float, max_gap_ms: float) -> dict[str, Any]:
        count = len(self.timestamps)
        gaps = [(b-a)/1000 for a,b in zip(self.timestamps, self.timestamps[1:])]
        ordered = sorted(gaps)
        dominant = Counter(self.signatures).most_common(1)
        indices = [i for i,s in enumerate(self.signatures) if dominant and s == dominant[0][0]]
        subgaps = [(self.timestamps[j]-self.timestamps[i])/1000 for i,j in zip(indices,indices[1:])]
        return {'target_frame_count': count, 'target_sample_rate_hz': count / duration_s if duration_s > 0 else 0,
                'target_time_coverage_s': (self.timestamps[-1]-self.timestamps[0])/1e6 if count > 1 else 0,
                'target_gap_p95_ms': ordered[min(len(ordered)-1, int(.95*len(ordered)))] if ordered else None,
                'target_gap_max_ms': max(gaps, default=0),
                'target_long_gap_count': sum(g > max_gap_ms for g in gaps),
                'target_clock_error_count': self.clock_errors + self.missing_timestamps,
                'target_dominant_phy_fraction': len(indices)/count if count else 0,
                'target_dominant_phy_gap_max_ms': max(subgaps, default=0),
                'target_dominant_phy_rate_hz': len(indices)/duration_s if duration_s > 0 else 0}
