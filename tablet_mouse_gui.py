"""Tray-first scrcpy UHID switcher for Windows 11."""
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, SimpleQueue
from tempfile import TemporaryDirectory
from threading import Event
from types import MappingProxyType

from PySide6 import QtCore as Q, QtGui as G, QtWidgets as W

import adb_windows as native
import core
import win_input as win

APP = '平板物理键鼠切换器'
CONFIG = core.HOME / 'config.json'
HOT_ID = 1121
KEYS = core.HOT_KEYS
RUNTIME_KEYS = ('keyboard', 'keyboard_mode', 'mouse_mode', 'protect')
TRANSPORT_NAMES = {'usb': 'USB', 'tcpip': '无线', 'mdns': '无线'}
UHID_REASONS = {'permission': '权限受限', 'unsupported': '设备不支持', 'layout': '请检查键盘布局',
                'connection': '连接中断', 'unknown': '请查看日志'}
MANUAL_FOCUS = '系统未允许安全取焦或点击；请松开按键，人工点击悬浮条重试。'
KEYBOARD_MODE_ITEMS = (
    ('实体键盘 UHID', 'uhid'),
    ('系统输入 SDK', 'sdk'),
    ('USB 直连 AOA（实验）', 'aoa'),
)

STYLE = '''
QWidget { color:#e5edf9; font: 13px "Microsoft YaHei UI"; }
QMainWindow, QWidget#surface, QDialog, QMessageBox, QScrollArea { background:#121923; }
QFrame#card { background:#1c2736; border:1px solid #2e3a4b; border-radius:11px; }
QLabel#hero { font-size:24px; font-weight:700; }
QLabel#muted { color:#aebed3; }
QLabel#state { font-weight:700; }
QPushButton, QToolButton { background:#1c2736; min-height:30px; padding:0 12px;
  border:1px solid #4b5c73; border-radius:7px; font-weight:600; }
QPushButton#primary { background:#325bc2; color:white; border-color:#325bc2; font-weight:700; }
QPushButton#danger { color:#f08080; border-color:#f08080; }
QPushButton:hover, QToolButton:hover { background:#283952; }
QPushButton#primary:hover { background:#3a65d0; }
QPushButton:disabled { background:#232d3a; color:#a7b4c6; }
QPushButton#danger:disabled { color:#bc858a; border-color:#765359; }
QPushButton:focus, QToolButton:focus, QComboBox:focus, QSpinBox:focus { border:1px solid #92baff; }
QComboBox, QSpinBox, QMenu { background:#26364b; padding:4px; }
QMenu::item:selected { background:#3454a0; }
QPlainTextEdit { background:#1c2736; border:1px solid #4b5c73; padding:8px; }
QScrollBar:vertical { background:#121923; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#4b5c73; min-height:32px; border-radius:3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }
QLabel#info { color:#aebed3; border:1px solid #8395ad; border-radius:6px; font-size:10px; font-weight:700; }
'''


def glyph():
    pix = G.QPixmap(48, 48)
    pix.fill(Q.Qt.GlobalColor.transparent)
    p = G.QPainter(pix)
    p.setRenderHint(G.QPainter.RenderHint.Antialiasing)
    p.setPen(Q.Qt.PenStyle.NoPen)
    p.setBrush(G.QColor('#3454a0'))
    p.drawRoundedRect(0, 0, 48, 48, 11, 11)
    p.setPen(G.QPen(G.QColor('#b9f8e5'), 3, Q.Qt.PenStyle.SolidLine, Q.Qt.PenCapStyle.RoundCap))
    p.drawRoundedRect(9, 12, 30, 23, 4, 4)
    p.drawLine(16, 23, 32, 23)
    p.drawLine(26, 17, 32, 23)
    p.drawLine(26, 29, 32, 23)
    p.end()
    return G.QIcon(pix)


