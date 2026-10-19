from __future__ import annotations
import base64
import csv
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from acquisition_quality import (ConnectionIdentity, LinkTiming, acquisition_defaults,
                                 baseline_fingerprint, offer_preview, preflight_error)
from experiment_capture import ExperimentLedger, session_baseline_status

AP = '44:f9:71:f9:bb:7e'
OTHER = 'c6:b2:5b:d0:ab:39'


def config() -> dict:
    return acquisition_defaults({'participant_id': 'P01', 'session_id': 'S01', 'room_id': 'R01',
        'receiver_id': 'RX01', 'router_id': 'AP01', 'target_mac': AP, 'connected_ap_bssid': AP,
        'send_mode': 'router_ping', 'send_interval_ms': 10, 'calibration_role': 'static_reference',
        'baseline_min_duration_s': 1, 'require_static_baseline': False})


def frame(i: int, mac: str = AP) -> dict:
    return {'seq': i, 'local_timestamp': i*10000, 'mac': mac, 'rssi': -30,
            'rate': 11, 'sig_mode': 1, 'mcs': 7, 'cwb': 1, 'channel_primary': 1,
            'channel_secondary': 1, 'len': 104, 'stbc': 0, 'rx_state': 0,
            'first_word_invalid': 0, 'data': [3,4]*52}


def telemetry(count: int = 0, drops: int = 0) -> dict:
    return {'type': 'SYSTEM_TELEMETRY', 'csi_received': count, 'queue_drops': drops,
            'wifi_connected': 1, 'send_mode': 'router_ping', 'send_interval_ms': 10}


