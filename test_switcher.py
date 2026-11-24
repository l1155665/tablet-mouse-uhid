import os
import subprocess
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest
from unittest.mock import MagicMock, Mock, patch

from PySide6 import QtCore as Q, QtWidgets as W
from PySide6.QtTest import QTest

import core
import tablet_mouse_gui as gui
from tablet_mouse_gui import Panel, handle_existing_instance
import win_input as win


S = core.AdbState
USB = core.UsbState
DEVICE = core.UsbProbe(USB.DEVICE, 'ExampleTablet')
TEST_SERIAL = 'USB-TEST-1'
ADB_OWNER = core.native.Owner(42, str(core.ADB), 111)
OTHER_OWNER = core.native.Owner(43, r'C:\Tools\other.exe', 222)
ABSENT = core.AdbProbe(S.ABSENT)
READY = core.AdbProbe(S.READY, (ADB_OWNER,), '0029')
MISMATCH = core.AdbProbe(S.MISMATCH, (ADB_OWNER,), '002a', '0029')


class CoreTests(unittest.TestCase):
    def test_devices_exclude_emulator_and_unauthorized(self):
        text = ('List of devices attached\nemulator-5554 device model:LDPlayer\n'
                'USB-TEST-1 device product:x model:ExampleTablet\nxyz unauthorized\nother offline\n')
        self.assertEqual(core.parse_devices(text), {TEST_SERIAL: 'ExampleTablet'})

    def test_usb_transition_filters_emulator_and_rechecks_device(self):
        listed = 'emulator-5554 device model:LDPlayer\nother device\n'
        answers = [listed, listed + TEST_SERIAL + ' offline',
                   listed + TEST_SERIAL + ' device model:ExampleTablet']
        with (patch('core.adb_query', side_effect=answers + [answers[-1]]) as query,
              patch('core.time.sleep') as delay, patch('core.spawn_adb') as spawn):
            progress = []
            self.assertEqual(core.wait_for_usb(serial=TEST_SERIAL, progress=progress.append), DEVICE)
            self.assertEqual([x.args[0] for x in query.call_args_list], ['host:devices-l'] * 4)
            self.assertEqual(len({x.kwargs['deadline'] for x in query.call_args_list}), 1)
            self.assertEqual(delay.call_count, 3)
            self.assertIn('请连接数据线', progress[0])
            self.assertIn('暂未就绪', progress[1])
            spawn.assert_not_called()

    def test_usb_unauthorized_other_and_probe_error(self):
        with patch('core.adb_query', return_value=TEST_SERIAL + ' unauthorized') as query:
            with self.assertRaisesRegex(core.SwitcherError, '确认 USB 调试授权'):
                core.wait_for_usb(serial=TEST_SERIAL)
            query.assert_called_once()
            query.return_value = TEST_SERIAL + ' recovery'
            self.assertIs(core.probe_usb(serial=TEST_SERIAL).state, USB.OTHER)
            query.side_effect = core.AdbError('read', 'private raw payload')
            result = core.probe_usb(serial=TEST_SERIAL)
            self.assertIs(result.state, USB.PROBE_ERROR)
            self.assertEqual(result.stage, 'read')
            self.assertNotIn('private', result.message)

    def test_usb_budget_and_cancellation_during_io(self):
        clock = [0.]
        attempts = []

        def probe(*_, deadline):
            attempts.append(deadline)
            return core.UsbProbe(USB.OFFLINE)

        with (patch('core.time.monotonic', side_effect=lambda: clock[0]),
              patch('core.time.sleep', side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)),
              patch('core.probe_usb', side_effect=probe)):
            with self.assertRaisesRegex(core.SwitcherError, '超时.*暂未就绪'):
                core.wait_for_usb(serial=TEST_SERIAL)
            self.assertAlmostEqual(clock[0], core.USB_TIMEOUT)
            self.assertEqual(set(attempts), {core.USB_TIMEOUT})
            attempts.clear()
            with self.assertRaisesRegex(core.SwitcherError, '已取消'):
                core.wait_for_usb(serial=TEST_SERIAL, cancelled=lambda: bool(attempts))
            self.assertEqual(len(attempts), 1)

    def test_cancel_during_cold_adb_leaves_spawned_server_alone(self):
        cancel = Event()
        proc = Mock(pid=42)

        def poll():
            cancel.set()
            return None

        proc.poll.side_effect = poll
        with (patch('core.probe_adb', side_effect=[ABSENT, READY]),
              patch('core.native.adb_process_exists', return_value=False),
              patch('core.native.listener_pids', return_value=set()),
              patch('core.adb_client_protocol', return_value='0029'),
              patch('core.spawn_adb', return_value=proc) as spawn):
            with self.assertRaisesRegex(core.SwitcherError, '已取消'):
                core.ensure_adb_ready(cancelled=cancel.is_set)
            spawn.assert_called_once()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_config_utf8_round_trip(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            self.assertEqual(core.load_config(path), core.DEFAULTS)
            cfg = dict(core.DEFAULTS, position=71, keyboard=True, keyboard_mode='sdk')
            core.save_config(path, cfg)
            self.assertEqual(core.load_config(path), cfg)
            self.assertFalse(path.with_suffix('.tmp').exists())
            path.write_text('{"keyboard_mode": "invalid"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'keyboard_mode'):
                core.load_config(path)

    def test_geometry_and_dwell_rearm(self):
        self.assertEqual(core.edge_region((-300, 0, 300, 100), 'right', 100, 35, 12), (-12, 65, 12, 35))
        self.assertEqual(core.edge_region((10, 5, 300, 100), 'left', 0, 35, 1), (10, 5, 1, 35))
        dwell = core.Dwell()
        events = [(True, 0), (True, 1), (False, 2), (True, 3), (True, 3.1),
                  (False, 3.11), (True, 4), (True, 4.13), (True, 5)]
        self.assertEqual([dwell.update(on, t, .12) for on, t in events], [False] * 7 + [True, False])

    def test_capture_window_stays_inside_negative_work_area_and_outside_bar(self):
        info = win.MonitorInfo(area=win.W.RECT(-1920, -120, 0, 960),
                               work=win.W.RECT(-1872, -120, 0, 920))
        with (patch.object(win, 'monitor_info', return_value=info),
              patch.object(win, 'bounds', return_value=(0, 0, 120, 36)),
              patch.object(win, 'z_order', return_value=True) as place,
              patch.object(win, 'monitor', side_effect=lambda pt: 42 if pt[0] == 0 else None)):
            self.assertTrue(win.adjacent_monitor(7, 'right', .5))
            self.assertFalse(win.adjacent_monitor(7, 'left', .5))
            self.assertTrue(win.place_window(101, 7, 'right', 100, 1.5))
            place.assert_called_once_with(101, -1, -153, 884, 0, 0, 0x11)
            initial = win.capture_position(7, 'left', 0, scale=1.25)
            self.assertEqual(initial, (-1872, -120))
            args = core.command(core.DEFAULTS, serial=TEST_SERIAL, scrcpy_path='scrcpy.exe', initial=initial)
            self.assertIn('--window-x=-1872', args)
            self.assertIn('--window-y=-120', args)
            self.assertIn('--background-color=#1C2736', args)

    def test_qt_island_origin_maps_to_native_monitor_at_mixed_dpi(self):
        info = win.MonitorInfo(area=win.W.RECT(-1920, -120, 0, 960))
        with (patch.object(win, 'monitor', return_value=7) as lookup,
              patch.object(win, 'monitor_info', return_value=info)):
            self.assertEqual(win.monitor_for_qt_rect((-1920, -120, 1280, 720), 1.5), 7)
            lookup.assert_called_once_with((-1920, -120))
            self.assertIsNone(win.monitor_for_qt_rect((-1280, -80, 1280, 720), 1.5))

    def test_switcher_and_idle_child_log_rotation_preserve_live_writer(self):
        with TemporaryDirectory() as temp:
            switcher = Path(temp) / 'switcher.log'
            with patch.object(core, 'LOG_LIMIT', 80):
                handler = gui.file_log_handler(switcher)
            try:
                for idx in range(9):
                    record = gui.logging.LogRecord('test', 20, __file__, 1, str(idx) * 65, (), None)
                    handler.emit(record)
            finally:
                handler.close()
            self.assertEqual(len(list(Path(temp).glob('switcher.log*'))), 4)
            self.assertIn('8' * 65, switcher.read_text())
            child = Path(temp) / 'scrcpy.log'
            archive = Path(temp) / 'scrcpy.log.1'
            with child.open('wb') as writer:
                writer.write(b'active'); writer.flush()
                self.assertFalse(core.native.rotate_idle_log(child, 4))
                self.assertFalse(archive.exists())
                writer.write(b' writer')
            self.assertTrue(core.native.rotate_idle_log(child, 4))
            self.assertEqual(archive.read_bytes(), b'active writer')
            self.assertFalse(child.exists())

    def test_protection_is_explicit_in_both_modes(self):
        config = dict(core.DEFAULTS, selected_serial=TEST_SERIAL)
        self.assertIn('--mouse-bind=++++:bhsn', core.command(config, scrcpy_path='scrcpy.exe'))
        no_protection = core.command(dict(config, protect=False, keyboard=True), scrcpy_path='scrcpy.exe')
        self.assertIn('--mouse-bind=bhsn:++++', no_protection)
        self.assertIn('--keyboard=uhid', no_protection)
        sdk_mode = core.command(dict(config, keyboard=True, keyboard_mode='sdk'), scrcpy_path='scrcpy.exe')
        self.assertIn('--keyboard=sdk', sdk_mode)

    def test_open_tablet_keyboard_settings(self):
        with (patch('core.probe_adb', return_value=READY),
              patch('core.adb_client_protocol', return_value='0029'),
              patch('core.scan', return_value={TEST_SERIAL: 'ExampleTablet'}),
              patch('core.subprocess.run') as run):
            run.return_value.returncode = 0
            run.return_value.stdout, run.return_value.stderr = 'Starting: Intent', ''
            msg = core.open_tablet_keyboard_settings(serial=TEST_SERIAL)
            self.assertIn('实体键盘', msg)
            args = run.call_args.args[0]
            self.assertIn('android.settings.HARD_KEYBOARD_SETTINGS', args)
            run.return_value.returncode = 1
            with self.assertRaisesRegex(core.SwitcherError, 'select an ADB device'):
                core.open_tablet_keyboard_settings()

    def test_preflight_refuses_mismatch_and_existing_forward(self):
        with (patch('core.resolve_adb_path', return_value=Path('adb.exe')),
              patch('core.resolve_scrcpy_path', return_value=Path('scrcpy.exe')),
              patch('core.adb_client_protocol', return_value='0029'),
              patch('core.probe_adb', return_value=MISMATCH), patch('core.subprocess.Popen') as popen):
            with self.assertRaisesRegex(core.SwitcherError, '版本不匹配'):
                core.launch(core.DEFAULTS, serial=TEST_SERIAL)
            popen.assert_not_called()
        answers = ['0029', f'{TEST_SERIAL} device model:Same\nother device model:Same']
        other_forward = 'other tcp:1234 tcp:1234'
        with patch('core.adb_query', side_effect=answers + [other_forward, '']) as query:
            self.assertIs(core.preflight(TEST_SERIAL).state, USB.DEVICE)
            self.assertEqual(query.call_args.args, ('reverse:list-forward', TEST_SERIAL))
        with (patch('core.adb_query', side_effect=answers + [f'{TEST_SERIAL} tcp:1234 tcp:1234']),
              self.assertRaisesRegex(core.SwitcherError, '已有 ADB 转发')):
            core.preflight(TEST_SERIAL)
        with (patch('core.adb_query', side_effect=answers + [other_forward, 'UsbFfs tcp:1234 tcp:1234']),
              self.assertRaisesRegex(core.SwitcherError, '反向 ADB 转发')):
            core.preflight(TEST_SERIAL)

    def test_cleanup_reaps_own_process(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            core.stop_process(proc)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stop_never_posts_close_to_foreign_hwnd(self):
        proc = Mock(pid=33)
        proc.poll.return_value = None
        with patch.object(win, 'owns_window', return_value=False) as owns, patch.object(win, 'post') as post:
            core.stop_process(proc, 101)
        owns.assert_called_once_with(101, 33)
        post.assert_not_called()
        proc.terminate.assert_called_once()


class WinInputTests(unittest.TestCase):
    def test_click_revalidates_hit_and_owner_before_and_after_move(self):
        proc = Mock(pid=33)
        proc.poll.return_value = None
        for name, hits, owners, should_move, should_click in (
            ('obstructed initially', [9], None, False, False),
            ('obstructed after move', [101, 9], None, True, False),
            ('owner changed after move', [101], [True, True, True, False], True, False),
            ('verified window', [101, 101], None, True, True),
        ):
            with (self.subTest(name=name),
                  patch.object(win, 'owns_window', return_value=True, side_effect=owners),
                  patch.object(win, 'foreground', return_value=101), patch.object(win, 'held', return_value=False),
                  patch.object(win, 'bounds', return_value=(0, 0, 120, 36)),
                  patch.object(win, 'window_at', side_effect=hits),
                  patch.object(win, 'root_window', side_effect=lambda hit, _: hit),
                  patch.object(win, 'position', side_effect=[(10, 10), (60, 18)]),
                  patch.object(win, 'move', return_value=True) as move,
                  patch.object(win, 'click', return_value=True) as click):
                self.assertEqual(win.click_window(101, proc), (should_click, (60, 18) if should_move else None))
                self.assertEqual(move.called, should_move)
                self.assertEqual(click.called, should_click)

    def test_live_guard_rejects_5037_offscreen_and_missing_authorization(self):
        for port, authorized, qpa in (('5037', '1', 'windows'), ('55038', '1', 'offscreen'),
                                      ('55038', '', 'windows'), ('', '1', 'windows')):
            with (self.subTest(port=port, authorized=authorized, qpa=qpa),
                  patch.dict(os.environ, {'TABLET_SWITCHER_LIVE_PORT': port,
                                          'TABLET_SWITCHER_LIVE_AUTHORIZED': authorized, 'QT_QPA_PLATFORM': qpa}),
                  patch.object(core, 'probe_adb') as probe):
                with self.assertRaisesRegex(SystemExit, 'Live test refused'):
                    live_service()
                probe.assert_not_called()


class AdbTests(unittest.TestCase):
    def setUp(self):
        self.socket = MagicMock()
        self.socket.__enter__.return_value = self.socket
        self.connect = self.enterContext(patch('core.socket.create_connection', return_value=self.socket))
        self.listeners = self.enterContext(patch('core.native.listener_pids', return_value=set()))
        self.owner = self.enterContext(patch('core.native.owner', side_effect=lambda pid: {
            42: ADB_OWNER, 43: OTHER_OWNER, 44: core.native.Owner(44)}[pid]))
        self.process = self.enterContext(patch('core.native.adb_process_exists', return_value=False))

    def test_query_frames_and_partial_reads(self):
        self.socket.recv.side_effect = [b'OKAY', b'OK', b'AY', b'0004', b'00', b'29']
        self.assertEqual(core.adb_query('reverse:list-forward', TEST_SERIAL, port=55038), '0029')
        self.assertEqual([c.args[0] for c in self.socket.sendall.call_args_list],
                         [b'0019host:transport:USB-TEST-1', b'0014reverse:list-forward'])
        self.assertEqual(self.connect.call_args.args[0], ('127.0.0.1', 55038))

    def test_query_phase_errors_localized(self):
        for stage, connect, write, read in (
            ('connect', TimeoutError('timed out'), None, []),
            ('write', None, TimeoutError('timed out'), []),
            ('read', None, None, [b'OKAY', TimeoutError('timed out')]),
            ('read', None, None, [b'OKAY', b'0004', b'00', b'']),
            ('protocol', None, None, [b'HTTP']),
            ('protocol', None, None, [b'OKAY', b'+001']),
            ('rejected', None, None, [b'FAIL', b'0009', b'timed out']),
        ):
            with self.subTest(stage=stage, read=read):
                self.connect.side_effect = connect
                self.socket.sendall.side_effect = write
                self.socket.recv.side_effect = read
                with self.assertRaises(core.AdbError) as err:
                    core.adb_query('host:version')
                self.assertEqual(err.exception.stage, stage)
                self.assertNotIn('timed out', str(err.exception))
                self.assertRegex(str(err.exception), r'[\u4e00-\u9fff]')

    def test_probe_five_states_and_uncertainty(self):
        for pids, connect, reply, processes, state in (
            (set(), ConnectionRefusedError(), [], False, S.ABSENT),
            (set(), TimeoutError(), [], False, S.ABSENT),
            (set(), TimeoutError(), [], True, S.BUSY_UNKNOWN),
            (set(), PermissionError(), [], False, S.BUSY_UNKNOWN),
            ({42}, None, [b'OKAY', b'0004', b'0029'], True, S.READY),
            ({42}, None, [b'OKAY', b'0004', b'002a'], True, S.MISMATCH),
            ({42}, None, [TimeoutError()], True, S.UNRESPONSIVE),
            ({42}, None, [b'OKAY', b'0004', b'nope'], True, S.BUSY_UNKNOWN),
            ({43}, None, [b'HTTP'], False, S.BUSY_UNKNOWN),
            ({44}, None, [TimeoutError()], True, S.BUSY_UNKNOWN),
            ({42, 43}, None, [b'OKAY', b'0004', b'0029'], True, S.BUSY_UNKNOWN),
        ):
            with self.subTest(pids=pids, answer=reply, state=state):
                self.listeners.return_value = pids
                self.connect.side_effect = connect
                self.socket.recv.side_effect = reply
                self.process.return_value = processes
                with patch('core.native.peer_pid', return_value=42):
                    status = core.probe_adb(expected='0029')
                self.assertIs(status.state, state)
                self.assertEqual({o.pid for o in status.owners}, pids)
        self.listeners.side_effect = [set(), {43}]
        self.connect.side_effect = TimeoutError()
        self.assertEqual(core.probe_adb().occupant, 'other.exe（PID 43）')
        self.listeners.side_effect = OSError('snapshot failed')
        with self.assertLogs('core', level='WARNING'):
            self.assertIs(core.probe_adb().state, S.BUSY_UNKNOWN)

    def test_spawn_uses_only_direct_server_and_clean_environment(self):
        with (TemporaryDirectory() as temp, patch('core.HOME', Path(temp)),
              patch('core.subprocess.Popen') as popen,
              patch.dict('core.os.environ', {'ADB_SERVER_SOCKET': 'wrong', 'ANDROID_ADB_SERVER_PORT': '0',
                                             'ANDROID_ADB_SERVER_ADDRESS': 'wrong'})):
            self.assertIs(core.spawn_adb(55038, adb_path=core.ADB), popen.return_value)
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(core.ADB), '-L', 'tcp:55038', 'server', 'nodaemon'])
            self.assertEqual(kwargs['cwd'], core.ADB.parent)
            self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
            self.assertFalse(kwargs['env'].keys() & {'ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS',
                                                    'ANDROID_ADB_SERVER_PORT'})

    def test_cold_start_and_compatible_reuse_after_nonzero(self):
        for code in (None, 1):
            with (self.subTest(code=code), patch('core.probe_adb', side_effect=[ABSENT, READY]),
                  patch('core.adb_client_protocol', return_value='0029'), patch('core.spawn_adb') as spawn):
                spawn.return_value.poll.return_value = code
                progress = Mock()
                self.assertIs(core.ensure_adb_ready(55038, adb_path=core.ADB, progress=progress), READY)
                spawn.assert_called_once_with(55038, adb_path=core.ADB)
                progress.assert_called_once_with('正在唤起 ADB…')
                spawn.return_value.terminate.assert_not_called()

    def test_cold_start_budget_exhaustion_does_not_kill_server(self):
        clock = [100.0]

        def sleep(delay):
            clock[0] += delay

        with (patch('core.probe_adb', return_value=ABSENT) as probe, patch('core.spawn_adb') as spawn,
              patch('core.adb_client_protocol', return_value='0029'),
              patch('core.time.monotonic', side_effect=lambda: clock[0]),
              patch('core.time.sleep', side_effect=sleep)):
            spawn.return_value.poll.return_value = None
            with self.assertRaisesRegex(OSError, '超时'):
                core.ensure_adb_ready()
            spawn.assert_called_once()
            self.assertEqual(clock[0], 108.0)
            self.assertTrue(all(c.kwargs['deadline'] == 108.0 for c in probe.call_args_list))
            spawn.return_value.terminate.assert_not_called()
            spawn.return_value.kill.assert_not_called()

    def test_unsafe_or_reused_listener_never_spawns(self):
        for status in (MISMATCH, core.AdbProbe(S.UNRESPONSIVE, (ADB_OWNER,)),
                       core.AdbProbe(S.BUSY_UNKNOWN, (OTHER_OWNER,)), READY):
            with (self.subTest(status=status), patch('core.probe_adb', return_value=status),
                  patch('core.adb_client_protocol', return_value='0029'),
                  patch('core.spawn_adb') as spawn):
                if status is READY:
                    self.assertIs(core.ensure_adb_ready(), READY)
                else:
                    with self.assertRaisesRegex(OSError, status.state.value):
                        core.ensure_adb_ready()
                spawn.assert_not_called()

    def test_second_check_blocks_new_occupant(self):
        self.listeners.return_value = {43}
        busy = core.AdbProbe(S.BUSY_UNKNOWN, (OTHER_OWNER,))
        with (patch('core.probe_adb', side_effect=[ABSENT, busy]), patch('core.spawn_adb') as spawn,
              patch('core.adb_client_protocol', return_value='0029'),
              self.assertRaisesRegex(OSError, '复核.*占用')):
            core.ensure_adb_ready()
        spawn.assert_not_called()

    def test_reset_graceful_or_verified_handle_fallback(self):
        self.listeners.return_value = {42}
        for graceful in (True, False):
            with (self.subTest(graceful=graceful), patch('core.probe_adb', return_value=MISMATCH),
                  patch('core.adb_client_protocol', return_value='0029'),
                  patch('core.native.process_handle') as handle, patch('core.native.identify', return_value=ADB_OWNER),
                  patch('core.native.peer_pid', return_value=42), patch('core.native.terminate') as terminate,
                  patch('core.wait_adb_release', side_effect=[graceful, True]),
                  patch('core.ensure_adb_ready', return_value=READY) as ensure):
                self.socket.reset_mock()
                self.assertIs(core.reset_to_leidian_adb(ADB_OWNER, 55038), READY)
                self.socket.sendall.assert_called_once_with(b'0009host:kill')
                if graceful:
                    terminate.assert_not_called()
                else:
                    handle.assert_any_call(42, allow_stop=True)
                    terminate.assert_called_once()
                ensure.assert_called_once()

    def test_reset_refuses_unknown_or_changed_owner(self):
        other = core.AdbProbe(S.BUSY_UNKNOWN, (OTHER_OWNER,))
        for status, confirmed in ((other, OTHER_OWNER), (MISMATCH, core.native.Owner(42, str(core.ADB), 99)),
                                  (READY, ADB_OWNER)):
            with (self.subTest(status=status), patch('core.probe_adb', return_value=status),
                  patch('core.native.process_handle') as handle, patch('core.ensure_adb_ready') as ensure,
                  self.assertRaisesRegex(OSError, '未关闭任何程序')):
                core.reset_to_leidian_adb(confirmed)
            handle.assert_not_called()
            ensure.assert_not_called()
        self.connect.assert_not_called()

    def test_graceful_reset_never_sends_kill_to_replaced_peer(self):
        self.listeners.return_value = {42}
        with patch('core.native.peer_pid', return_value=43):
            core.request_adb_shutdown(55038, ADB_OWNER)
        self.socket.sendall.assert_not_called()


class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.probe = self.enterContext(patch('core.probe_adb', return_value=ABSENT))
        self.scan = self.enterContext(patch('core.scan', return_value=[
            core.DeviceCandidate(TEST_SERIAL, 'device', 'usb', '测试平板')]))
        self.wait_usb = self.enterContext(patch('core.wait_for_usb', return_value=DEVICE))
        self.ensure = self.enterContext(patch('core.ensure_adb_ready', return_value=READY))
        self.config_path = Path(self.enterContext(TemporaryDirectory())) / 'config.json'
        self.enterContext(patch.object(gui, 'CONFIG', self.config_path))
        self.fg, self.clip, self.shown = 5, (0, 0, 1920, 1080), True
        self.box = (400, 200, 520, 236)
        self.clipped = (460, 218, 461, 219)
        self.stop = self.enterContext(patch('core.stop_process'))
        self.owns = self.enterContext(patch.object(win, 'owns_window', side_effect=lambda h, p: h == 101 and p == 33))
        self.show = self.enterContext(patch.object(win, 'show', side_effect=self.show_window))
        self.enterContext(patch.object(win, 'find_window', return_value=101))
        self.enterContext(patch.object(win, 'visible', side_effect=lambda _: self.shown))
        self.enterContext(patch.object(win, 'foreground', side_effect=lambda: self.fg))
        self.enterContext(patch.object(win, 'bounds', return_value=self.box))
        self.enterContext(patch.object(win, 'clip_bounds', side_effect=lambda: self.clip))
        self.enterContext(patch.object(win, 'position', return_value=(100, 100)))
        self.monitor = self.enterContext(patch.object(win, 'monitor', return_value=7))
        self.enterContext(patch.object(win, 'monitor_for_qt_rect', return_value=7))
        self.enterContext(patch.object(win, 'capture_position', return_value=(1800, 510)))
        self.enterContext(patch.object(win, 'desktop_bounds', return_value=(0, 0, 1920, 1080)))
        self.move = self.enterContext(patch.object(win, 'move', return_value=True))
        self.focus = self.enterContext(patch.object(win, 'focus', side_effect=self.focus_window))
        self.place = self.enterContext(patch.object(win, 'place_window', return_value=True))
        self.tap = self.enterContext(patch.object(win, 'tap'))
        self.click = self.enterContext(patch.object(win, 'click_window', return_value=(True, (460, 218))))
        self.keys = self.enterContext(patch.object(win, 'key_down', return_value=0))
        self.register = self.enterContext(patch.object(win, 'register', return_value=True))
        self.unregister = self.enterContext(patch.object(win, 'unregister', return_value=True))
        self.enterContext(patch.object(win, 'session_notifications', return_value=True))

    def show_window(self, _, command):
        self.shown = bool(command)
        if not command:
            self.clip = (0, 0, 1920, 1080)
            if self.fg == 101:
                self.fg = 5
        return True

    def focus_window(self, hwnd):
        self.fg = hwnd
        return True

    def make_panel(self, *, smoke=True, config=None, **kwargs):
        core.save_config(self.config_path, core.DEFAULTS | {'selected_serial': TEST_SERIAL} | (config or {}))
        return self.panel_from_disk(smoke=smoke, **kwargs)

    def panel_from_disk(self, *, smoke=True, **kwargs):
        panel = Panel(smoke=smoke, **kwargs)
        panel.timer.stop()
        panel.scan_timer.stop()
        self.addCleanup(self.dispose_panel, panel)
        self.finish_scan(panel)
        return panel

    @staticmethod
    def dispose_panel(panel):
        panel.cleanup()
        panel.executor.shutdown(wait=True)
        panel.deleteLater()

    def finish_scan(self, panel):
        while panel.scan_task:
            panel.scan_task.result(timeout=2)
            panel.tick()

    def make_proc(self):
        proc = Mock(pid=33)
        proc.poll.return_value = None
        return proc

    def start_ready(self, panel):
        proc = self.make_proc()
        with patch('core.launch', return_value=proc):
            self.shown = True
            panel.toggle_service()
            self.assertEqual(panel.phase, 'starting')
            panel.operation_task.result(timeout=2)
            panel.tick()
            panel.tick()
        self.assertEqual(panel.phase, 'ready')
        self.finish_scan(panel)
        return proc

    def activate_capture(self, panel):
        panel.switch_in()
        self.fg, self.clip = 101, self.clipped
        panel.tick()
        panel.clip_since -= .1
        panel.tick()
        self.assertEqual(panel.phase, 'active')

    def test_config_fixture_is_isolated_and_frozen(self):
        panel = self.make_panel(smoke=False, config={'keyboard': True, 'keyboard_mode': 'sdk'})
        self.start_ready(panel)
        self.assertEqual(panel.applied_config['keyboard_mode'], 'sdk')
        self.assertNotEqual(self.config_path.parent, core.HOME)
        panel.change('keyboard_mode', 'uhid')
        self.assertEqual(core.load_config(self.config_path)['keyboard_mode'], 'uhid')
        self.assertEqual(panel.applied_config['keyboard_mode'], 'sdk')

    def test_old_config_migration_and_damaged_original_backup(self):
        old = '{"keyboard":true,"keyboard_mode":"sdk","side":"left","key":"F9","dwell":1000}'
        self.config_path.write_text(old, encoding='utf-8')
        migrated = core.load_config(self.config_path)
        self.assertEqual(tuple(migrated[k] for k in ('keyboard_mode', 'side', 'key', 'dwell', 'screen')),
                         ('sdk', 'left', 'F9', 1000, ''))
        backup = self.config_path.with_suffix('.json.bak')
        original = b'{broken\xff'
        self.config_path.write_bytes(original)
        panel = self.panel_from_disk(smoke=False)
        self.assertIn('设置读取失败', panel.message.full_text)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertFalse(backup.exists())
        panel.change('side', 'left')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(core.load_config(self.config_path)['side'], 'left')
        self.assertFalse(panel.config_error)
        self.config_path.write_bytes(b'{broken again')
        core.save_config(self.config_path, migrated, backup_invalid=True)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(core.load_config(self.config_path), migrated)
        for bad in ({'side': 'up'}, {'position': 101}, {'span': 4}, {'dwell': True}, {'key': ''}, {'screen': '\n'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.validate_config(bad)

    def test_panel_primary_footprint_and_keyboard_settings_gate(self):
        self.probe.return_value = READY
        panel = self.make_panel(config={'keyboard': True, 'keyboard_mode': 'uhid'})
        panel.show()
        self.app.processEvents()
        self.assertLessEqual(panel.height(), 480)
        self.assertTrue(panel.kb_settings_btn.isEnabled())
        footer = panel.message.geometry()
        height = panel.height()
        for phase, label in [('off', '启动服务'), ('ready', '切入平板'), ('active', '回电脑')]:
            panel.set_phase(phase)
            panel.note = '很长的错误\n' * 50
            panel.render()
            self.app.processEvents()
            primary = panel.findChildren(W.QPushButton, 'primary')
            self.assertEqual([b.text() for b in primary if b.isVisible()], [label])
            self.assertEqual(panel.message.geometry(), footer)
            self.assertEqual(panel.height(), height)
            self.assertNotIn('\n', panel.message.text())
            self.assertTrue(panel.message.text().endswith('…'))
        self.assertFalse(panel.kb_settings_btn.isEnabled())
        panel.set_phase('off')
        panel.render()
        panel.change('keyboard_mode', 'sdk')
        self.assertFalse(panel.kb_settings_btn.isEnabled())

    def test_monitor_change_rearms_dwell_and_seam_is_not_a_trigger(self):
        panel = self.make_panel(config={'side': 'right', 'auto': True})
        edge = gui.Q.QRect(*panel.region(1)).center()
        with (patch.object(panel, 'native_screen', return_value=(7, 1.5)),
              patch.object(win, 'adjacent_monitor', side_effect=[True, False]) as seam):
            self.assertFalse(panel.auto_edge(edge))
            self.assertTrue(panel.auto_edge(edge))
            self.assertEqual(seam.call_args.args[:2], (7, 'right'))
        panel.dwell.update(False, 0, .1)
        panel.change('screen', 'removed screen')
        self.assertTrue(panel.dwell.latched)
        panel.refresh_screens()
        self.assertEqual(panel.selected_screen(), self.app.primaryScreen())
        self.assertIn('暂用主屏', panel.screen_note)

    def test_usb_cancel_and_retry_do_not_start_scrcpy_early(self):
        panel = self.make_panel()
        entered, released = Event(), Event()

        def wait(*, serial, progress, cancelled, deadline):
            self.assertEqual(serial, TEST_SERIAL)
            progress('平板暂未就绪…')
            entered.set()
            if not released.wait(2):
                raise AssertionError('test did not release USB wait')
            core.check_cancelled(cancelled)
            return DEVICE

        self.wait_usb.side_effect = wait
        with patch('core.launch', return_value=self.make_proc()) as launch:
            panel.toggle_service()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(panel.stop_button.isEnabled())
                launch.assert_not_called()
                panel.stop_or_cancel()
            finally:
                released.set()
            with self.assertRaisesRegex(core.SwitcherError, '已取消'):
                panel.operation_task.result(timeout=2)
            panel.tick()
            self.assertEqual(panel.phase, 'off')
            self.assertIn('已取消', panel.message.full_text)
            launch.assert_not_called()
            self.wait_usb.side_effect = None
            panel.toggle_service()
            panel.operation_task.result(timeout=2)
            panel.tick()
            launch.assert_called_once()

    def test_expected_operation_failure_logs_one_line_and_repeat_count(self):
        panel = self.make_panel()
        with self.assertLogs(level='INFO') as logs:
            for _ in range(3):
                panel.set_phase('starting')
                task = Future()
                task.set_exception(core.AdbError('read', 'private raw payload'))
                panel.operation_task, panel.operation_kind = task, 'start'
                panel.operation_generation = panel.generation
                panel.operation_tick()
                self.finish_scan(panel)
            panel.failures.flush(float('inf'))
        self.assertEqual(len(logs.output), 2)
        self.assertIn('operation=start stage=read state=absent kind=expected', logs.output[0])
        self.assertIn('suppressed=2', logs.output[1])
        self.assertNotIn('Traceback', '\n'.join(logs.output))
        self.assertNotIn('private raw payload', '\n'.join(logs.output))

    def test_capture_transitions_and_close_to_tray(self):
        panel = self.make_panel()
        panel.show()
        proc = self.start_ready(panel)
        self.assertTrue(panel.bar.isVisible())
        panel.bar.clicked.emit()
        self.assertEqual(panel.phase, 'switching')
        panel.tick()
        self.place.assert_called_once()
        panel.tick()
        self.assertEqual(panel.stage, 'focused')
        panel.focus_time -= .06
        panel.tick()
        self.click.assert_called_once_with(101, proc, (0x78,))
        self.assertEqual(panel.stage, 'clicked')
        self.clip = self.clipped
        panel.tick()
        panel.clip_since -= .1
        panel.tick()
        self.assertEqual(panel.phase, 'active')
        self.assertFalse(panel.bar.isVisible())
        panel.tray_enter.trigger()
        self.assertEqual(panel.phase, 'ready')
        self.show.assert_called_with(101, 0)
        self.tap.assert_not_called()
        with patch.object(panel.tray, 'isSystemTrayAvailable', return_value=True):
            panel.close()
        self.assertFalse(panel.isVisible())
        self.assertTrue(panel.tray.isVisible())

    def test_focus_or_click_failure_does_not_retry_or_tap_alt(self):
        panel = self.make_panel()
        self.start_ready(panel)
        for operation in ('place_window', 'show', 'focus', 'click_window'):
            with self.subTest(operation=operation):
                self.fg = 5
                panel.switch_in()
                if operation not in ('place_window', 'show'):
                    panel.tick()
                if operation == 'click_window':
                    panel.tick()
                    panel.focus_time -= .1
                original = getattr(win, operation)
                failed = (False, None) if operation == 'click_window' else False
                with patch.object(win, operation, side_effect=lambda *args: (
                        original(*args) if operation == 'show' and args[1] == 0 else failed)) as failure:
                    panel.tick()
                    self.assertIn('人工点击悬浮条', panel.note)
                    self.assertNotEqual(getattr(panel, 'stage'), 'clicked')
                    self.assertGreaterEqual(failure.call_count, 1)
                self.assertEqual(panel.phase, 'ready')
        self.tap.assert_not_called()

    def test_runtime_settings_coalesce_with_frozen_config(self):
        self.probe.return_value = READY
        for before, mode in ((False, 'sdk'), (True, 'uhid')):
            with self.subTest(keyboard=before, keyboard_mode=mode):
                self.ensure.reset_mock()
                self.stop.reset_mock()
                panel = self.make_panel(config={'keyboard': before, 'keyboard_mode': mode, 'protect': True})
                proc1, proc2 = self.start_ready(panel), self.make_proc()
                self.activate_capture(panel)
                panel.keyboard.setChecked(not before)
                stale = panel.generation, panel.reapply_revision
                new_mode = 'uhid' if mode == 'sdk' else 'sdk'
                index = panel.keyboard_mode.findData(new_mode)
                panel.keyboard_mode.setCurrentIndex(index)
                panel.keyboard_mode.activated.emit(index)
                panel.apply_runtime_settings(*stale)
                self.assertIs(panel.proc, proc1)
                self.assertEqual(panel.applied_config['keyboard'], before)
                with patch('core.launch', return_value=proc2) as launch:
                    panel.apply_runtime_settings(panel.generation, panel.reapply_revision)
                    self.assertEqual(panel.phase, 'parking')
                    self.assertFalse(self.shown)
                    panel.protect.setChecked(False)  # folded in while worker is still being reaped
                    panel.change('side', 'left')  # geometry-only change must survive the same reapply
                    panel.operation_task.result(timeout=2)
                    panel.tick()
                    panel.operation_task.result(timeout=2)
                    panel.tick()
                    panel.tick()
                    self.assertIs(panel.proc, proc2)
                    launch.assert_called_once()
                    self.assertEqual(launch.call_args.args[0]['side'], 'left')
                    self.assertEqual([panel.applied_config[k] for k in gui.RUNTIME_KEYS],
                                     [not before, new_mode, 'uhid', False])
                    self.ensure.assert_called_once()  # only the original explicit start can cold-start ADB
                    self.stop.assert_called_once_with(proc1, 101)
                self.dispose_panel(panel)

    def test_active_exit_reasons_and_no_focus_reclaim(self):
        panel = self.make_panel()
        self.start_ready(panel)
        for fg, clipped, alt, reason in (
            (None, True, False, 'foreground_null'), (6, True, False, 'foreground_other'),
            (101, False, False, 'focus_same_clip_lost'), (101, False, True, 'alt_return'),
        ):
            with self.subTest(reason=reason):
                self.fg = 5
                self.activate_capture(panel)
                self.fg, self.clip = fg, self.clipped if clipped else (0, 0, 1920, 1080)
                panel.alt_down = alt
                with self.assertLogs(level='INFO') as log:
                    panel.tick()
                self.assertTrue(any(f'reason_code={reason}' in line and 'clip_in=' in line for line in log.output))
                self.assertEqual(panel.phase, 'ready')
                self.focus.assert_not_called()
                self.move.assert_not_called()

    def test_invalid_hwnd_and_expired_capture_deadline(self):
        panel = self.make_panel()
        proc = self.start_ready(panel)
        panel.switch_in()
        self.fg, self.clip = 101, self.clipped
        panel.clip_since = time.monotonic() - .2
        panel.deadline = time.monotonic() - .1
        with self.assertLogs(level='INFO') as log:
            panel.tick()
        self.assertIn('reason_code=timeout', '\n'.join(log.output))
        self.assertEqual(panel.phase, 'ready')
        self.activate_capture(panel)
        self.owns.return_value = False
        self.owns.side_effect = None
        panel.tick()
        panel.operation_task.result(timeout=2)
        panel.tick()
        self.assertEqual(panel.phase, 'off')
        self.stop.assert_called_once_with(proc, 101)
        self.tap.assert_not_called()

    def test_park_waits_for_release_and_ignores_duplicate_requests(self):
        panel = self.make_panel()
        self.start_ready(panel)
        self.activate_capture(panel)
        self.fg, self.shown = 6, True
        with patch.object(win, 'show', return_value=True) as show:
            panel.tick()
            self.assertEqual(panel.phase, 'parking')
            self.assertFalse(panel.bar.isVisible())
            panel.park('lock_or_session')
            show.assert_called_once_with(101, 0)
            self.shown = False
            panel.tick()
            self.assertEqual(panel.phase, 'parking')  # still clipped to the SDL window
            self.clip = win.desktop_bounds()
            panel.tick()
            self.assertEqual(panel.phase, 'ready')
        self.focus.assert_not_called()
        self.move.assert_not_called()

    def test_confirmed_disconnect_stops_only_owned_process(self):
        self.probe.return_value = READY
        panel = self.make_panel()
        proc = self.start_ready(panel)
        self.activate_capture(panel)
        self.scan.return_value = [core.DeviceCandidate('OTHER-DEVICE', 'device', 'tcpip')]
        panel.request_scan()
        self.finish_scan(panel)
        self.assertEqual(panel.phase, 'active')
        panel.request_scan()
        with self.assertLogs(level='INFO') as log:
            self.finish_scan(panel)
        self.assertIn('reason_code=disconnect', '\n'.join(log.output))
        self.assertFalse(self.shown)
        # finish_scan() ticks the panel; a fast cleanup may already be consumed.
        if panel.operation_task is not None:
            panel.operation_task.result(timeout=2)
            panel.tick()
        self.stop.assert_called_once_with(proc, 101)
        self.assertEqual(panel.phase, 'off')
        self.assertEqual(panel.target_serial, TEST_SERIAL)

    def test_wireless_selection_is_pinned_and_aoa_is_not_silently_changed(self):
        wireless = '192.0.2.10:5555'
        self.probe.return_value = READY
        self.scan.return_value += [core.DeviceCandidate(wireless, 'device', 'tcpip', '测试平板')]
        panel = self.make_panel(config={'keyboard': True, 'keyboard_mode': 'aoa'})
        panel.select_device(panel.device_picker.findData(wireless))
        self.finish_scan(panel)
        self.assertEqual(panel.device_picker.currentData(), wireless)
        self.assertEqual(panel.version_badge.text(), 'v2.3 · 无线')
        self.assertEqual(panel.keyboard_mode.currentIndex(), -1)
        self.assertEqual(panel.config['keyboard_mode'], 'aoa')
        for combo in (panel.keyboard_mode, panel.mouse_mode):
            self.assertFalse(combo.model().item(combo.findData('aoa')).isEnabled())
        panel.set_phase('starting')
        panel.render()
        self.assertFalse(panel.device_picker.isEnabled())
        panel.select_device(panel.device_picker.findData(TEST_SERIAL))
        self.assertEqual(panel.target_serial, wireless)

    def test_uhid_exit_does_not_change_settings_until_explicit_sdk_action(self):
        self.probe.return_value = READY
        panel = self.make_panel(config={'keyboard': True, 'keyboard_mode': 'uhid'})
        proc = self.start_ready(panel)
        proc.poll.return_value = proc.returncode = 1
        with patch('core.session_log_tail', return_value='UHID: permission denied'):
            panel.tick()
        panel.operation_task.result(timeout=2)
        panel.tick()
        self.assertEqual(panel.uhid_reason.full_text, 'UHID 失败：权限受限')
        self.assertEqual(panel.config['keyboard_mode'], 'uhid')
        self.assertEqual(panel.config['mouse_mode'], 'uhid')
        panel.use_sdk_btn.click()
        self.assertEqual(panel.config['keyboard_mode'], 'sdk')
        self.assertEqual(panel.config['mouse_mode'], 'sdk')
        self.assertTrue(panel.uhid_reason.isHidden())

    def test_hotkey_stages_release_and_stale_messages(self):
        panel = self.make_panel(smoke=False, config={'key': 'Ctrl+Shift+M', 'return_key': False})
        self.register.assert_not_called()
        self.start_ready(panel)
        mods, vk = gui.KEYS['Ctrl+Shift+M']
        packed = (vk << 16) | mods
        first = panel.hot_binding[0]
        self.assertFalse(panel.handle_hotkey(first, packed ^ 2))
        self.keys.side_effect = lambda key: -1 if key in (0x11, vk) else 0
        self.assertTrue(panel.handle_hotkey(first, packed))
        self.assertFalse(panel.handle_hotkey(first, packed))
        self.assertIsNone(panel.hot_binding)
        self.fg, self.clip = 101, self.clipped
        panel.tick()
        self.assertIsNone(panel.clip_since)
        self.keys.side_effect = lambda key: -1 if key == 0x11 else 0
        panel.tick()
        self.assertIsNone(panel.clip_since)
        self.keys.side_effect = None
        panel.tick()
        panel.clip_since -= .1
        panel.tick()
        self.assertEqual(panel.phase, 'active')
        self.assertIsNone(panel.hot_binding)
        panel.change('return_key', True)
        second = panel.hot_binding[0]
        self.assertNotEqual(first, second)
        self.assertFalse(panel.handle_hotkey(first, packed))
        self.keys.side_effect = lambda key: -1 if key == vk else 0
        self.assertTrue(panel.handle_hotkey(second, packed))
        self.assertEqual(panel.phase, 'ready')
        panel.tick()
        self.assertIsNone(panel.hot_binding)
        self.keys.side_effect = None
        panel.tick()
        self.assertNotIn(panel.hot_binding[0], (first, second))
        panel.session_change(True)
        self.assertIsNone(panel.hot_binding)
        self.assertFalse(panel.bar.isVisible())

    def test_stale_timer_start_result_and_scan(self):
        panel = self.make_panel()
        proc = self.make_proc()
        task = Future()
        task.set_result(proc)
        panel.operation_task, panel.operation_kind = task, 'start'
        panel.operation_generation = panel.generation - 1
        panel.tick()
        panel.executor.submit(lambda: None).result(timeout=2)
        self.stop.assert_called_once_with(proc, 101)
        self.assertIsNone(panel.proc)
        task = Future()
        task.set_result((READY, core.UsbProbe(USB.DEVICE, 'stale scan')))
        panel.scan_task, panel.scan_generation = task, panel.generation - 1
        panel.tick()
        self.finish_scan(panel)
        self.assertNotEqual(panel.device.text(), 'stale scan')
        self.start_ready(panel)
        old = panel.generation
        self.activate_capture(panel)
        panel.park('return_hotkey')
        panel.entry_clip = self.clip
        panel.restore_pointer(old, 5, (2, 2), (100, 100), 7, 5)
        panel.restore_pointer(panel.generation, 5, (2, 2), (101, 100), 7, 5)
        self.move.assert_not_called()
        panel.restore_pointer(panel.generation, 5, (2, 2), (100, 100), 7, 5)
        self.move.assert_called_once_with(2, 2)

    def test_stop_releases_first_and_worker_failure_is_retryable(self):
        panel = self.make_panel()
        proc = self.start_ready(panel)
        self.activate_capture(panel)
        entered, release = Event(), Event()
        worker_calls = []

        def fail(*args):
            worker_calls.append((get_ident(), self.shown))
            entered.set()
            release.wait(2)
            raise OSError('test reaper exception')

        self.stop.side_effect = fail
        try:
            panel.stop()
            task = panel.operation_task
            self.assertTrue(entered.wait(2))
            self.assertEqual(worker_calls, [(worker_calls[0][0], False)])
            self.assertNotEqual(worker_calls[0][0], get_ident())
            self.assertFalse(task.done())
            panel.tick()
            panel.stop()
            self.stop.assert_called_once_with(proc, 101)
        finally:
            release.set()
        with self.assertRaises(OSError):
            task.result(timeout=2)
        with self.assertLogs(level='ERROR'):
            panel.tick()
        self.assertEqual(panel.phase, 'off')
        self.assertIs(panel.proc, proc)
        self.assertIn('回收未确认', panel.note)
        self.stop.side_effect = None
        panel.stop()
        panel.operation_task.result(timeout=2)
        panel.tick()
        self.assertIsNone(panel.proc)

    def test_capture_exception_hides_before_reap(self):
        panel = self.make_panel()
        proc = self.start_ready(panel)
        self.activate_capture(panel)
        with patch.object(win, 'clip_bounds', side_effect=[OSError('test snapshot exception'), self.clip]):
            with self.assertLogs(level='ERROR'):
                panel.tick()
        self.assertFalse(self.shown)
        panel.operation_task.result(timeout=2)
        panel.tick()
        self.stop.assert_called_once_with(proc, 101)
        self.assertEqual(panel.phase, 'off')

    def test_handle_existing_instance_replace_and_show(self):
        lock = Mock()
        lock.tryLock.side_effect = [False, True]
        lock.getLockInfo.return_value = (999, 'local', 'pythonw.exe')
        poster, shower, focuser = Mock(), Mock(), Mock()
        owner = core.native.Owner(999, r'C:\Python312\pythonw.exe', 123)
        unrelated = core.native.Owner(998, r'C:\Python312\pythonw.exe', 456)
        finder = lambda: [(777, unrelated), (888, owner)]
        self.assertTrue(handle_existing_instance(
            lock, finder=finder, asker=lambda *_: 'replace',
            poster=poster, shower=shower, focuser=focuser))
        poster.assert_called_once_with(888, 0x0016, 1, 0)
        poster.reset_mock()
        self.assertFalse(handle_existing_instance(
            lock, finder=finder, asker=lambda *_: 'show',
            poster=poster, shower=shower, focuser=focuser))
        shower.assert_called_once_with(888, 9)
        focuser.assert_called_once_with(888)
        poster.assert_not_called()

    def test_existing_instance_refuses_changed_owner_after_confirmation(self):
        lock = Mock()
        lock.getLockInfo.side_effect = [(999, 'local', 'pythonw.exe'), (998, 'local', 'pythonw.exe')]
        owner = core.native.Owner(999, r'C:\Python312\pythonw.exe', 123)
        poster = Mock()
        self.assertFalse(handle_existing_instance(
            lock, finder=lambda: [(888, owner)], asker=lambda *_: 'replace', poster=poster))
        poster.assert_not_called()

    def test_start_progress_single_flight_and_stale_scan(self):
        panel = self.make_panel()
        self.assertIn('未启动', panel.device.text())
        ready_to_launch, release = Event(), Event()

        def ensure(*, progress, cancelled, adb_path):
            progress('正在唤起 ADB…')
            ready_to_launch.set()
            if not release.wait(2):
                raise AssertionError('test did not release startup')
            return READY

        self.ensure.side_effect = ensure
        proc = Mock()
        proc.poll.return_value = None
        panel.request_scan()
        scan = panel.scan_task
        scan.result(timeout=2)
        with (patch('core.launch', return_value=proc) as launch, patch('core.stop_process') as stop,
              patch('win_input.find_window', return_value=None)):
            try:
                panel.toggle_service()
                self.assertIn('正在检查 ADB', panel.message.text())
                self.assertTrue(ready_to_launch.wait(2))
                self.assertFalse(panel.service.isEnabled())
                self.assertFalse(panel.reset_adb.isEnabled())
                panel.toggle_service()
                panel.request_scan()
                self.assertIsNone(panel.scan_task)
                panel.tick()
                self.assertIn('正在唤起 ADB', panel.message.full_text)
            finally:
                release.set()
            panel.operation_task.result(timeout=2)
            panel.tick()
            self.ensure.assert_called_once()
            launch.assert_called_once()
            self.assertIs(panel.proc, proc)
            self.assertEqual(panel.phase, 'starting')
            self.dispose_panel(panel)
            stop.assert_called_once_with(proc, None)

    def test_reset_requires_confirmation_and_refreshes_status(self):
        self.probe.return_value = MISMATCH
        panel = self.make_panel()
        self.assertTrue(panel.reset_adb.isEnabled())
        self.assertIn('002a', panel.diagnostic_text())
        with (patch.object(panel, 'ask_reset') as confirm,
              patch('core.reset_to_leidian_adb') as reset, patch('core.launch') as launch):
            confirm.return_value = False
            panel.reset_adb.click()
            self.assertIn('PID 42', confirm.call_args.args[0])
            self.assertIn('5037', confirm.call_args.args[0])
            reset.assert_not_called()
            confirm.return_value = True
            self.probe.return_value = READY
            panel.reset_adb.click()
            self.assertFalse(panel.reset_adb.isEnabled())
            panel.operation_task.result(timeout=2)
            panel.tick()
            panel.scan_task.result(timeout=2)
            panel.tick()
            self.assertIs(reset.call_args.args[0], ADB_OWNER)
            reset.assert_called_once()
            launch.assert_not_called()
            self.assertIn('已重置', panel.message.text())
            self.assertIn('测试平板', panel.device_picker.currentText())
            self.assertFalse(panel.reset_adb.isEnabled())

    def test_quit_reaps_unclaimed_scrcpy_only(self):
        panel = self.make_panel()
        launch_entered, release, reaped = Event(), Event(), Event()
        proc = self.make_proc()

        def launch(*args, **kwargs):
            launch_entered.set()
            if not release.wait(2):
                raise AssertionError('test did not release launch')
            return proc

        def stop(target, hwnd):
            reaped.set()

        with (patch('core.launch', side_effect=launch), patch('core.stop_process', side_effect=stop) as cleanup,
              patch('core.native.terminate') as terminate, patch('core.request_adb_shutdown') as shutdown,
              patch('win_input.find_window', return_value=101)):
            panel.toggle_service()
            try:
                self.assertTrue(launch_entered.wait(2))
                panel.cleanup()
            finally:
                release.set()
            self.assertTrue(reaped.wait(2))
            cleanup.assert_called_once_with(proc, 101)
            terminate.assert_not_called()
            shutdown.assert_not_called()
            self.assertIsNone(panel.proc)

    def test_chinese_error_and_read_only_smoke(self):
        self.ensure.side_effect = core.AdbError('read', 'timed out')
        panel = self.make_panel()
        with patch('core.launch') as launch:
            panel.toggle_service()
            with self.assertRaises(core.AdbError):
                panel.operation_task.result(timeout=2)
            panel.tick()
            self.assertIn('完整回复', panel.message.text())
            self.assertNotIn('timed out', panel.message.text())
            launch.assert_not_called()
        self.ensure.reset_mock()
        self.probe.return_value = MISMATCH
        smoke = self.make_panel(read_only=True)
        self.assertIsNone(smoke.scan_task)
        with patch.object(smoke, 'ask_reset') as confirm:
            smoke.toggle_service()
            smoke.confirm_reset()
            confirm.assert_not_called()
        self.ensure.assert_not_called()


def live_test_port():
    try:
        port = int(os.environ.get('TABLET_SWITCHER_LIVE_PORT', ''))
    except ValueError:
        port = 0
    if (os.environ.get('TABLET_SWITCHER_LIVE_AUTHORIZED') != '1' or not 1024 <= port <= 65535
            or port == core.ADB_PORT or os.environ.get('QT_QPA_PLATFORM') == 'offscreen'):
        raise SystemExit('Live test refused: authorize an isolated desktop/tablet and specify its non-5037 '
                         'TABLET_SWITCHER_LIVE_PORT with TABLET_SWITCHER_LIVE_AUTHORIZED=1.')
    return port


def forward_snapshot(port, serial):
    return (set(core.adb_query('host:list-forward', port=port).splitlines()),
            set(core.adb_query('reverse:list-forward', serial, port=port).splitlines()))


def live_service(capture=False):
    """Explicitly authorized spare tablet and idle, unlocked desktop; never use port 5037."""
    port = live_test_port()
    serial = os.environ.get('TABLET_SERIAL', '').strip()
    if not serial:
        raise SystemExit('Live test refused: set TABLET_SERIAL to the authorized spare tablet.')
    if core.probe_adb(port).state is not S.READY:
        raise SystemExit('Live test refused: start the compatible isolated ADB server yourself first.')
    baseline = forward_snapshot(port, serial)
    with TemporaryDirectory(prefix='tablet-switcher-live-') as temp:
        with patch.object(gui, 'CONFIG', Path(temp) / 'config.json'):
            core.save_config(gui.CONFIG, dict(core.DEFAULTS, selected_serial=serial))
            run_live_service(port, serial, Path(temp) / 'scrcpy.log', baseline, capture)


def run_live_service(port, serial, log_path, baseline, capture):
    if capture:
        import logging
        logging.basicConfig(level=logging.INFO, format='%(relativeCreated)dms %(message)s')
    app = W.QApplication(sys.argv[:1])
    class LivePanel(Panel):
        def scan_status(self):
            return super().scan_status(port)

        def launch_service(self, config, reapply, initial=None):
            return core.launch(config, log_path=log_path, port=port, cancelled=self.cancel_start.is_set,
                               initial=initial)

    panel = LivePanel(smoke=not capture)
    desktop_clip = win.clip_bounds()
    panel.show()
    panel.toggle_service()
    try:
        for _ in range(240):
            if not panel.operation_task:
                break
            QTest.qWait(50)
        proc = panel.proc
        assert proc, panel.note
        for _ in range(100):
            if panel.phase == 'ready' and not core.win.visible(panel.cap):
                break
            QTest.qWait(50)
        assert panel.phase == 'ready' and not core.win.visible(panel.cap), panel.note
        assert panel.bar.isVisible() and panel.tray.isVisible()
        running = forward_snapshot(port, serial)
        print('Qt real service waiting, owned SDL window hidden, edge/tray visible; pid', proc.pid)
        if capture:
            panel.change('return_key', True)
            assert panel.hot_registered

            def reach(state):
                end = time.monotonic() + 1.5
                while panel.phase != state and time.monotonic() < end:
                    QTest.qWait(20)
                assert panel.phase == state, (state, panel.phase, panel.note)

            def release_ok():
                reach('ready')
                QTest.qWait(130)
                assert not win.visible(panel.cap) and win.clip_bounds() == desktop_clip

            def captured_stably():
                reach('active')
                QTest.qWait(150)
                assert win.capture_snapshot(panel.cap, proc).captured, panel.phase

            assert win.tap(0x78)
            captured_stably()
            print('Qt WM_HOTKEY F9 enters:', win.clip_bounds())
            assert win.tap(0xa4)
            release_ok()
            print('LAlt -> ready, idle SDL window hidden:', win.clip_bounds())

            assert win.tap(0x78)
            captured_stably()
            assert win.tap(0x78)
            release_ok()
            print('F9 while captured -> Qt returns, idle window hidden:', win.clip_bounds())

            QTest.mouseClick(panel.bar, Q.Qt.MouseButton.LeftButton)
            captured_stably()
            assert win.tap(0xa4)
            release_ok()
            print('Edge bar click -> capture -> LAlt return OK')

            panel.config['auto'] = True
            x, y, _, h = panel.region(1)
            # Convert Qt logical coordinates to the current monitor's native pixels via QCursor.
            from PySide6.QtGui import QCursor
            QCursor.setPos(x - 20 if panel.config['side'] == 'right' else x + 20, y + h // 2)
            QTest.qWait(60)
            QCursor.setPos(x, y + h // 2)
            captured_stably()
            assert win.tap(0xa4)
            release_ok()
            print('Edge dwell -> capture -> LAlt return OK')
        app.aboutToQuit.connect(panel.cleanup)
        Q.QTimer.singleShot(10, lambda: panel.tray_quit.trigger())
        Q.QTimer.singleShot(5000, lambda: app.exit(124))
        assert app.exec() == 0
    finally:
        panel.cleanup()
        panel.executor.shutdown(wait=True)
        after = forward_snapshot(port, serial)
        assert after == baseline, tuple((a - b, b - a) for a, b in zip(after, baseline))
    assert proc.returncode == 0, proc.returncode
    print('Tray Quit: owned scrcpy exit=0, forward/reverse baseline restored; '
          'test-time additions:', tuple(len(r - b) for r, b in zip(running, baseline)))


class MultiDeviceV23Tests(unittest.TestCase):
    def test_explicit_target_never_falls_back_to_same_model(self):
        candidates = core.parse_devices_l('USB-1 offline usb:1-2 model:Same\n'
                                          '[2001:db8::1]:5555 device model:Same')
        self.assertEqual(candidates[1].transport, 'tcpip')
        self.assertIsNone(core.select_target_device(candidates, {'selected_serial': 'USB-1'}))
        self.assertIsNone(core.select_target_device(candidates[1:], {'selected_serial': 'USB-1'}))
        self.assertEqual(core.select_target_device(candidates).serial, '[2001:db8::1]:5555')

    def test_client_protocol_is_read_locally_and_mismatch_is_rejected(self):
        path = Path('platform-tools/adb.exe')
        with patch('core.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'Android Debug Bridge version 1.0.42\n', '')
            self.assertEqual(core.adb_client_protocol(path), '002a')
            self.assertEqual(run.call_args.args[0], [str(path), 'version'])
            run.return_value.stdout = 'unrecognized'
            with self.assertRaisesRegex(core.SwitcherError, '无法识别'):
                core.adb_client_protocol(path)

    def test_launch_stops_if_compatible_listener_identity_changes(self):
        replacement = core.AdbProbe(S.READY, (core.native.Owner(42, str(core.ADB), 999),), '0029')
        with (TemporaryDirectory() as temp,
              patch('core.resolve_adb_path', return_value=Path('adb.exe')),
              patch('core.resolve_scrcpy_path', return_value=Path('scrcpy.exe')),
              patch('core.adb_client_protocol', return_value='0029'),
              patch('core.probe_adb', side_effect=[READY, replacement]),
              patch('core.preflight', return_value=DEVICE) as check, patch('core.subprocess.Popen') as spawn,
              self.assertRaisesRegex(core.SwitcherError, '监听者已变化')):
            core.launch(core.DEFAULTS, Path(temp) / 'child.log', serial=TEST_SERIAL)
        self.assertEqual(check.call_args.args[0], TEST_SERIAL)
        spawn.assert_not_called()

    def test_failure_reads_only_current_session_log(self):
        with TemporaryDirectory() as temp:
            log = Path(temp) / 'scrcpy.log'
            old = b'permission denied on /dev/uhid\n'
            log.write_bytes(old + b'ERROR: connection reset\n')
            proc = Mock(switcher_log=(log, len(old)))
            self.assertEqual(core.classify_uhid_failure(core.session_log_tail(proc)), 'connection')

    def test_parse_transport_and_duplicate_serial(self):
        text = ('USB-1 device usb:1-2 model:One\n'
                '192.0.2.1:5555 device model:Two\n'
                'USB-1 offline usb:1-2 model:Latest\n'
                'emulator-5554 device model:Emulator\n')
        devices = core.parse_devices_l(text)
        self.assertEqual([item.serial for item in devices], ['USB-1', '192.0.2.1:5555'])
        self.assertEqual(devices[0].state, 'offline')
        self.assertEqual(devices[1].transport, 'tcpip')

    def test_missing_probe_is_absent_without_global_fallback(self):
        with patch('core.adb_query', side_effect=AssertionError('must not query')):
            self.assertEqual(core.probe_usb().state, core.UsbState.ABSENT)

    def test_aoa_requires_usb_for_keyboard_and_mouse(self):
        with self.assertRaisesRegex(core.SwitcherError, 'AOA'):
            core.command(dict(core.DEFAULTS, keyboard=True, keyboard_mode='aoa', transport='tcpip'),
                         serial='192.0.2.1:5555', scrcpy_path='scrcpy.exe')
        with self.assertRaisesRegex(core.SwitcherError, 'AOA'):
            core.command(dict(core.DEFAULTS, mouse_mode='aoa', transport='tcpip'),
                         serial='192.0.2.1:5555', scrcpy_path='scrcpy.exe')

    def test_usb_probe_message_has_no_model_fallback(self):
        self.assertEqual(core.UsbProbe(core.UsbState.DEVICE).message, '平板已连接')


if __name__ == '__main__':
    if '--live-service' in sys.argv or '--live-capture' in sys.argv:
        live_service(capture='--live-capture' in sys.argv)
    else:
        unittest.main()