class SingleLine(W.QLabel):
    def setText(self, text):
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        self.setAccessibleName(self.full_text)
        self.refresh()

    def refresh(self):
        text = ' '.join(getattr(self, 'full_text', '').split())
        super().setText(self.fontMetrics().elidedText(text, Q.Qt.TextElideMode.ElideRight,
                                                      self.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()


class FailureLog:
    WINDOW = 5.0

    def __init__(self):
        self.last = None
        self.since = self.repeats = 0

    def flush(self, now, *, force=False):
        if self.last and (force or now - self.since >= self.WINDOW):
            if self.repeats:
                logging.info('operation=%s stage=%s state=%s kind=%s reason=%s suppressed=%d',
                             *self.last, self.repeats)
            self.last, self.repeats = None, 0

    def record(self, operation, phase, state, exc):
        expected = isinstance(exc, core.SwitcherError)
        reason = (' '.join(str(exc).split()) if expected else type(exc).__name__)
        stage = exc.stage if isinstance(exc, core.AdbError) else phase
        signature = (operation, stage, state, 'expected' if expected else 'unexpected', reason)
        now = time.monotonic()
        self.flush(now, force=signature != self.last)
        if self.last == signature:
            self.repeats += 1
            return
        self.last, self.since = signature, now
        logging.log(logging.INFO if expected else logging.ERROR,
                    'operation=%s stage=%s state=%s kind=%s reason=%s', *signature,
                    exc_info=None if expected else (type(exc), exc, exc.__traceback__))


def file_log_handler(path):
    return RotatingFileHandler(path, maxBytes=core.LOG_LIMIT, backupCount=3, encoding='utf-8', delay=True)


class EdgeBar(W.QWidget):
    WIDTH = 14
    clicked = Q.Signal()

    def __init__(self):
        flag = Q.Qt.WindowType
        super().__init__(None, flag.Tool | flag.FramelessWindowHint | flag.WindowStaysOnTopHint |
                         flag.WindowDoesNotAcceptFocus)
        self.setAttribute(Q.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Q.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Q.Qt.CursorShape.PointingHandCursor)
        self.setToolTip('点此切入 · 左 Alt 返回')

    def paintEvent(self, _):
        p = G.QPainter(self)
        p.fillRect(self.rect(), G.QColor(92, 167, 255, 3))  # keep the layered HWND hit band nonzero
        p.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        p.setPen(Q.Qt.PenStyle.NoPen)
        p.setBrush(G.QColor(92, 167, 255, 225 if self.underMouse() else 105))
        p.drawRoundedRect(self.rect().adjusted(2, 0, -2, 0), 5, 5)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Q.Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()


class Panel(W.QMainWindow):
    def __init__(self, smoke=False, *, read_only=False, missing=(), check_tools=False):
        super().__init__()
        self.smoke = smoke
        self.config_error = ''
        try:
            self.config = core.load_config(CONFIG)
        except (OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.config_error = f'设置读取失败：{reason}。已使用默认设置；手动更改任一设置将备份原件并保存。'
            self.config = core.DEFAULTS.copy()
        self.proc = self.cap = self.previous = self.return_pos = None
        self.phase = 'off'
        self.generation = 0
        self.applied_config = self.start_config = None
        self.stopping_proc = None
        self.pending_config = None
        self.reapply_revision = 0
        self.scan_generation = self.operation_generation = None
        self.missing_device_samples = 0
        self.hot_binding = None
        self.next_hot_id = HOT_ID
        self.release_keys = ()
        self.session_blocked = False
        self.tool_paths = []
        if check_tools:
            missing = list(missing)
            for resolve in (core.resolve_adb_path, core.resolve_scrcpy_path):
                try:
                    self.tool_paths.append(str(resolve(self.config)))
                except core.SwitcherError as exc:
                    missing.append(str(exc))
        self.missing = missing
        self.read_only = read_only
        self.note = self.config_error or ('缺少程序：' + '；'.join(missing) if missing else '')
        self.last_error = ''
        self.uhid_failure = ''
        self.hot_registered = self.quitting = False
        self.scan_task = self.operation_task = self.adb_status = None
        self.usb_status = None
        self.device_candidates = []
        self.selected_device = None
        self.target_serial = self.config['selected_serial']
        self.failures = FailureLog()
        self.operation_kind = ''
        self.confirming_reset = False
        self.cancel_start = Event()
        self.progress = SimpleQueue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.dwell = core.Dwell()
        self.bar = EdgeBar()
        self.bar.clicked.connect(lambda: self.capture_action('edge_bar'))
        self.setWindowTitle(APP)
        self.setWindowIcon(glyph())
        self.build_panel()
        self.build_tray()
        app = W.QApplication.instance()
        self.screen_watch = []
        app.screenAdded.connect(self.refresh_screens)
        app.screenRemoved.connect(self.refresh_screens)
        app.primaryScreenChanged.connect(self.refresh_screens)
        self.refresh_screens()
        self.session_registered = not smoke and win.session_notifications(int(self.winId()), True)
        if not smoke and not self.session_registered:
            logging.warning('Windows session notifications unavailable; relying on foreground/clip checks')
        self.timer = Q.QTimer(self, interval=25)
        self.timer.setTimerType(Q.Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.scan_timer = Q.QTimer(self, interval=1500)
        self.scan_timer.timeout.connect(self.request_scan)
        self.scan_timer.start()
        if not (self.smoke and self.read_only):
            self.request_scan()
        self.bind_hotkey()
        self.render()

    def build_panel(self):
        shell = W.QWidget(objectName='surface')
        self.setCentralWidget(shell)
        outer = W.QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = W.QScrollArea(widgetResizable=True)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)
        surface = W.QWidget(objectName='surface')
        scroll.setWidget(surface)
        root = W.QVBoxLayout(surface)
        root.setContentsMargins(16, 16, 16, 8)
        root.setSpacing(12)
        heading = W.QHBoxLayout()
        heading.addWidget(W.QLabel(APP, objectName='hero'), 1)
        self.version_badge = W.QLabel(core.APP_VERSION, objectName='muted')
        heading.addWidget(self.version_badge)
        root.addLayout(heading)
        root.addWidget(W.QLabel('鼠标与键盘共享 · 不投屏、不传音频', objectName='muted'))

        def card():
            frame = W.QFrame(objectName='card')
            box = W.QVBoxLayout(frame)
            box.setContentsMargins(14, 12, 14, 12)
            box.setSpacing(8)
            root.addWidget(frame)
            return box

        status = card()
        row = W.QHBoxLayout()
        self.state = W.QLabel(objectName='state')
        row.addWidget(self.state, 1)
        self.service = W.QPushButton(objectName='primary', clicked=self.main_action)
        self.stop_button = W.QPushButton('停止', clicked=self.stop_or_cancel)
        row.addWidget(self.service)
        row.addWidget(self.stop_button)
        status.addLayout(row)
        status_row = W.QHBoxLayout()
        self.device = self.single_line()
        status_row.addWidget(self.device, 1)
        self.device_picker = W.QComboBox()
        self.device_picker.setFixedWidth(228)
        self.device_picker.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_picker.setMinimumContentsLength(18)
        self.device_picker.setPlaceholderText('请选择目标平板')
        self.device_picker.setAccessibleName('目标平板设备')
        self.device_picker.activated.connect(self.select_device)
        status_row.addWidget(self.device_picker)
        self.refresh_devices_button = W.QPushButton('刷新', clicked=self.refresh_devices)
        status_row.addWidget(self.refresh_devices_button)
        status_row.addWidget(W.QPushButton('详情 ›', clicked=self.show_details))
        status.addLayout(status_row)

        def checkbox(box, key, label):
            widget = W.QCheckBox(label, checked=self.config[key])
            widget.toggled.connect(lambda checked: self.change(key, checked))
            box.addWidget(widget)
            return widget

        gameplay = card()
        gameplay.addWidget(W.QLabel('输入设置', objectName='state'))
        kb_line = W.QHBoxLayout()
        self.keyboard = checkbox(kb_line, 'keyboard', '共享键盘')
        self.keyboard_mode = W.QComboBox()
        for label, value in KEYBOARD_MODE_ITEMS:
            self.keyboard_mode.addItem(label, value)
        idx = self.keyboard_mode.findData(self.config.get('keyboard_mode', 'uhid'))
        self.keyboard_mode.setCurrentIndex(max(0, idx))
        self.keyboard_mode.activated.connect(
            lambda _: self.change('keyboard_mode', self.keyboard_mode.currentData()))
        kb_line.addWidget(self.keyboard_mode)
        self.aoa_hint = W.QLabel('AOA 需要 USB', objectName='muted')
        kb_line.addWidget(self.aoa_hint)
        kb_line.addStretch()
        self.kb_settings_btn = W.QPushButton('实体键盘设置 ›',
                                              clicked=self.open_tablet_kb_settings)
        kb_line.addWidget(self.kb_settings_btn)
        gameplay.addLayout(kb_line)
        help_line = W.QHBoxLayout()
        self.uhid_reason = self.single_line()
        help_line.addWidget(self.uhid_reason, 1)
        self.open_kb_help_btn = W.QPushButton('打开物理键盘设置', clicked=self.open_tablet_kb_settings)
        self.use_sdk_btn = W.QPushButton('改用 SDK', clicked=self.use_sdk)
        help_line.addWidget(self.open_kb_help_btn)
        help_line.addWidget(self.use_sdk_btn)
        gameplay.addLayout(help_line)
        protect = W.QHBoxLayout()
        self.protect = checkbox(protect, 'protect', '游戏防误触')
        self.protect.setToolTip('开启后普通右键交给游戏；按住 Shift 才使用 Android 鼠标快捷操作。')
        protect.addStretch()
        protect.addWidget(W.QLabel('回电脑：轻按左 Alt', objectName='muted'))
        gameplay.addLayout(protect)

        def fold(host, label):
            button = W.QToolButton(text=label, checkable=True)
            button.setToolButtonStyle(Q.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setArrowType(Q.Qt.ArrowType.RightArrow)
            host.addWidget(button, 1)
            body = W.QWidget()
            body.hide()
            button.toggled.connect(body.setVisible)
            button.toggled.connect(lambda opened: button.setArrowType(
                Q.Qt.ArrowType.DownArrow if opened else Q.Qt.ArrowType.RightArrow))
            return button, body, W.QVBoxLayout(body)

        folds = W.QHBoxLayout()
        root.addLayout(folds)
        self.methods_toggle, methods_body, methods = fold(folds, '切入方式')
        self.advanced_toggle, advanced_body, advanced = fold(folds, '高级与诊断')
        root.addWidget(methods_body)
        root.addWidget(advanced_body)
        screen_row = W.QHBoxLayout()
        self.screen_picker = W.QComboBox()
        self.screen_picker.setAccessibleName('悬浮条显示器')
        self.screen_picker.currentIndexChanged.connect(
            lambda _: self.change('screen', self.screen_picker.currentData()))
        screen_row.addWidget(W.QLabel('显示器'))
        screen_row.addWidget(self.screen_picker, 1)
        methods.addLayout(screen_row)
        modes = W.QHBoxLayout()
        checkbox(modes, 'bar', '悬浮条切入')
        self.auto = checkbox(modes, 'auto', '撞边自动切入')
        modes.addStretch()
        methods.addLayout(modes)
        line = W.QHBoxLayout()
        side = W.QComboBox()
        side.addItems(['右边缘', '左边缘'])
        side.setAccessibleName('悬浮条侧边')
        side.setCurrentIndex(int(self.config['side'] == 'left'))
        side.currentIndexChanged.connect(lambda n: self.change('side', 'left' if n else 'right'))
        line.addWidget(side)
        for key, label, suffix in (('position', '位置', '%'), ('span', '范围', '%'), ('dwell', '停留', ' ms')):
            low, high = core.RANGES[key]
            field = W.QSpinBox(minimum=low, maximum=high, value=self.config[key], suffix=suffix)
            field.setAccessibleName(label)
            field.valueChanged.connect(lambda value, k=key: self.change(k, value))
            line.addWidget(W.QLabel(label))
            line.addWidget(field)
        methods.addLayout(line)
        hk = W.QHBoxLayout()
        checkbox(hk, 'hotkey', '全局快捷键')
        key = W.QComboBox()
        key.addItems(list(KEYS))
        key.setAccessibleName('切入快捷键')
        key.setCurrentText(self.config['key'])
        key.currentTextChanged.connect(lambda value: self.change('key', value))
        hk.addWidget(key)
        hk.addStretch()
        methods.addLayout(hk)
        self.hot_hint = W.QLabel(objectName='muted')
        methods.addWidget(self.hot_hint)
        checkbox(advanced, 'return_key', '用同一全局快捷键切回电脑')
        mouse_line = W.QHBoxLayout()
        mouse_line.addWidget(W.QLabel('鼠标模式'))
        self.mouse_mode = W.QComboBox()
        for label, value in (('系统输入 SDK', 'sdk'), ('实体鼠标 UHID', 'uhid'), KEYBOARD_MODE_ITEMS[-1]):
            self.mouse_mode.addItem(label, value)
        self.mouse_mode.activated.connect(lambda _: self.change('mouse_mode', self.mouse_mode.currentData()))
        mouse_line.addWidget(self.mouse_mode)
        mouse_line.addStretch()
        advanced.addLayout(mouse_line)
        tools = W.QHBoxLayout()
        tools.addWidget(W.QPushButton('打开日志目录', clicked=lambda: G.QDesktopServices.openUrl(
            Q.QUrl.fromLocalFile(str(core.HOME)))))
        tools.addWidget(W.QPushButton('复制诊断', clicked=self.copy_diagnostics))
        tools.addStretch()
        self.reset_adb = W.QPushButton('重置为雷电 ADB', objectName='danger', clicked=self.confirm_reset)
        tools.addWidget(self.reset_adb)
        advanced.addLayout(tools)
        root.addStretch()

        footer = W.QHBoxLayout()
        footer.setContentsMargins(16, 7, 16, 12)
        info = W.QLabel('i', objectName='info', alignment=Q.Qt.AlignmentFlag.AlignCenter)
        info.setFixedSize(13, 13)
        footer.addWidget(info)
        self.message = self.single_line()
        footer.addWidget(self.message, 1)
        footer.addWidget(W.QPushButton('查看', clicked=self.show_details))
        outer.addLayout(footer)
        screen = W.QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(560, area.width()), min(472, max(220, area.height() - 44)))

    @staticmethod
    def single_line():
        label = SingleLine(objectName='muted')
        label.setTextFormat(Q.Qt.TextFormat.PlainText)
        label.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Fixed)
        label.setFixedHeight(22)
        return label

    def diagnostic_text(self):
        adb, usb = self.adb_status, self.usb_status
        cfg = self.applied_config or self.config
        mode = dict((value, label) for label, value in KEYBOARD_MODE_ITEMS)[cfg['keyboard_mode']]
        visible_cfg = ' · '.join(f'{key}={self.config[key]}' for key in core.DEFAULTS)
        screen = self.selected_screen()
        pids = '、'.join(str(owner.pid) for owner in adb.owners) if adb else ''
        candidate = self.selected_device
        serial = self.target_serial
        transport = candidate.transport if candidate else 'unknown'
        lines = [f'{APP} {core.APP_VERSION} · {self.state.text()}',
                 f'ADB：{adb.state.value if adb else "尚未检查"}',
                 f'ADB PID：{pids or "—"} · 服务端协议：{adb.version if adb and adb.version else "—"}'
                 + f' · 客户端协议：{adb.expected if adb and adb.expected else "未检测"}',
                 *(f'{key}：{path}' for key, path in zip(('adb_path', 'scrcpy_path'), self.tool_paths)),
                 f'平板序列号：{serial or "—"} · 型号：{usb.model if usb and usb.model else "—"} · 连接：{transport}',
                 f'平板状态：{usb.state.value if usb else "未检查"}',
                 f'平板：{usb.message if usb else "等待读取"}' + (f'（{usb.stage}）' if usb and usb.stage else ''),
                 f'scrcpy PID：{self.proc.pid if self.proc else "—"} · 鼠标：{cfg["mouse_mode"]}',
                 self.uhid_message(),
                 f'显示器：{screen.name() if screen else "—"} · {self.screen_note or "已选定"}',
                 f'实际运行：{mode if cfg["keyboard"] else "只共享鼠标"}' if self.applied_config else
                 f'键盘选项：{mode if cfg["keyboard"] else "只共享鼠标"}',
                 f'设置：{visible_cfg}', self.config_error, self.note,
                 f'最近操作错误：{self.last_error}' if self.last_error and self.last_error != self.note else '',
                 '帮助：轻按左 Alt 回电脑；关闭面板后仍在托盘运行。',
                 '游戏防误触开启时，Shift＋右/中键分别返回/回桌面，Shift＋侧键用于多任务/通知。']
        return '\n'.join(line for line in lines if line)

    def copy_diagnostics(self):
        W.QApplication.clipboard().setText(self.diagnostic_text())

    def show_details(self):
        dialog = W.QDialog(self)
        dialog.setWindowTitle('状态与诊断')
        content = W.QVBoxLayout(dialog)
        diagnostic = W.QPlainTextEdit(self.diagnostic_text(), readOnly=True)
        content.addWidget(diagnostic)
        buttons = W.QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(W.QPushButton('复制诊断', clicked=lambda: W.QApplication.clipboard().setText(
            diagnostic.toPlainText())))
        buttons.addWidget(W.QPushButton('关闭', clicked=dialog.accept))
        content.addLayout(buttons)
        dialog.resize(min(520, self.width()), min(410, self.height()))
        dialog.exec()
        dialog.deleteLater()

    def build_tray(self):
        self.tray = W.QSystemTrayIcon(self.windowIcon(), self)
        self.menu = W.QMenu(self)
        self.tray_status = self.menu.addAction(f'{APP} {core.APP_VERSION} · 未启动')
        self.tray_status.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction('打开控制面板').triggered.connect(self.open_panel)
        self.tray_service = self.menu.addAction('启动平板键鼠服务')
        self.tray_service.triggered.connect(self.toggle_service)
        self.tray_enter = self.menu.addAction('切入平板')
        self.tray_enter.triggered.connect(lambda: self.capture_action('tray'))
        self.tray_keyboard = self.menu.addAction('同时共享键盘')
        self.tray_keyboard.setCheckable(True)
        self.tray_keyboard.setChecked(self.config['keyboard'])
        self.tray_keyboard.toggled.connect(self.keyboard.setChecked)
        self.tray_auto = self.menu.addAction('撞边自动切入')
        self.tray_auto.setCheckable(True)
        self.tray_auto.setChecked(self.config['auto'])
        self.tray_auto.toggled.connect(self.auto.setChecked)
        self.menu.addSeparator()
        self.tray_quit = self.menu.addAction('退出')
        self.tray_quit.triggered.connect(self.quit_app)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (W.QSystemTrayIcon.ActivationReason.Trigger, W.QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_panel()

    def open_panel(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        W.QApplication.instance().quit()

    def closeEvent(self, event):
        if self.quitting or not self.tray.isSystemTrayAvailable():
            W.QApplication.instance().quit()
            event.accept()
        else:
            self.hide()
            event.ignore()

    def change(self, key, value, **also):
        if self.config.get(key) == value and not also:
            return
        self.config = core.validate_config(self.config | {key: value} | also)
        if key in RUNTIME_KEYS and self.phase == 'off':
            self.uhid_failure = ''
        if not (self.smoke or self.read_only):
            try:
                repairing = bool(self.config_error)
                core.save_config(CONFIG, self.config, backup_invalid=repairing)
                self.config_error = ''
                if repairing:
                    self.note = '设置已保存；损坏原件保留为 config.json.bak。'
            except OSError:
                self.note = '保存设置失败；请检查配置目录权限后再试。'
        if key in ('hotkey', 'key', 'return_key'):
            self.bind_hotkey()
        if key in ('bar', 'auto', 'side', 'position', 'span', 'dwell', 'screen'):
            self.dwell = core.Dwell()
        if key == 'screen':
            self.refresh_screens()
        for action, cfg_key in ((self.tray_auto, 'auto'), (self.tray_keyboard, 'keyboard')):
            action.blockSignals(True)
            action.setChecked(bool(self.config[cfg_key]))
            action.blockSignals(False)
        if key in RUNTIME_KEYS and (self.applied_config or self.start_config or self.pending_config):
            applied = self.applied_config or self.start_config
            self.pending_config = (self.config.copy() if applied is None or
                                   any(self.config[k] != applied[k] for k in RUNTIME_KEYS) else None)
            self.note = '键鼠选项待应用，安全释放后将重连一次…' if self.pending_config else ''
            self.schedule_reapply()
        self.render()

    def set_phase(self, phase):
        if phase != self.phase:
            self.phase = phase
            self.generation += 1
            self.missing_device_samples = 0
            self.bind_hotkey()
            self.schedule_reapply()

    def schedule_reapply(self):
        self.reapply_revision += 1
        if self.pending_config and self.phase in ('ready', 'active') and not self.operation_task:
            generation, revision = self.generation, self.reapply_revision
            Q.QTimer.singleShot(350, self, lambda: self.apply_runtime_settings(generation, revision))

    def apply_runtime_settings(self, generation, revision):
        if (generation != self.generation or revision != self.reapply_revision or not self.pending_config
                or self.actions_locked() or self.phase not in ('ready', 'active')):
            return
        self.stop('正在应用新键鼠设置并重连…', reason='settings_reapply')

    def open_tablet_kb_settings(self):
        if not self.can_open_kb_settings():
            return
        self.begin_operation('kb_settings', core.open_tablet_keyboard_settings, self.target_serial, config=self.config)

    def uhid_message(self):
        return f'UHID 失败：{UHID_REASONS[self.uhid_failure]}' if self.uhid_failure else ''

    def use_sdk(self):
        self.last_error = ''
        self.note = '已改用 SDK；请重新启动服务。'
        if self.config['keyboard'] and self.config['keyboard_mode'] == 'uhid':
            mouse = {'mouse_mode': 'sdk'} if self.config['mouse_mode'] == 'uhid' else {}
            self.change('keyboard_mode', 'sdk', **mouse)
        elif self.config['mouse_mode'] == 'uhid':
            self.change('mouse_mode', 'sdk')

    def refresh_devices(self):
        if self.phase in ('starting', 'ready', 'switching', 'active', 'parking'):
            self.note = '先回电脑并停止当前会话，再切换目标设备。'
            self.render()
            return
        self.request_scan()

    def select_device(self, index):
        serial = self.device_picker.itemData(index)
        candidate = next((item for item in self.device_candidates if item.serial == serial), None)
        if not candidate:
            return
        if self.phase != 'off' or self.proc or self.stopping_proc or self.actions_locked():
            self.note = '先回电脑并停止当前会话，再切换目标设备。'
            self.render()
            return
        if serial != self.target_serial:
            self.generation += 1  # Reject any in-flight scan of the old target.
            self.last_error = self.uhid_failure = self.note = ''
        self.target_serial = serial
        self.selected_device = candidate
        self.usb_status = core.target_status(self.device_candidates, serial)
        self.change('selected_serial', serial)
        self.render()
        self.request_scan()

    def can_open_kb_settings(self):
        cfg = self.applied_config or self.config
        return (not self.actions_locked() and self.phase in ('off', 'ready') and not self.session_blocked
                and cfg['keyboard'] and cfg['keyboard_mode'] == 'uhid'
                and self.adb_status is not None and self.adb_status.state is core.AdbState.READY
                 and self.target_serial and self.usb_status is not None
                 and self.usb_status.state is core.UsbState.DEVICE)

    def bind_hotkey(self):
        want = self.hotkey_allowed()
        mods, vk = KEYS[self.config['key']]
        binding = self.hot_binding
        target = (self.generation, self.phase, mods, vk)
        if binding and want and not self.release_keys and binding[1:] == target:
            return
        if binding:
            win.unregister(int(self.winId()), binding[0])
        self.hot_binding = None
        self.hot_registered = False
        if want and not self.release_keys:
            # A fresh ID fences queued messages even when the key itself did not change.
            hot_id = self.next_hot_id
            self.next_hot_id += 1
            self.hot_registered = hot_id <= 0xbfff and bool(win.register(int(self.winId()), hot_id, mods | 0x4000, vk))
            if self.hot_registered:
                self.hot_binding = (hot_id, *target)
        self.hot_hint.setText('快捷键已被占用，改用另一组或悬浮条。'
                              if want and not self.release_keys and not self.hot_registered else
                              '回电脑：轻按左 Alt 再松开；鼠标侧键不劫持。')

    def hotkey_allowed(self):
        return (not (self.smoke or self.quitting or self.session_blocked) and self.config['hotkey']
                and (self.phase == 'ready' or (self.phase == 'active' and self.config['return_key'])))

    def handle_hotkey(self, hot_id, packed):
        mods, vk = packed & 0x3fff, (packed >> 16) & 0xffff
        if (not self.hotkey_allowed() or self.release_keys
                or (mods, vk) != KEYS[self.config['key']]
                or self.hot_binding != (hot_id, self.generation, self.phase, mods, vk)):
            return False
        self.release_keys = (*win.MOD_KEYS, vk)
        self.bind_hotkey()
        self.capture_action('hotkey')
        return True

    def nativeEvent(self, kind, message):
        if kind == b'windows_generic_MSG':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and self.handle_hotkey(msg.wParam, msg.lParam):
                return True, 0
            if msg.message == 0x02b1:  # WM_WTSSESSION_CHANGE
                if msg.wParam in (2, 4, 6, 7):  # disconnect, logoff, lock
                    self.session_change(True)
                elif msg.wParam in (1, 3, 5, 8):
                    self.session_change(False)
            elif msg.message == 0x0218:  # WM_POWERBROADCAST
                if msg.wParam == 4:
                    self.session_change(True)
                elif msg.wParam in (7, 18):
                    self.session_change(False)
            elif msg.message == 0x0011:  # WM_QUERYENDSESSION
                self.park('lock_or_session')
        return super().nativeEvent(kind, message)

    def session_change(self, blocked):
        self.session_blocked = blocked
        if blocked:
            self.park('lock_or_session')
        self.bind_hotkey()
        self.render()

    def request_scan(self):
        if not (self.quitting or (self.smoke and self.read_only) or self.scan_task
                or self.operation_task or self.confirming_reset):
            self.scan_generation = self.generation
            self.scan_task = self.executor.submit(self.scan_status)

    def scan_status(self, port=core.ADB_PORT):
        deadline = time.monotonic() + 2.0
        status = core.probe_adb(port, deadline=deadline, expected=self.adb_status.expected if self.adb_status else None)
        if status.state is not core.AdbState.READY:
            return status, core.UsbProbe(core.UsbState.ABSENT) if status.state is core.AdbState.ABSENT else None, []
        try:
            return status, None, core.scan(port, detailed=True, deadline=deadline)
        except core.AdbError as exc:
            return status, core.UsbProbe(core.UsbState.PROBE_ERROR, stage=exc.stage), []

    def begin_operation(self, kind, work, *args, **kwargs):
        if self.scan_task:
            self.scan_task.cancel()
            self.scan_task = None  # Never display a pre-operation result afterwards.
        self.operation_kind = kind
        self.operation_generation = self.generation
        self.operation_task = self.executor.submit(work, *args, **kwargs)
        labels = {'start': '正在检查 ADB…', 'reset': '正在重置 ADB…',
                   'kb_settings': '正在打开平板物理键盘设置…'}
        self.note = labels.get(kind, self.note or '正在停止服务…')
        self.render()

    def launch_service(self, config, reapply, initial=None):
        if self.cancel_start.is_set():
            return None
        if not reapply:
            self.progress.put(core.ensure_adb_ready(adb_path=core.resolve_adb_path(config),
                                                    progress=self.progress.put, cancelled=self.cancel_start.is_set))
        if self.cancel_start.is_set():
            return None
        self.progress.put('ADB 已就绪，正在检查平板…')
        deadline = time.monotonic() + core.USB_TIMEOUT
        serial = config['selected_serial']
        if not serial:
            candidates = core.scan(detailed=True, deadline=deadline)
            target = core.select_target_device(candidates, config)
            if not target:
                raise core.SwitcherError('未选定平板；请刷新列表，连接并授权后选择目标设备。')
            serial = target.serial
            self.progress.put(target)
        if not reapply:
            self.progress.put(core.wait_for_usb(serial=serial, progress=self.progress.put, cancelled=self.cancel_start.is_set,
                                                deadline=deadline))
        return core.launch(config, serial=serial, initial=initial, progress=self.progress.put, cancelled=self.cancel_start.is_set,
                            deadline=deadline)

    def main_action(self):
        if self.phase in ('ready', 'active'):
            self.capture_action('panel')
        elif self.phase == 'off':
            self.toggle_service()

    def stop_or_cancel(self):
        if self.operation_task and self.operation_kind == 'start':
            self.cancel_start.set()
            self.note = '正在取消启动…'
            self.render()
        elif self.proc:
            self.stop()

    def toggle_service(self):
        if self.actions_locked():
            return
        if self.proc:
            self.stop()
            return
        self.start_service(self.config)

    def start_service(self, config, *, reapply=False):
        handle, scale = self.native_screen()
        initial = win.capture_position(handle, config['side'], config['position'], scale=scale)
        if initial is None and not self.smoke:
            self.note = self.last_error = '无法定位选定显示器的取焦窗口；请在「切入方式」换屏后重试。'
            self.render()
            return
        self.cancel_start.clear()
        self.last_error = self.uhid_failure = ''
        self.missing_device_samples = 0
        self.start_config = MappingProxyType(dict(config, selected_serial=self.target_serial))
        self.set_phase('starting')
        self.begin_operation('start', self.launch_service, self.start_config, reapply, initial)

    def actions_locked(self):
        return self.read_only or self.quitting or self.missing or self.operation_task or self.confirming_reset

    def confirm_reset(self):
        status = self.adb_status
        if self.actions_locked() or self.proc or not status or not status.can_reset:
            return
        self.confirming_reset = True
        self.render()
        try:
            confirmed = self.ask_reset(
                f'当前占用：{status.occupant}\n\n'
            '重置会中断共用 5037 端口的 ADB 连接，可能影响模拟器或其他工具。'
                '请先保存工作并在其他软件中结束共享。确定继续吗？')
        finally:
            self.confirming_reset = False
        if confirmed and not self.actions_locked():
            def reset():
                core.reset_to_leidian_adb(status.owners[0], expected=status.expected, progress=self.progress.put)
            self.begin_operation('reset', reset)
        else:
            self.render()

    def ask_reset(self, text):
        box = W.QMessageBox(self)
        box.setIcon(W.QMessageBox.Icon.Warning)
        box.setWindowTitle('确认重置为雷电 ADB？')
        box.setTextFormat(Q.Qt.TextFormat.PlainText)
        box.setText(text)
        yes = box.addButton('确认重置', W.QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton('取消', W.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        return clicked == yes

    def operation_tick(self):
        task = self.operation_task
        if not task:
            return
        kind = self.operation_kind
        current = self.operation_generation == self.generation
        latest = None
        try:
            while True:
                event = self.progress.get_nowait()
                if current and isinstance(event, core.AdbProbe):
                    self.adb_status = event
                elif current and isinstance(event, core.UsbProbe):
                    self.usb_status = event
                elif current and isinstance(event, core.DeviceCandidate):
                    self.selected_device, self.target_serial = event, event.serial
                elif isinstance(event, str):
                    latest = event
        except Empty:
            pass
        if latest and current:
            self.note = '正在取消启动…' if kind == 'start' and self.cancel_start.is_set() else latest
            self.render()
        if not task.done():
            return
        self.operation_task = None
        if not current:
            if kind == 'start':
                self.executor.submit(self.reap_unclaimed, task)
                self.start_config = None
            self.render()
            self.request_scan()
            self.schedule_reapply()
            return
        try:
            result = task.result()
        except core.SwitcherError as exc:
            if kind == 'start' and self.cancel_start.is_set():
                self.note = '已取消启动；共享 ADB 保持原状。'
            else:
                state = self.adb_status.state.name.lower() if self.adb_status else 'unknown'
                self.failures.record(kind, self.phase, state, exc)
                self.last_error = self.note = str(exc)
        except Exception as exc:
            state = self.adb_status.state.name.lower() if self.adb_status else 'unknown'
            self.failures.record(kind, self.phase, state, exc)
            self.note = ('自有 scrcpy 回收未确认；请重试停止或查看 switcher.log。' if kind == 'stop'
                         else '操作失败；请重试或查看 switcher.log。')
            self.last_error = self.note
        else:
            if kind == 'reset':
                self.note = '已重置，雷电 ADB 就绪。'
            elif kind == 'kb_settings':
                self.note = result or '已打开平板物理键盘设置。'
            elif kind == 'start' and result is not None:
                self.proc = result
                self.applied_config = MappingProxyType(dict(self.start_config, selected_serial=self.target_serial))
                self.deadline = time.monotonic() + 10
                if self.cancel_start.is_set():
                    self.start_config = None
                    self.stop('已取消启动；共享 ADB 保持原状。')
                    return
                self.note = '键鼠选项待应用…' if self.pending_config else ''
            elif kind == 'stop':
                self.stopping_proc = None
                pending, self.pending_config = self.pending_config, None
                self.set_phase('off')
                if pending and not self.quitting:
                    self.start_service(self.config, reapply=True)
                    return
        if kind == 'start':
            self.start_config = None
            if self.proc is None:
                if self.cancel_start.is_set():
                    self.note = '已取消启动；共享 ADB 保持原状。'
                self.pending_config = None
                self.set_phase('off')
        if kind == 'stop' and self.stopping_proc is not None:
            self.proc, self.stopping_proc = self.stopping_proc, None
            self.pending_config = None
            self.set_phase('off')
        if kind == 'reset':
            self.adb_status = self.usb_status = None
        self.render()
        self.request_scan()
        self.schedule_reapply()

    def stop(self, note='', *, reason='stop', snapshot=None):
        if reason != 'settings_reapply':
            self.pending_config = None
        if self.operation_task and self.operation_kind == 'stop':
            return
        proc, hwnd = self.proc, self.cap
        if proc is None:
            return
        trigger = {'settings_reapply': 'settings', 'disconnect': 'scan', 'proc_exit': 'child', 'stop': 'service'}
        self.park(reason, note, snapshot=snapshot, stopping=True, trigger=trigger.get(reason))
        self.proc = self.cap = None
        self.applied_config = None
        self.stopping_proc = proc
        self.begin_operation('stop', self.reap_process, proc, hwnd)

    @staticmethod
    def reap_process(proc, hwnd=None):
        core.stop_process(proc, hwnd or (win.find_window(proc.pid) if proc.poll() is None else None))

    @staticmethod
    def screen_id(screen):
        serial = screen.serialNumber().strip()
        return f'{screen.manufacturer()}|{screen.model()}|{serial}' if serial and serial != '0' else screen.name()

    def selected_screen(self):
        wanted = self.config['screen']
        return next((s for s in W.QApplication.screens() if wanted and self.screen_id(s) == wanted),
                    W.QApplication.primaryScreen())

    def native_screen(self):
        screen = self.selected_screen()
        if not screen:
            return None, 1
        scale = screen.devicePixelRatio()
        return win.monitor_for_qt_rect(screen.geometry().getRect(), scale), scale

    def refresh_screens(self, *_):
        if self.quitting:
            return
        screens = W.QApplication.screens()
        for screen in self.screen_watch:
            if screen in screens:
                for signal in (screen.geometryChanged, screen.availableGeometryChanged, screen.logicalDotsPerInchChanged):
                    signal.disconnect(self.refresh_screens)
        self.screen_watch = screens
        picker = self.screen_picker
        with Q.QSignalBlocker(picker):
            picker.clear()
            picker.addItem('跟随主屏', '')
            for index, screen in enumerate(screens, 1):
                for signal in (screen.geometryChanged, screen.availableGeometryChanged, screen.logicalDotsPerInchChanged):
                    signal.connect(self.refresh_screens)
                name = screen.model().strip() or screen.name() or '显示器'
                label = f'{index} · {name}' + ('（主屏）' if screen == W.QApplication.primaryScreen() else '')
                picker.addItem(label, self.screen_id(screen))
            index = picker.findData(self.config['screen'])
            self.screen_note = '选定显示器未连接，暂用主屏。' if index < 0 else ''
            if index < 0:
                picker.addItem('原显示器未连接（暂用主屏）', self.config['screen'])
                index = picker.count() - 1
            picker.setCurrentIndex(index)
        self.dwell = core.Dwell()
        self.render()

    def capture_action(self, trigger):
        if self.phase == 'active':
            self.park('return_hotkey' if trigger == 'hotkey' else 'return_action', trigger=trigger)
        else:
            self.switch_in(trigger)

    def capture_log(self, action, snapshot, result, *, reason=None, trigger=None):
        config = self.applied_config
        mode = (config['keyboard_mode'] if config['keyboard'] else 'mouse_only') if config else 'none'
        logging.info('action=%s generation=%d trigger=%s reason_code=%s fg_kind=%s '
                     'clip_in=%s clip_active=%s clip_out=%s mode=%s result=%s mono=%.3f dt_ms=%.1f',
                     action, self.generation, trigger or getattr(self, 'capture_trigger', 'service'),
                     reason or '-', snapshot.fg_kind, getattr(self, 'entry_clip', None),
                     getattr(self, 'active_clip', None), snapshot.clip, mode, result, snapshot.monotonic,
                     1000 * (snapshot.monotonic - getattr(self, 'capture_started', snapshot.monotonic)))

    def switch_in(self, trigger='panel'):
        if self.phase != 'ready' or self.session_blocked or self.quitting:
            return
        self.set_phase('switching')
        self.capture_trigger = trigger
        self.capture_started = time.monotonic()
        self.entry_clip = self.active_clip = self.return_pos = self.moved_to = self.return_monitor = None
        try:
            snapshot = win.capture_snapshot(self.cap, self.proc)
            self.previous = snapshot.fg_after
            self.entry_clip = snapshot.clip
            self.deadline = self.capture_started + 2.2
            self.stage = 'pending'
            self.clip_since = None
            self.alt_down = False
            self.note = ''
            self.capture_log('switch_in', snapshot, 'requested')
            if snapshot.fg_before != snapshot.fg_after:
                reason = 'foreground_null' if not snapshot.fg_before or not snapshot.fg_after else 'foreground_other'
                self.park(reason, snapshot=snapshot)
                return
            self.render()
        except Exception:
            self.capture_exception()

    def hide_capture(self):
        if self.proc and self.proc.poll() is None and win.owns_window(self.cap, self.proc.pid):
            return bool(win.show(self.cap, 0))  # SW_HIDE; SDL releases on focus loss
        return None

    def park(self, reason_code, note='', *, snapshot=None, stopping=False, trigger=None):
        if self.phase == 'parking':
            return
        if self.phase not in ('active', 'switching') and not stopping:
            return
        self.set_phase('parking')
        self.note = note
        self.dwell = core.Dwell()
        self.park_restore = None
        self.park_reason = reason_code
        self.park_trigger = trigger
        try:
            try:
                snapshot = snapshot or win.capture_snapshot(self.cap, self.proc)
                if (reason_code in ('alt_return', 'return_hotkey', 'return_action')
                        and snapshot.fg_kind == 'self' and snapshot.fg_before == snapshot.fg_after
                        and self.moved_to is not None and self.return_pos is not None
                        and win.position() == self.moved_to):
                    self.park_restore = (self.return_pos, self.moved_to, self.return_monitor, self.previous)
            finally:
                hidden = self.hide_capture()
            self.capture_log('park', snapshot, 'hide_queued' if hidden else 'hide_failed' if hidden is False
                             else 'no_owned_window', reason=reason_code, trigger=trigger)
            self.park_deadline = time.monotonic() + .35
            if not stopping:
                self.park_tick(time.monotonic())
        except Exception:
            logging.exception('action=park reason_code=exception generation=%d', self.generation)
            if not stopping:
                self.stop('输入释放异常，正在回收自有 scrcpy。', reason='exception')
        self.render()

    def park_tick(self, now):
        snap = win.capture_snapshot(self.cap, self.proc)
        if not snap.pid_alive or not snap.hwnd_valid:
            self.stop('已失去捕获窗口；请重新启动服务。', reason='proc_exit' if not snap.pid_alive else 'exception',
                      snapshot=snap)
        elif (not win.visible(self.cap) and snap.fg_kind != 'self' and snap.clip is not None
              and not win.clip_inside(snap.bounds, snap.clip)):
            self.capture_log('park', snap, 'released', reason=self.park_reason, trigger=self.park_trigger)
            restore, self.park_restore = self.park_restore, None
            self.previous = self.return_pos = self.moved_to = None
            self.set_phase('ready')
            if restore:
                generation = self.generation
                Q.QTimer.singleShot(110, self, lambda: self.restore_pointer(generation, snap.fg_after, *restore))
            self.render()
        elif now >= self.park_deadline:
            self.capture_log('park', snap, 'release_unconfirmed', reason=self.park_reason, trigger=self.park_trigger)
            self.stop('未确认捕获已释放；正在回收自有 scrcpy。', reason='timeout', snapshot=snap)

    def restore_pointer(self, generation, foreground_after, pos, moved_to, monitor, previous):
        if (generation == self.generation and self.phase == 'ready' and not self.session_blocked
                and foreground_after and foreground_after == previous == win.foreground()
                and monitor and monitor == win.monitor(pos) and win.position() == moved_to
                and self.entry_clip is not None and win.clip_bounds() == self.entry_clip == win.desktop_bounds()):
            # Windows/SDL already returned focus to the prior window. Never reclaim it from another app.
            win.move(*pos)

    def switch_tick(self, now):
        snap = win.capture_snapshot(self.cap, self.proc)
        if not snap.pid_alive or not snap.hwnd_valid:
            self.stop('捕获窗口已失效。', reason='proc_exit' if not snap.pid_alive else 'exception', snapshot=snap)
            return
        if now >= self.deadline or snap.monotonic >= self.deadline:
            self.park('timeout', '切入抓取超时。' if self.stage == 'clicked' else MANUAL_FOCUS, snapshot=snap)
            return
        if not snap.fg_before or not snap.fg_after:
            self.park('foreground_null', MANUAL_FOCUS, snapshot=snap)
            return
        allowed = (self.cap,) if self.stage in ('focused', 'clicked') else (self.previous, self.cap)
        if snap.fg_before not in allowed or snap.fg_after not in allowed:
            self.park('foreground_other', snapshot=snap)
            return
        if snap.fg_before != snap.fg_after:
            self.clip_since = None
            self.capture_log('switch_tick', snap, 'sample_unstable')
            return
        if self.release_keys or win.held(win.MOD_KEYS + win.BUTTON_KEYS):
            self.clip_since = None
            return
        if snap.captured:
            if self.clip_since is None:
                self.clip_since = now
            elif now - self.clip_since >= .09:
                self.active_clip = snap.clip
                self.set_phase('active')
                self.capture_log('switch_tick', snap, 'captured')
                self.render()
            return
        self.clip_since = None
        if self.stage == 'pending':
            safe = win.owns_window(self.cap, self.proc.pid) and self.proc.poll() is None
            monitor, scale = self.native_screen()
            placed = bool(safe and (monitor or self.smoke) and win.place_window(
                self.cap, monitor, self.config['side'], self.config['position'], scale))
            self.capture_log('place_window', snap, 'ok' if placed else 'failed')
            shown = bool(placed and win.owns_window(self.cap, self.proc.pid) and win.show(self.cap, 4))
            self.capture_log('show', snap, 'ok' if shown else 'failed' if placed else 'skipped')
            if not shown:
                self.park('exception', MANUAL_FOCUS, snapshot=snap)
                return
            self.stage = 'shown'
        elif self.stage == 'shown' and win.visible(self.cap):
            if win.foreground() != snap.fg_after:
                self.park('foreground_other', snapshot=snap)
                return
            focused = (self.proc.poll() is None and win.owns_window(self.cap, self.proc.pid)
                       and win.activate(self.cap, snap.fg_after))
            self.capture_log('focus', snap, 'ok' if focused else 'failed')
            if not focused:
                self.park('focus_denied', MANUAL_FOCUS, snapshot=snap)
                return
            self.stage, self.focus_time = 'focused', now
        elif self.stage == 'focused' and now - self.focus_time >= .04:
            self.return_pos = win.position()
            self.return_monitor = win.monitor(self.return_pos)
            clicked, self.moved_to = win.click_window(self.cap, self.proc, (KEYS[self.config['key']][1],))
            self.capture_log('click_window', snap, 'ok' if clicked else 'failed')
            if not clicked:
                self.park('click_denied', MANUAL_FOCUS, snapshot=snap)
                return
            self.stage = 'clicked'

    def capture_exception(self):
        logging.exception('action=capture reason_code=exception generation=%d', self.generation)
        self.stop('捕获状态异常；正在回收自有 scrcpy。', reason='exception')

    def region(self, width):
        screen = self.selected_screen()
        if not screen:
            return None
        r = screen.geometry()
        return core.edge_region(r.getRect(), self.config['side'], self.config['position'], self.config['span'], width)

    def auto_edge(self, point):
        region = self.region(1)
        if not region or not Q.QRect(*region).contains(point):
            return False
        screen = self.selected_screen()
        r = screen.geometry()
        handle, _ = self.native_screen()
        side = self.config['side']
        adjacent = win.adjacent_monitor(handle, side, (point.y() - r.y() + .5) / r.height())
        if adjacent is not None:
            return not adjacent
        outside = point + Q.QPoint(-1 if side == 'left' else 1, 0)
        return not any(s != screen and s.geometry().contains(outside) for s in W.QApplication.screens())

    def position_bar(self):
        region = self.region(EdgeBar.WIDTH)
        if region:
            screen = self.selected_screen()
            if self.bar.screen() != screen:
                self.bar.setScreen(screen)
            self.bar.setGeometry(*region)
        return bool(region)

    def tick(self):
        self.failures.flush(time.monotonic())
        if self.scan_task and self.scan_task.done():
            task, self.scan_task = self.scan_task, None
            if self.scan_generation == self.generation:
                try:
                    result = task.result()
                    self.adb_status, self.usb_status = result[:2]
                    if len(result) > 2:
                        self.device_candidates = [item for item in result[2] if item.serial]
                        if not self.target_serial and self.phase == 'off':
                            target = core.select_target_device(self.device_candidates, self.config)
                            if target:
                                self.target_serial = target.serial
                        self.selected_device = next((item for item in self.device_candidates
                                                      if item.serial == self.target_serial), None)
                        if self.usb_status is None and self.adb_status.state is core.AdbState.READY:
                            self.usb_status = core.target_status(result[2], self.target_serial)
                except Exception:
                    logging.exception('ADB scan failed')
                    self.adb_status = core.AdbProbe(core.AdbState.BUSY_UNKNOWN)
                    self.usb_status = None
                self.render()
                missing = self.usb_status and self.usb_status.state in (
                    core.UsbState.ABSENT, core.UsbState.OFFLINE, core.UsbState.UNAUTHORIZED, core.UsbState.OTHER)
                self.missing_device_samples = self.missing_device_samples + 1 if missing else 0
                if self.proc and self.phase in ('ready', 'switching', 'active') and self.missing_device_samples >= 2:
                    self.stop(self.usb_status.message + '；恢复后请手动启动。', reason='disconnect')
            else:
                self.request_scan()
        self.operation_tick()
        if self.release_keys and not self.session_blocked and not win.held(self.release_keys):
            self.release_keys = ()
            self.bind_hotkey()
        if not self.proc:
            return
        try:
            self.capture_tick(time.monotonic())
        except Exception:
            self.capture_exception()

    def capture_tick(self, now):
        if self.proc.poll() is not None:
            cfg = self.applied_config
            if (self.proc.returncode and cfg and (cfg['mouse_mode'] == 'uhid' or
                    (cfg['keyboard'] and cfg['keyboard_mode'] == 'uhid'))):
                self.uhid_failure = core.classify_uhid_failure(core.session_log_tail(self.proc))
            self.stop(f'scrcpy 已退出（{self.proc.returncode}）；详见 scrcpy.log。', reason='proc_exit')
            return
        if self.phase == 'starting':
            if now >= self.deadline:
                self.stop('等待 scrcpy 窗口超时；详见 scrcpy.log。', reason='timeout')
            elif self.cap:
                if not win.owns_window(self.cap, self.proc.pid):
                    self.stop('scrcpy 窗口已失效。', reason='exception')
                elif not win.visible(self.cap):
                    self.set_phase('ready')
                    self.dwell = core.Dwell()
                    self.render()
            else:
                hwnd = win.find_window(self.proc.pid)
                if hwnd and win.owns_window(hwnd, self.proc.pid):
                    self.cap = hwnd
                    if win.visible(hwnd) and not self.hide_capture():
                        self.stop('未能隐藏 scrcpy 捕获窗口。', reason='exception')
        elif self.phase == 'switching':
            self.switch_tick(now)
        elif self.phase == 'parking':
            self.park_tick(now)
        elif self.phase == 'active':
            snap = win.capture_snapshot(self.cap, self.proc)
            if not snap.hwnd_valid:
                self.stop('捕获窗口已失效。', reason='exception' if snap.pid_alive else 'proc_exit', snapshot=snap)
            elif not snap.fg_before or not snap.fg_after:
                self.park('foreground_null', snapshot=snap)
            elif snap.fg_before != self.cap or snap.fg_after != self.cap:
                self.park('foreground_other', snapshot=snap)
            else:
                alt_down = win.held((0xa4,))
                if not snap.captured:
                    reason = 'alt_return' if self.alt_down and not alt_down else 'focus_same_clip_lost'
                    self.park(reason, snapshot=snap)
                self.alt_down = alt_down
        elif self.phase == 'ready' and not self.session_blocked:
            if self.config['auto']:
                if self.dwell.update(self.auto_edge(G.QCursor.pos()),
                                     now, self.config['dwell'] / 1000):
                    self.switch_in('edge_dwell')

    def render(self):
        labels = {'off': '未启动', 'starting': '正在连接…', 'ready': '等待切入',
                  'switching': '正在抓取…', 'active': '正在控制平板', 'parking': '正在释放…'}
        text = labels[self.phase]
        adb, usb = self.adb_status, self.usb_status
        fault = (self.config_error or self.last_error or
                 (adb and adb.state not in (core.AdbState.ABSENT, core.AdbState.READY)) or
                 (usb and usb.state in (core.UsbState.UNAUTHORIZED, core.UsbState.PROBE_ERROR)))
        badge = '需处理 · 未启动' if self.phase == 'off' and fault else text
        color = ('#83e0b2' if self.phase == 'active' else '#f4bd77' if self.phase == 'off' and fault
                 else '#afbecf' if self.phase == 'off' else '#91bdff')
        self.state.setText('●  ' + badge)
        self.state.setStyleSheet(f'color:{color};')
        self.tray.setToolTip(f'{APP} {core.APP_VERSION} · {text}')
        self.tray_status.setText(f'{APP} {core.APP_VERSION} · {text}')
        service_label = '停止服务' if self.proc else '启动平板键鼠服务'
        self.tray_service.setText(service_label)
        blocked = bool(self.actions_locked())
        ready = self.phase == 'ready' and not self.session_blocked
        active = self.phase == 'active'
        main_label = {'off': '启动服务', 'ready': '切入平板', 'active': '回电脑'}.get(self.phase, text)
        self.service.setText(main_label)
        self.service.setEnabled(ready or active or (self.phase == 'off' and not blocked))
        cancellable = bool(self.operation_task and self.operation_kind == 'start')
        self.stop_button.setVisible(bool(self.proc or self.stopping_proc or cancellable))
        self.stop_button.setText('取消启动' if cancellable else '停止')
        self.stop_button.setEnabled((cancellable and not self.cancel_start.is_set()) or bool(self.proc and not blocked))
        self.tray_service.setEnabled(not blocked)
        self.reset_adb.setEnabled(not blocked and not self.proc and
                                  bool(self.adb_status and self.adb_status.can_reset))
        self.kb_settings_btn.setEnabled(bool(self.can_open_kb_settings()))
        self.kb_settings_btn.setToolTip('平板需已连接并启用实体键盘 UHID；请先回到电脑再打开。')
        help_visible = bool(self.uhid_failure)
        self.uhid_reason.setText(self.uhid_message())
        self.uhid_reason.setToolTip('权限受限；部分机型需在开发者选项打开安全调试。'
                                    if self.uhid_failure == 'permission' else self.uhid_message())
        self.uhid_reason.setVisible(help_visible)
        self.open_kb_help_btn.setVisible(help_visible)
        self.open_kb_help_btn.setEnabled(bool(self.can_open_kb_settings()))
        self.use_sdk_btn.setVisible(help_visible)
        self.use_sdk_btn.setEnabled(not blocked and self.phase == 'off' and not self.proc)
        self.tray_enter.setEnabled(ready or active)
        self.tray_enter.setText('回电脑' if active else '切入平板')
        editable = not (self.quitting or self.read_only or self.missing)
        self.keyboard.setEnabled(editable)
        self.keyboard_mode.setEnabled(editable)
        self.mouse_mode.setEnabled(editable)
        self.tray_keyboard.setEnabled(editable)
        self.protect.setEnabled(editable)
        transport = self.selected_device.transport if self.selected_device else ''
        suffix = TRANSPORT_NAMES.get(transport, '')
        self.version_badge.setText(core.APP_VERSION + (f' · {suffix}' if suffix else ''))
        usb_direct = transport == 'usb'
        for combo, key in ((self.keyboard_mode, 'keyboard_mode'), (self.mouse_mode, 'mouse_mode')):
            combo.model().item(combo.findData('aoa')).setEnabled(usb_direct)
            combo.setPlaceholderText('AOA 需要 USB')
            combo.setToolTip('USB 直连 AOA（实验）可用' if usb_direct else 'AOA 需要 USB')
            requested = self.config[key]
            combo.setCurrentIndex(-1 if requested == 'aoa' and not usb_direct else combo.findData(requested))
        self.aoa_hint.setVisible(bool(self.selected_device and not usb_direct and self.config['keyboard_mode'] != 'aoa'))
        with Q.QSignalBlocker(self.device_picker):
            self.device_picker.clear()
            for candidate in self.device_candidates:
                kind = TRANSPORT_NAMES.get(candidate.transport, '未知连接')
                short_serial = candidate.serial if len(candidate.serial) <= 10 else '…' + candidate.serial[-9:]
                label = f'{kind} · {candidate.model or "未知型号"} · {short_serial}'
                if candidate.state != core.DeviceState.DEVICE.value:
                    label += ' · 未就绪'
                self.device_picker.addItem(label, candidate.serial)
            self.device_picker.setCurrentIndex(self.device_picker.findData(self.target_serial))
        self.device_picker.setToolTip(self.device_picker.currentText())
        self.device_picker.setVisible(bool(self.device_candidates))
        target_editable = self.phase == 'off' and not blocked and not self.proc and not self.stopping_proc
        self.device_picker.setEnabled(bool(target_editable))
        self.refresh_devices_button.setEnabled(bool(target_editable))
        entry = []
        if self.config['bar']:
            entry.append('左边条' if self.config['side'] == 'left' else '右边条')
        if self.config['hotkey']:
            entry.append(self.config['key'])
        self.methods_toggle.setText('切入方式' + (' · ' + ' / '.join(entry) if entry else ''))
        adb_names = {core.AdbState.ABSENT: 'ADB 未启动', core.AdbState.READY: 'ADB 就绪',
                     core.AdbState.MISMATCH: 'ADB 版本不匹配', core.AdbState.UNRESPONSIVE: 'ADB 无响应',
                     core.AdbState.BUSY_UNKNOWN: 'ADB 占用不明'}
        host = adb_names[adb.state] if adb else 'ADB 未检查' if self.read_only else '正在检查 ADB…'
        tablet = {core.UsbState.ABSENT: '目标离线' if self.target_serial else
                  '请选择平板' if self.device_candidates else '无设备', core.UsbState.OFFLINE: '目标离线',
                  core.UsbState.DEVICE: '', core.UsbState.UNAUTHORIZED: '未授权', core.UsbState.OTHER: '未就绪',
                  core.UsbState.PROBE_ERROR: '读取失败'}[usb.state] if usb else ''
        if self.last_error.startswith('有其他会话占用') and usb and usb.state is core.UsbState.DEVICE:
            tablet = '有其他会话占用（上次启动）'
        self.device.setText(f'{host} · {tablet}' if tablet else host)
        hint = ('点「启动服务」时才会唤起 ADB。' if adb and adb.state is core.AdbState.ABSENT else '')
        if self.uhid_failure == 'permission':
            hint = '部分机型需在开发者选项打开安全调试；也可手动改用 SDK。'
        self.message.setText(hint if self.uhid_failure == 'permission' else
                             self.note or self.config_error or self.screen_note or hint or '关闭窗口后仍会在托盘运行。')
        if ready and self.config['bar'] and self.position_bar():
            self.bar.show()
        else:
            self.bar.hide()

    def cleanup(self):
        if self.quitting:
            return
        self.quitting = True
        self.generation += 1
        self.cancel_start.set()
        self.failures.flush(time.monotonic(), force=True)
        self.scan_timer.stop()
        self.timer.stop()
        self.bind_hotkey()
        if self.session_registered:
            win.session_notifications(int(self.winId()), False)
        if self.scan_task:
            self.scan_task.cancel()
        if self.operation_task and self.operation_kind == 'start':
            # Queue even if the start is already done: a done callback could run on Qt's thread.
            task = self.operation_task
            task.cancel()
            self.executor.submit(self.reap_unclaimed, task)
        try:
            self.stop()
        finally:
            self.bar.hide()
            self.tray.hide()
            self.executor.shutdown(wait=False)

    @staticmethod
    def reap_unclaimed(task):
        if not task.cancelled() and task.exception() is None:
            proc = task.result()
            if proc is not None:
                try:
                    Panel.reap_process(proc)
                except Exception:
                    logging.exception('Failed to reap an unclaimed owned scrcpy')


def handle_existing_instance(lock, finder=None, asker=None, poster=win.post, shower=win.show, focuser=win.focus):
    finder = finder or (lambda: native.find_switcher_windows(APP, exclude_pid=os.getpid()))

    def locked_window():
        pid, *_ = lock.getLockInfo()
        matches = [(hwnd, info) for hwnd, info in finder() if info.pid == pid and info.started]
        return matches[0] if len(matches) == 1 else None

    target = locked_window()
    if not target:
        if asker is None:
            W.QMessageBox.information(None, APP, '切换器已经运行，请从右下角托盘打开。')
        return False
    hwnd, info = target
    if asker is None:
        box = W.QMessageBox()
        box.setIcon(W.QMessageBox.Icon.Question)
        box.setWindowTitle(f'{APP} {core.APP_VERSION}')
        box.setTextFormat(Q.Qt.TextFormat.PlainText)
        box.setText(f'检测到切换器已经在运行（PID {info.pid}）。\n\n'
                    f'是否关闭旧实例并立即启动当前 {core.APP_VERSION} 新版？\n'
                    '（仅重启切换器界面与键鼠转发，不会中断本机 ADB 服务）')
        switch_btn = box.addButton('关闭旧版并启动新版', W.QMessageBox.ButtonRole.AcceptRole)
        show_btn = box.addButton('显示原有窗口', W.QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton('取消', W.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        choice = 'replace' if clicked == switch_btn else ('show' if clicked == show_btn else 'cancel')
        box.deleteLater()
    else:
        choice = asker(hwnd, info)
    if choice not in ('show', 'replace'):
        return False
    if locked_window() != target:
        if asker is None:
            W.QMessageBox.warning(None, APP, '原实例已变化，请从右下角托盘退出后重试。')
        return False
    if choice == 'show':
        shower(hwnd, 9)  # SW_RESTORE
        focuser(hwnd)
        return False
    poster(hwnd, 0x0016, 1, 0)  # targeted WM_ENDSESSION -> graceful Qt quit
    end = time.monotonic() + 4.0
    while time.monotonic() < end:
        if lock.tryLock(0):
            return True
        time.sleep(0.1)
    if asker is None:
        W.QMessageBox.warning(None, APP, '旧实例未在 4 秒内退出，请先在右下角托盘右键「退出」后再试。')
    return False


def main():
    global CONFIG
    smoke = '--smoke-test' in sys.argv
    if smoke:
        original = CONFIG
        with TemporaryDirectory(prefix='tablet-switcher-smoke-') as temp:
            CONFIG = Path(temp) / 'config.json'
            try:
                return run_gui(smoke)
            finally:
                CONFIG = original
    return run_gui(smoke)


def run_gui(smoke):
    app = W.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    if not smoke:
        lock = Q.QLockFile(str(core.HOME / 'switcher.lock'))
        if not lock.tryLock(0) and not handle_existing_instance(lock):
            return 0
    panel = Panel(smoke, read_only=smoke, check_tools=True)
    missing = panel.missing
    app.aboutToQuit.connect(panel.cleanup)
    panel.show()
    if smoke:
        def finish():
            print(f'Qt window={panel.isVisible()} tray={panel.tray.isVisible()} '
                  f'system_tray={panel.tray.isSystemTrayAvailable()} paths_ok={not missing} '
                  f'config_ok={not panel.config_error} device={panel.device.text()}')
            app.exit(0 if panel.isVisible() and panel.tray.isVisible() and not (missing or panel.config_error) else 1)

        Q.QTimer.singleShot(0, finish)
    result = app.exec()
    # Keep the instance lock until queued child cleanup finishes. The UI event
    # loop has already exited, so this does not block input release or Qt ticks.
    panel.executor.shutdown(wait=True)
    return result


if __name__ == '__main__':
    if '--smoke-test' not in sys.argv:
        logging.basicConfig(handlers=[file_log_handler(core.HOME / 'switcher.log')], level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s')

    def report_error(kind, value, tb):
        logging.error('Unhandled error', exc_info=(kind, value, tb))
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = report_error
    sys.exit(main())