class AcquisitionQualityTests(unittest.TestCase):
    def test_dedicated_telemetry_keeps_frame_window_without_wifi(self):
        from acquisition_quality import ESPNOW_SOURCE_MAC
        link = ConnectionIdentity()
        link.set_expected_target(ESPNOW_SOURCE_MAC)
        row = dict(telemetry(), send_mode='espnow_research', wifi_connected=0,
                   research_rx_valid=300, research_rx_experiment_hash=123)
        link.note_telemetry(row)
        for _ in range(200):
            link.note_csi({'mac': ESPNOW_SOURCE_MAC})
            link.note_csi({'mac': AP})
        link.note_telemetry(row)
        self.assertEqual(link.snapshot()['target_rate_hz'], 100)
        self.assertFalse(link.connected)

    def test_dedicated_recording_does_not_require_ap_but_checks_experiment(self):
        from experiment_capture import research_experiment_hash
        cfg = dict(config(), send_mode='espnow_research', target_mac='1a:00:00:00:00:00')
        with tempfile.TemporaryDirectory() as root:
            ledger = ExperimentLedger(Path(root), cfg)
            row = dict(telemetry(), send_mode='espnow_research', wifi_connected=0,
                       research_rx_experiment_hash=research_experiment_hash('S01'))
            ledger.note_telemetry(row)
            ledger.begin(Path(root)/'test.csv', 'static', '1', 1000)
            ledger.note_telemetry(row)
            self.assertFalse(ledger.active.acquisition_errors)
            ledger.note_telemetry(dict(row, research_rx_experiment_hash=0))
            self.assertIn('transmitter_experiment_mismatch', ledger.active.acquisition_errors)

    def test_scan_is_not_connection_and_disconnect_invalidates_identity(self):
        link = ConnectionIdentity()
        link.note_log(f'Router, ssid: AP, bssid: {AP}, channel: 1, rssi: -25')
        self.assertFalse(link.connected)
        link.note_log(f'I (15) wifi_cmd: Connected to AP (bssid: {AP}, channel: 1)')
        self.assertEqual(link.bssid, AP)
        link.note_telemetry({'wifi_connected': 1, 'send_mode': 'router_ping'})
        self.assertTrue(link.connected)
        link.note_telemetry({'wifi_connected': 0, 'send_mode': 'router_ping'})
        link.note_telemetry({'wifi_connected': 1, 'send_mode': 'router_ping'})
        self.assertFalse(link.connected)  # telemetry alone never invents the BSSID

    def test_association_before_ip_survives_zero_telemetry_and_renders_batch(self):
        import esp_csi_tool as gui
        import pandas as pd
        link = ConnectionIdentity()
        link.note_log(f'I (42969) wifi_cmd: Connected to 2A1511 (bssid: {AP}, channel: 1)')
        for uptime in (42987, 43988):
            link.note_telemetry({'device_uptime_ms': uptime, 'wifi_connected': 0, 'send_mode': 'router_ping'})
            self.assertEqual(link.bssid, AP)
            self.assertFalse(link.connected)
        link.note_telemetry({'device_uptime_ms':44989,'wifi_connected':1,'send_mode':'router_ping'})
        self.assertTrue(link.connected)
        row = {key: 0 for key in gui.CSI_DATA_COLUMNS_NAMES}
        row.update(frame(1), type='CSI_DATA')
        queue = Queue()
        # Follow the actual worker's source gate, batch transport and consumer.
        if link.connected and row['mac'] == link.bssid:
            offer_preview(queue, pd.Series({'type':'CSI_BATCH','data':[row]}))
        queue.put(pd.Series({'type':'EXIT_EVENT'}))
        previous = gui.g_csi_update_counter
        gui.DataHandleThread(queue).run()
        self.assertEqual(gui.g_csi_update_counter, previous+1)
        self.assertEqual(int(gui.g_rssi_array[-1]), -30)
        self.assertTrue((gui.g_csi_amplitude_array[-1] == 5).all())

    def test_pending_identity_expires_or_is_cleared_by_disconnect(self):
        for explicit in (True, False):
            link=ConnectionIdentity()
            link.note_log(f'Connected to AP (bssid: {AP}, channel: 1)')
            if explicit:
                link.note_log('wifi_cmd: sta disconnected, reason=8')
            else:
                link.association_at -= 31
            link.note_telemetry({'wifi_connected':1,'send_mode':'router_ping'})
            self.assertFalse(link.connected)
            self.assertEqual(link.bssid,'')

    def test_wrong_source_null_stale_and_sparse_stream_are_rejected(self):
        state = {'connected': True, 'bssid': AP, 'mode': 'router_ping', 'telemetry_age_s': 0,
                 'settled_seconds': 4, 'target_rate_hz': 100, 'target_age_ms': 10}
        self.assertEqual(preflight_error(config(), state), '')
        for change in ({'bssid': OTHER}, {'telemetry_age_s': 4}, {'target_rate_hz': .4},
                       {'target_age_ms': 101}, {'settled_seconds': 1}, {'mode': 'null_data'}):
            self.assertTrue(preflight_error(config(), dict(state, **change)))
        self.assertTrue(preflight_error(dict(config(), send_mode='null_data'), state))

    def test_espnow_research_requires_dedicated_receiver(self):
        cfg = dict(config(), send_mode='espnow_research', target_mac='1a:00:00:00:00:00',
                   transmitter_experiment_id='S01')
        state = {'connected': False, 'bssid': '', 'mode': 'espnow_research',
                 'telemetry_age_s': .2, 'settled_seconds': 4, 'target_mac': '1a:00:00:00:00:00',
                 'target_rate_hz': 100, 'target_age_ms': 10,
                 'research_hash': 0x123, 'research_valid': 10}
        self.assertIn('实验 ID', preflight_error(cfg, state))
        state['research_hash'] = __import__('acquisition_quality').experiment_hash('S01')
        self.assertEqual(preflight_error(cfg, state), '')

    def test_espnow_wrong_firmware_is_rejected(self):
        cfg = dict(config(), send_mode='espnow_research', target_mac='1a:00:00:00:00:00')
        state = {'connected': True, 'bssid': AP, 'mode': 'router_ping', 'telemetry_age_s': 0,
                 'settled_seconds': 4, 'target_rate_hz': 100, 'target_age_ms': 10}
        self.assertIn('ESP-NOW', preflight_error(cfg, state))

    def test_recent_rate_excludes_other_sources(self):
        link = ConnectionIdentity(bssid=AP, connected=True)
        now = time.monotonic()
        for i in range(200):
            link.note_csi({'mac': OTHER}, now)
        self.assertEqual(link.snapshot()['target_rate_hz'], 0)
        for i in range(200):
            link.note_csi({'mac': AP}, now)
        self.assertEqual(link.snapshot()['target_rate_hz'], 100)

    def test_full_preview_queue_never_blocks(self):
        q = Queue(maxsize=1)
        self.assertTrue(offer_preview(q, 'first'))
        self.assertFalse(offer_preview(q, 'next'))
        self.assertEqual(q.get_nowait(), 'first')

    def test_timestamp_wrap_reset_and_phy_substream_gaps(self):
        t = LinkTiming()
        t.add(dict(frame(0), local_timestamp=2**32-5000))
        t.add(dict(frame(1), local_timestamp=5000))
        self.assertEqual(t.summary(.02, 100)['target_gap_max_ms'], 10)
        t.add(dict(frame(2), local_timestamp=4000))
        self.assertEqual(t.clock_errors, 1)
        t = LinkTiming()
        for i in range(100):
            t.add(dict(frame(i), mcs=6 if 40 <= i < 60 else 7))
        summary = t.summary(1, 100)
        self.assertEqual(summary['target_gap_max_ms'], 10)
        self.assertGreater(summary['target_dominant_phy_gap_max_ms'], 100)

    def make_recording(self, root: Path, cfg: dict | None = None) -> ExperimentLedger:
        ledger = ExperimentLedger(root, cfg or config())
        p = root/'capture.csv';p.write_text('type,seq\n', encoding='utf-8')
        ledger.note_telemetry(telemetry())
        ledger.begin(p, 'static', '1', duration_ms=1000)
        return ledger

    def test_raw_source_exclusion_and_valid_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = self.make_recording(Path(temp))
            for i in range(100):
                self.assertFalse(ledger.add_csi(frame(i, OTHER), i*10**7, i*10**7))
                self.assertTrue(ledger.add_csi(frame(i), i*10**7, i*10**7))
            ledger.note_telemetry(telemetry(200))
            result = ledger.finish()
            self.assertEqual(result['frame_count'], 100)
            self.assertEqual(result['excluded_source_frames'], 100)
            self.assertEqual(result['target_sample_rate_hz'], 100)
            self.assertEqual(result['quality_status'], 'pass')
            self.assertTrue(session_baseline_status(Path(temp), ledger.config)['ready'])
            for change in ({'target_mac': OTHER}, {'send_mode': 'null_data'}, {'distance_m': 4},
                           {'receiver_position': {'x_m': 2}}, {'maximum_target_gap_ms': 50}):
                self.assertFalse(session_baseline_status(Path(temp), dict(ledger.config, **change))['ready'])

    def test_gap_or_partial_duration_cannot_pass_even_if_rate_is_high(self):
        for kind in ('gap', 'partial'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                ledger = self.make_recording(Path(temp))
                for i in range(100):
                    stamp = i*5000 if kind == 'partial' else i*5000+(500000 if i>=50 else 0)
                    ledger.add_csi(dict(frame(i), local_timestamp=stamp), stamp*1000, stamp*1000)
                ledger.note_telemetry(telemetry(100))
                result = ledger.finish()
                self.assertEqual(result['target_sample_rate_hz'], 100)
                self.assertTrue(result['needs_retake'])

    def test_drop_disconnect_and_empty_recordings_fail(self):
        for kind in ('drop', 'disconnect', 'empty'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                ledger = self.make_recording(Path(temp))
                if kind != 'empty':
                    for i in range(100): ledger.add_csi(frame(i),i*10**7,i*10**7)
                row = telemetry(100, 1 if kind=='drop' else 0)
                if kind=='disconnect': row['wifi_connected']=0
                ledger.note_telemetry(row)
                self.assertTrue(ledger.finish()['needs_retake'])

    def test_legacy_baseline_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'session_manifest.csv'
            p.write_text('session_id,calibration_role,status,device_time_span_s,quality_status\nS01,static_reference,complete,60,pass\n')
            self.assertFalse(session_baseline_status(Path(temp),config())['ready'])

    def test_serial_worker_saves_raw_and_selected_when_ui_is_full(self):
        import esp_csi_tool as gui
        with tempfile.TemporaryDirectory() as temp:
            previous = Path.cwd();os.chdir(temp)
            try:
                read_q, write_q = Queue(maxsize=1), Queue()
                read_q.put('UI stalled')
                rows=[]
                for i,mac in enumerate((OTHER,AP,AP,AP)):
                    row={k:0 for k in gui.CSI_DATA_COLUMNS_NAMES}
                    row.update(frame(i,mac),type='CSI_DATA',timestamp=i*10,
                               taget='static' if i<3 else 'unknown',taget_seq=1,
                               data=base64.b64encode(bytes([3,4]*52)).decode())
                    buf=io.StringIO();csv.writer(buf).writerow([row[k] for k in gui.CSI_DATA_COLUMNS_NAMES]);rows.append(buf.getvalue().encode())
                class FakeSerial:
                    is_open=True
                    def __init__(self): self.lines=iter(rows)
                    def flushInput(self): pass
                    def write(self, data): return len(data)
                    def close(self): self.is_open=False
                    def readline(self):
                        line=next(self.lines,None)
                        if line is None: write_q.put('exit');return b''
                        return line
                cfg=dict(config(),acquisition_gate_enabled=False)
                with patch.object(gui.serial,'Serial',return_value=FakeSerial()):
                    gui.serial_handle(read_q,write_q,'FAKE',cfg,str(Path(temp)/'data'))
                raw=next(Path('log').glob('*/csi_data.csv'))
                with raw.open(encoding='utf-8') as handle:
                    raw_rows=list(csv.DictReader(handle))
                self.assertEqual(len(raw_rows),4)
                self.assertEqual(json.loads(raw_rows[0]['data']),[3,4]*52)
                sidecar=next(Path('data').rglob('*.meta.json'))
                meta=json.loads(sidecar.read_text())['recording']
                self.assertEqual(meta['frame_count'],2)
                self.assertEqual(meta['excluded_source_frames'],1)
                with sidecar.with_suffix('').with_suffix('.csv').open(encoding='utf-8') as handle:
                    saved=list(csv.DictReader(handle))
                self.assertEqual({r['mac'] for r in saved},{AP})
            finally:
                os.chdir(previous)

    def test_no_frames_after_start_creates_failed_manifest(self):
        import esp_csi_tool as gui
        import itertools
        with tempfile.TemporaryDirectory() as temp:
            previous = Path.cwd();os.chdir(temp)
            try:
                read_q, write_q = Queue(), Queue()
                write_q.put('radar --collect_number 1 --collect_tagets static --collect_duration 1000')
                class SilentSerial:
                    is_open=True
                    reads=0
                    def flushInput(self): pass
                    def write(self, data): return len(data)
                    def close(self): self.is_open=False
                    def readline(self):
                        self.reads+=1
                        if self.reads>20:write_q.put('exit')
                        return b''
                ticks=itertools.count(0,.5)
                with patch.object(gui.serial,'Serial',return_value=SilentSerial()), patch.object(gui.time,'monotonic',side_effect=lambda:next(ticks)):
                    gui.serial_handle(read_q,write_q,'FAKE',dict(config(),acquisition_gate_enabled=False),str(Path(temp)/'data'))
                sidecar=next(Path('data').rglob('*.meta.json'))
                row=json.loads(sidecar.read_text())['recording']
                self.assertEqual(row['status'],'incomplete')
                self.assertEqual(row['frame_count'],0)
                self.assertTrue(row['needs_retake'])
            finally:
                os.chdir(previous)


if __name__ == '__main__': unittest.main()
