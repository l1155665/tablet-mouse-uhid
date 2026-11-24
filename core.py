from dataclasses import dataclass, replace
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from tempfile import NamedTemporaryFile

import adb_windows as native
import win_input as win

HOME = Path(__file__).resolve().parent
APP_VERSION = 'v2.3'
ADB = Path(r'C:\leidian\LDPlayer14\adb.exe')  # compatibility fallback only
SCRCPY = HOME / 'scrcpy-win64-v5.0.1' / 'scrcpy.exe'  # discovery fallback only
ADB_PORT = 5037
LOOPBACK = '127.0.0.1'
CONNECT_TIMEOUT = 2.5
IO_TIMEOUT = 1.0
START_TIMEOUT = 8.0
USB_TIMEOUT = 10.0
LOG_LIMIT = 2 * 1024 * 1024
LOG = logging.getLogger(__name__)
KEYBOARD_MODES = ('uhid', 'sdk', 'aoa')
MOUSE_MODES = ('uhid', 'sdk', 'aoa')
HOT_KEYS = {'F9': (0, 0x78), 'Ctrl+Shift+M': (0x0006, 0x4D)}
RANGES = {'position': (0, 100), 'span': (5, 100), 'dwell': (80, 1000)}
DEFAULTS = dict(schema_version=2, selected_serial='', adb_path='', scrcpy_path='',
                devices={}, bar=True, auto=False, hotkey=True, key='F9', return_key=False,
                keyboard=False, keyboard_mode='uhid', mouse_mode='uhid', protect=True,
                side='right', position=50, span=35, dwell=120, screen='')


class DeviceState(Enum):
    DEVICE = 'device'
    OFFLINE = 'offline'
    UNAUTHORIZED = 'unauthorized'
    NO_PERMISSIONS = 'no_permissions'
    BOOTLOADER = 'bootloader'
    RECOVERY = 'recovery'
    OTHER = 'other'
    PROBE_ERROR = 'probe_error'
    ABSENT = 'absent'


@dataclass(frozen=True)
class DeviceCandidate:
    serial: str
    state: str
    transport: str = 'unknown'
    model: str = ''
    manufacturer: str = ''
    product: str = ''
    usb_path: str = ''
    address: str = ''
    is_emulator: bool = False
    last_seen: float = 0.0


def _transport(serial, fields):
    if any(field.startswith('usb:') for field in fields):
        return 'usb'
    if re.fullmatch(r'(?:[^:\s]+|\[[0-9a-fA-F:.%\w-]+\]):[0-9]+', serial):
        return 'tcpip'
    if any(field.startswith(('mdns:', 'service:')) for field in fields) or '_adb-tls-' in serial:
        return 'mdns'
    return 'unknown'


def parse_devices_l(text, *, probe_error=False, include_emulators=False):
    """Parse host:devices-l without discarding diagnostic states or metadata."""
    if probe_error or text is None:
        return [DeviceCandidate('', DeviceState.PROBE_ERROR.value)]
    if not text or not text.strip():
        return [DeviceCandidate('', DeviceState.ABSENT.value)]
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].lower() == 'list':
            continue
        serial, raw_state, fields = parts[0], parts[1].lower(), parts[2:]
        if raw_state == 'no' and fields and fields[0].lower() == 'permissions':
            raw_state, fields = 'no_permissions', fields[1:]
        emulator = serial.startswith('emulator-')
        if emulator and not include_emulators:
            continue
        state = raw_state.replace(' ', '_')
        if state not in {item.value for item in DeviceState}:
            state = DeviceState.OTHER.value
        attrs = {}
        for field in fields:
            if ':' in field:
                key, value = field.split(':', 1)
                attrs[key] = value
        if serial in found:
            LOG.warning('duplicate ADB serial %s; using last record', serial)
        found[serial] = DeviceCandidate(
            serial, state, _transport(serial, fields), attrs.get('model', ''),
            attrs.get('manufacturer', ''), attrs.get('product', ''), attrs.get('usb', ''),
            serial if ':' in serial else '', emulator, time.time())
    return list(found.values()) or [DeviceCandidate('', DeviceState.ABSENT.value)]


def select_target_device(candidates, config=None):
    config = config or {}
    usable = [item for item in candidates if item.state == DeviceState.DEVICE.value and not item.is_emulator]
    if not usable:
        return None
    selected = config.get('selected_serial', '')
    if selected:
        return next((item for item in usable if item.serial == selected), None)
    if len(usable) == 1:
        return usable[0]
    recent = [item for item in usable if config.get('devices', {}).get(item.serial, {}).get('last_success_at')]
    return recent[0] if len(recent) == 1 else None


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError('config.json 必须为 JSON 对象')
    cfg = DEFAULTS | data
    for key, default in DEFAULTS.items():
        if type(cfg[key]) is not type(default):
            raise ValueError(f'{key} 类型不正确')
    for key, values in {'side': ('left', 'right'), 'keyboard_mode': KEYBOARD_MODES,
                        'mouse_mode': MOUSE_MODES, 'key': HOT_KEYS}.items():
        if cfg[key] not in values:
            raise ValueError(f'{key} 不在支持的选项中')
    for key, (low, high) in RANGES.items():
        if not low <= cfg[key] <= high:
            raise ValueError(f'{key} 必须在 {low}–{high} 之间')
    screen = cfg['screen']
    if len(screen) > 256 or (screen and (screen.strip() != screen or not screen.isprintable())):
        raise ValueError('screen 显示器标识无效')
    if cfg['schema_version'] != 2 or not all(isinstance(k, str) and isinstance(v, dict)
                                             for k, v in cfg['devices'].items()):
        raise ValueError('invalid schema_version/devices')
    return cfg


def load_config(path):
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise ValueError('config.json must be an object')
        migrated = dict(raw)
        for key, value in DEFAULTS.items():
            migrated.setdefault(key, value.copy() if isinstance(value, dict) else value)
        return validate_config(migrated)
    except FileNotFoundError:
        return DEFAULTS.copy()


def save_config(path, data, *, backup_invalid=False):
    text = json.dumps(validate_config(data), ensure_ascii=False, indent=2) + '\n'
    temp = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(text)
        if backup_invalid and path.exists():
            original = path.read_bytes()
            backup = path.with_suffix(path.suffix + '.bak')
            try:
                with backup.open('xb') as file:
                    file.write(original)
            except FileExistsError:
                pass
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def parse_devices(text):
    return {item.serial: item.model or item.serial for item in parse_devices_l(text)
            if item.state == DeviceState.DEVICE.value and item.serial}


@dataclass(frozen=True)
class Toolchain:
    adb: Path
    scrcpy: Path


def _resolve_tool(config, key, name, relative, common):
    explicit = str(config.get(key, '') or '').strip()
    candidates = ([Path(explicit)] if explicit else [])
    candidates += [HOME / relative, HOME / 'platform-tools' / name, HOME / 'scrcpy' / name]
    candidates += [Path(item) for item in common]
    candidates += [Path(item) / name for item in os.environ.get('PATH', '').split(os.pathsep) if item]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SwitcherError(f'找不到 {name}；请在 config.json 指定 {key}。')


def resolve_adb_path(config=None):
    return _resolve_tool(config or {}, 'adb_path', 'adb.exe', 'adb.exe',
                          [ADB])


def resolve_scrcpy_path(config=None):
    return _resolve_tool(config or {}, 'scrcpy_path', 'scrcpy.exe', 'scrcpy.exe',
                          [SCRCPY])


def resolve_toolchain(config=None):
    return Toolchain(resolve_adb_path(config), resolve_scrcpy_path(config))


class SwitcherError(OSError):
    """Error with a user-facing Chinese message."""


class AdbError(SwitcherError):
    messages = {'connect': '连不上本机 ADB', 'write': '向本机 ADB 发送请求失败',
                'read': '未收到本机 ADB 的完整回复', 'protocol': '本机 ADB 的回复格式不正确',
                'rejected': '本机 ADB 拒绝了请求'}

    def __init__(self, stage, detail=''):
        super().__init__(self.messages[stage] + '；请检查连接后重试。')
        self.stage, self.detail = stage, detail


def remaining(deadline):
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError
    return budget


def adb_frame(text):
    payload = text.encode('ascii')
    return f'{len(payload):04x}'.encode('ascii') + payload


def adb_query(command, serial=None, *, port=ADB_PORT, deadline=None, owner=None):
    # Raw queries are read-only and cannot implicitly spawn or replace a server.
    deadline = float('inf') if deadline is None else deadline
    try:
        sock = socket.create_connection((LOOPBACK, port), min(CONNECT_TIMEOUT, remaining(deadline)))
    except OSError as exc:
        raise AdbError('connect', repr(exc)) from exc
    with sock:
        if owner is not None and (native.peer_pid(sock) != owner.pid or native.owner(owner.pid) != owner):
            raise AdbError('protocol', 'ADB peer changed')
        io_end = min(deadline, time.monotonic() + IO_TIMEOUT)

        def request(text):
            try:
                sock.settimeout(remaining(io_end))
                sock.sendall(adb_frame(text))
            except OSError as exc:
                raise AdbError('write', repr(exc)) from exc

        def read(size):
            data = bytearray()
            try:
                while len(data) < size:
                    sock.settimeout(remaining(io_end))
                    chunk = sock.recv(size - len(data))
                    if not chunk:
                        raise EOFError('ADB closed its socket')
                    data.extend(chunk)
            except (OSError, EOFError) as exc:
                raise AdbError('read', repr(exc)) from exc
            return bytes(data)

        def response(transport=False):
            status = read(4)
            if status not in (b'OKAY', b'FAIL'):
                raise AdbError('protocol', repr(status))
            if status == b'OKAY' and transport:
                return ''
            size = read(4)
            if not re.fullmatch(rb'[0-9a-fA-F]{4}', size):
                raise AdbError('protocol', repr(size))
            data = read(int(size, 16)).decode('utf-8', errors='replace')
            if status == b'FAIL':
                raise AdbError('rejected', data)
            return data

        if serial:
            request(f'host:transport:{serial}')
            response(transport=True)
        request(command)
        return response()


class AdbState(Enum):
    ABSENT = '本机 ADB 未启动；点「启动平板键鼠服务」时将自动唤起。'
    READY = '本机 ADB 已就绪'
    MISMATCH = '本机 ADB 版本不匹配；未替换。可在原软件中处理，或手动重置。'
    UNRESPONSIVE = '本机 ADB 无响应；未重启。可先检查原软件，或手动重置。'
    BUSY_UNKNOWN = '无法确认本机 ADB 端口可用；未启动。请先检查占用。'


@dataclass(frozen=True)
class AdbProbe:
    state: AdbState
    owners: tuple = ()
    version: str = ''
    expected: str = ''

    @property
    def occupant(self):
        return '、'.join(f'{owner.name}（PID {owner.pid}）' for owner in self.owners)

    @property
    def message(self):
        pieces = [self.state.value]
        if self.state is AdbState.MISMATCH:
            pieces.append(f'当前 {self.version}，客户端需要 {self.expected or "—"}。')
        if self.occupant:
            pieces.append(f'占用：{self.occupant}')
        return ' '.join(pieces)

    @property
    def can_reset(self):
        return (self.state in (AdbState.MISMATCH, AdbState.UNRESPONSIVE, AdbState.BUSY_UNKNOWN)
                and len(self.owners) == 1 and self.owners[0].is_adb)


def probe_adb(port=ADB_PORT, *, deadline=None, expected=None):
    """Five-state, read-only probe. A missing/unstable snapshot never grants start permission."""
    version = ''
    error = None
    try:
        before = native.listener_pids(port)
        original = tuple(native.owner(pid) for pid in sorted(before))
        if original and (len(original) != 1 or not original[0].is_adb):
            return AdbProbe(AdbState.BUSY_UNKNOWN, original)
        try:
            version = adb_query('host:version', port=port, deadline=deadline,
                                owner=original[0] if original else None).strip().lower()
            if not re.fullmatch(r'[0-9a-f]{4}', version):
                raise AdbError('protocol', repr(version))
        except AdbError as exc:
            error = exc
            LOG.debug('ADB probe port=%d stage=%s', port, exc.stage)
        after = native.listener_pids(port)
        owners = tuple(native.owner(pid) for pid in sorted(after))
        if original != owners:
            return AdbProbe(AdbState.BUSY_UNKNOWN, owners)
        if not error and owners:
            state = AdbState.MISMATCH if expected and version != expected else AdbState.READY
            return AdbProbe(state, owners, version, expected or '')
        if (not owners and error and error.stage == 'connect'
                and isinstance(error.__cause__, (ConnectionRefusedError, TimeoutError))
                and not native.adb_process_exists()):
            return AdbProbe(AdbState.ABSENT)
        if (owners and owners[0].is_adb and error and
                error.stage in ('connect', 'write', 'read')):
            return AdbProbe(AdbState.UNRESPONSIVE, owners)
        return AdbProbe(AdbState.BUSY_UNKNOWN, owners)
    except OSError:
        LOG.warning('Cannot inspect ADB listener on port %d', port, exc_info=True)
        return AdbProbe(AdbState.BUSY_UNKNOWN)


def adb_environment():
    return {key: value for key, value in os.environ.items() if key.upper() not in {
        'ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT'}}


def adb_client_protocol(path, *, deadline=None):
    """Read the local executable's protocol without contacting its server."""
    try:
        result = subprocess.run([str(path), 'version'], cwd=path.parent, env=adb_environment(),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=min(2, remaining(deadline)) if deadline is not None else 2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwitcherError('无法读取 ADB 客户端版本；未使用共享服务。') from exc
    match = re.search(r'^Android Debug Bridge version 1\.0\.(\d{1,5})\s*$', result.stdout, re.M)
    if result.returncode or not match or not 0 < int(match[1]) <= 0xffff:
        raise SwitcherError('ADB 客户端版本无法识别；未使用共享服务。')
    return f'{int(match[1]):04x}'


def require_compatible_adb(port, expected, *, deadline=None):
    status = probe_adb(port, deadline=deadline, expected=expected)
    if status.state is not AdbState.READY:
        raise SwitcherError(status.message)
    return status


def spawn_adb(port, *, adb_path=None):
    # Direct server mode performs no client handshake and never replaces an incumbent.
    adb_path = adb_path or resolve_adb_path()
    try:
        native.rotate_idle_log(HOME / 'adb-start.log', LOG_LIMIT)
        with (HOME / 'adb-start.log').open('ab') as log:
            # ADB host uses loopback for tcp:<port> when -a is omitted.
            return subprocess.Popen([str(adb_path), '-L', f'tcp:{port}', 'server', 'nodaemon'],
                                    cwd=adb_path.parent, env=adb_environment(), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        LOG.warning('Cannot launch ADB', exc_info=True)
        raise SwitcherError('无法启动 ADB；请检查配置路径与文件权限。') from exc


def check_cancelled(cancelled):
    if cancelled():
        raise SwitcherError('已取消启动；共享 ADB 保持原状。')


def check_adb_budget(end, cancelled):
    check_cancelled(cancelled)
    if time.monotonic() >= end:
        raise SwitcherError('等待 ADB 就绪超时；请检查 ADB 状态或 adb-start.log。')


def ensure_adb_ready(port=ADB_PORT, *, adb_path=None, progress=lambda _: None, cancelled=lambda: False):
    end = time.monotonic() + START_TIMEOUT
    check_adb_budget(end, cancelled)
    adb_path = adb_path or resolve_adb_path()
    expected = adb_client_protocol(adb_path, deadline=end)
    check = probe_adb(port, deadline=end, expected=expected)
    check_adb_budget(end, cancelled)
    if check.state is AdbState.READY:
        return check
    if check.state is not AdbState.ABSENT:
        if check.state is AdbState.MISMATCH:
            progress(check)
        raise SwitcherError(check.state.value)
    progress('正在唤起 ADB…')
    # This second check is immediately before spawn. A later bind race is handled
    # by server nodaemon, not by adb.exe client auto-repair.
    try:
        occupied = native.adb_process_exists() or bool(native.listener_pids(port))
    except OSError as exc:
        raise SwitcherError('无法复核本机 ADB 端口；未尝试启动。') from exc
    if occupied:
        check = probe_adb(port, deadline=end, expected=expected)
        check_adb_budget(end, cancelled)
        if check.state is AdbState.READY:
            return check
        raise SwitcherError('复核时 ADB 端口状态已变化；未尝试启动。' + check.state.value)
    check_cancelled(cancelled)
    if time.monotonic() >= end:
        raise SwitcherError('检查 ADB 端口超时；未尝试启动。')
    proc = spawn_adb(port, adb_path=adb_path)
    while time.monotonic() < end and not cancelled():
        code = proc.poll()
        check = probe_adb(port, deadline=end, expected=expected)
        check_adb_budget(end, cancelled)
        if check.state is AdbState.READY:
            return check
        if check.state is AdbState.MISMATCH or (check.owners and
                                               any(o.pid != proc.pid for o in check.owners)):
            raise SwitcherError(check.state.value)
        if code is not None:
            raise SwitcherError(f'ADB 已退出（{code}），没有可复用的服务。详见 adb-start.log。')
        time.sleep(max(0, min(.2, end - time.monotonic())))
    # This is the server itself, not a startup client. Never terminate it here.
    raise SwitcherError('等待 ADB 就绪超时或已取消；请检查 ADB 状态或 adb-start.log。')


def request_adb_shutdown(port, confirmed):
    with socket.create_connection((LOOPBACK, port), .75) as sock:
        if (native.peer_pid(sock) != confirmed.pid or
                native.listener_pids(port) != {confirmed.pid} or native.owner(confirmed.pid) != confirmed):
            return
        sock.sendall(adb_frame('host:kill'))


def wait_adb_release(port, confirmed, timeout):
    end = time.monotonic() + timeout
    while True:
        pids = native.listener_pids(port)
        if not pids and not native.adb_process_exists():
            return True
        if pids and (pids != {confirmed.pid} or native.owner(confirmed.pid) != confirmed):
            raise SwitcherError('端口占用者已变化；已停止重置，请重新检查。')
        if time.monotonic() >= end:
            return False
        time.sleep(.1)


def reset_to_leidian_adb(confirmed, port=ADB_PORT, *, expected=None, progress=lambda _: None):
    """Explicitly confirmed only; never stop a different listener or a non-ADB process."""
    check = probe_adb(port, expected=expected)
    if not check.can_reset or check.owners != (confirmed,):
        raise SwitcherError('占用者已变化、不是已确认的 ADB，或服务已恢复；未关闭任何程序。'
                            '如被其他软件占用，请在原软件中正常退出。' + check.state.value)
    adb_client_protocol(ADB)
    try:
        # Keep this process object pinned across the graceful request and PID checks.
        with native.process_handle(confirmed.pid) as handle:
            if native.identify(confirmed.pid, handle) != confirmed:
                raise SwitcherError('占用者已变化；未关闭任何程序。')
            try:
                request_adb_shutdown(port, confirmed)
            except OSError:
                LOG.info('Graceful ADB shutdown request failed', exc_info=True)
            if not wait_adb_release(port, confirmed, 1.0):
                if native.listener_pids(port) == {confirmed.pid}:
                    with native.process_handle(confirmed.pid, allow_stop=True) as target:
                        if (native.identify(confirmed.pid, target) != confirmed or
                                native.listener_pids(port) != {confirmed.pid}):
                            raise SwitcherError('占用者已变化；未强制关闭任何程序。')
                        if not native.terminate(target, 1):
                            raise SwitcherError('没有权限关闭已确认的 ADB。')
                if not wait_adb_release(port, confirmed, 2.0):
                    raise SwitcherError('ADB 端口仍未释放。')
    except SwitcherError:
        raise
    except OSError as exc:
        LOG.warning('ADB reset could not release port %d', port, exc_info=True)
        raise SwitcherError('未能释放 ADB 端口；请检查当前占用，在原软件中退出后重试。') from exc
    return ensure_adb_ready(port, adb_path=ADB, progress=progress)


def scan(port=ADB_PORT, *, detailed=False, deadline=None):
    result = parse_devices_l(adb_query('host:devices-l', port=port, deadline=deadline))
    return result if detailed else {item.serial: item.model or item.serial for item in result
                                    if item.state == DeviceState.DEVICE.value and item.serial}


class UsbState(Enum):
    ABSENT = 'absent'
    OFFLINE = 'offline'
    UNAUTHORIZED = 'unauthorized'
    DEVICE = 'device'
    OTHER = 'other'
    PROBE_ERROR = 'probe_error'


@dataclass(frozen=True)
class UsbProbe:
    state: UsbState
    model: str = ''
    stage: str = ''
    transport: str = 'unknown'

    @property
    def message(self):
        if self.state is UsbState.DEVICE:
            return f'平板 {self.model} 已连接' if self.model else '平板已连接'
        return {
            UsbState.ABSENT: '请连接数据线',
            UsbState.OFFLINE: '平板暂未就绪',
            UsbState.UNAUTHORIZED: '在平板确认 USB 调试授权',
            UsbState.OTHER: '平板处于其他模式；请正常启动并解锁',
            UsbState.PROBE_ERROR: '平板状态读取失败；请重试',
        }[self.state]


def probe_usb(port=ADB_PORT, serial=None, *, deadline=None):
    if not serial:
        return UsbProbe(UsbState.ABSENT)
    try:
        text = adb_query('host:devices-l', port=port, deadline=deadline)
    except AdbError as exc:
        return UsbProbe(UsbState.PROBE_ERROR, stage=exc.stage)
    return target_status(parse_devices_l(text), serial)


def target_status(candidates, serial):
    for item in candidates:
        if item.state == DeviceState.PROBE_ERROR.value:
            return UsbProbe(UsbState.PROBE_ERROR)
        if serial and item.serial == serial and not item.is_emulator:
            states = {s.value: s for s in (UsbState.OFFLINE, UsbState.UNAUTHORIZED, UsbState.DEVICE)}
            state = states.get(item.state, UsbState.OTHER)
            return UsbProbe(state, item.model, transport=item.transport)
    return UsbProbe(UsbState.ABSENT)


def wait_for_usb(port=ADB_PORT, serial=None, *, progress=lambda _: None, cancelled=lambda: False, deadline=None):
    if not serial:
        raise SwitcherError('请先选择目标平板。')
    end = deadline if deadline is not None else time.monotonic() + USB_TIMEOUT
    delay, previous = .15, None
    status = UsbProbe(UsbState.PROBE_ERROR)
    while time.monotonic() < end:
        check_cancelled(cancelled)
        status = probe_usb(port, serial, deadline=end)
        check_cancelled(cancelled)
        budget = end - time.monotonic()
        if budget <= 0:
            break
        if status.state is UsbState.DEVICE and previous is UsbState.DEVICE:
            return status
        if status.state in (UsbState.UNAUTHORIZED, UsbState.OTHER):
            raise SwitcherError(f'{status.message}，然后重新点「启动服务」。')
        if status.state != previous:
            progress('正在复核平板连接…' if status.state is UsbState.DEVICE else status.message + '…')
        previous = status.state
        time.sleep(min(delay, budget))
        delay = min(.55, delay * 1.5)
    check_cancelled(cancelled)
    advice = status.message if status.state is not UsbState.DEVICE else '请重试'
    raise SwitcherError(f'等待平板就绪超时；{advice}。')


def preflight(serial=None, port=ADB_PORT, *, deadline=None, cancelled=lambda: False):
    if not serial:
        raise SwitcherError('must select an ADB device before starting')
    def query(command, serial=None):
        check_cancelled(cancelled)
        return adb_query(command, serial, port=port, deadline=deadline)

    check_cancelled(cancelled)
    version = query('host:version').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{4}', version):
        raise SwitcherError('invalid ADB host protocol response')
    usb = probe_usb(port, serial, deadline=deadline)
    if usb.state is not UsbState.DEVICE:
        raise SwitcherError(usb.message + '。')
    forwards = query('host:list-forward')
    if any(line.split() and line.split()[0] == serial for line in forwards.splitlines()):
        raise SwitcherError('有其他会话占用：平板已有 ADB 转发。请先正常退出其他共享会话。')
    if query('reverse:list-forward', serial).strip():
        raise SwitcherError('有其他会话占用：平板已有反向 ADB 转发。请先正常退出其他共享会话。')
    return usb


def resource_diff(before, after):
    """Return observed additions; a difference is not proof of ownership."""
    return set(after or ()) - set(before or ())


def forward_diff(before, after):
    return resource_diff(before, after)


def reverse_diff(before, after):
    return resource_diff(before, after)


def command(config, width=120, height=36, *, serial=None, scrcpy_path=None, initial=None):
    serial = serial or config.get('selected_serial', '')
    if not serial:
        raise SwitcherError('must select an ADB device before starting')
    kb_mode = config.get('keyboard_mode', 'uhid')
    if kb_mode not in KEYBOARD_MODES:
        kb_mode = 'uhid'
    mouse_mode = config.get('mouse_mode', 'uhid')
    if mouse_mode == 'aoa' and config.get('transport') != 'usb':
        raise SwitcherError('鼠标 AOA 需要 USB 直连设备')
    if config.get('keyboard') and kb_mode == 'aoa' and config.get('transport') != 'usb':
        raise SwitcherError('键盘 AOA 需要 USB 直连设备')
    args = [str(scrcpy_path or resolve_scrcpy_path(config)), '--serial', serial, '--no-video', '--no-audio',
            '--mouse=' + mouse_mode,
            '--keyboard=' + (kb_mode if config['keyboard'] else 'disabled'),
            '--no-clipboard-autosync', '--shortcut-mod=lalt', '--window-title', win.TITLE,
             '--window-borderless', f'--window-width={width}', f'--window-height={height}']
    args.append('--background-color=#1C2736')
    if initial is not None:
        args.extend((f'--window-x={initial[0]}', f'--window-y={initial[1]}'))
    # UHID already defaults to ++++:bhsn in scrcpy 5.0.1; make both choices explicit.
    args.append('--mouse-bind=' + ('++++:bhsn' if config['protect'] else 'bhsn:++++'))
    return args


def open_tablet_keyboard_settings(serial=None, port=ADB_PORT, *, adb_path=None, config=None):
    if not serial:
        raise SwitcherError('must select an ADB device before opening keyboard settings')
    config = config or {}
    adb_path = Path(adb_path or resolve_adb_path(config))
    expected = adb_client_protocol(adb_path)
    status = require_compatible_adb(port, expected)
    if serial not in scan(port):
        raise SwitcherError(f'未检测到已授权的平板 {serial}；请先用数据线连接并确认 USB 调试授权。')
    if require_compatible_adb(port, expected).owners != status.owners:
        raise SwitcherError('ADB 监听者已变化；未打开平板设置。')
    env = dict(adb_environment(), ADB_SERVER_SOCKET=f'tcp:{LOOPBACK}:{port}')
    try:
        res = subprocess.run(
            [str(adb_path), '-s', serial, 'shell', 'am', 'start', '-a', 'android.settings.HARD_KEYBOARD_SETTINGS'],
            cwd=adb_path.parent, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=6, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning('Failed to open tablet keyboard settings', exc_info=True)
        raise SwitcherError('打开平板物理键盘设置失败；请在平板「设置 → 系统 → 语言和输入法 → 实体键盘」手动设置。') from exc
    if res.returncode != 0 or re.search(r'(?im)^\s*(?:error\b|exception\b)', res.stdout + '\n' + res.stderr):
        raise SwitcherError('平板未能打开实体键盘设置；请手动打开「设置 → 系统 → 语言和输入法 → 实体键盘」。')
    return '已在平板打开实体键盘设置页；请确认或选择键盘布局后重试打字。'


def classify_uhid_failure(log_text='', returncode=None):
    text = (log_text or '').casefold()
    if any(token in text for token in ('keyboard layout', 'physical keyboard', 'hard keyboard')):
        return 'layout'
    if any(token in text for token in ('offline', 'unauthorized', 'connection reset', 'broken pipe')):
        return 'connection'
    if any(token in text for token in ('permission denied', 'selinux')):
        return 'permission'
    if any(token in text for token in ('/dev/uhid', 'uhid')):
        return 'unsupported'
    return 'unknown'


def session_log_tail(proc):
    log_ref = getattr(proc, 'switcher_log', None)
    if not isinstance(log_ref, tuple):
        return ''
    path, offset = log_ref
    try:
        with path.open('rb') as log:
            end = log.seek(0, os.SEEK_END)
            log.seek(max(offset, end - 8192))
            return log.read(max(0, end - log.tell())).decode('utf-8', errors='replace')
    except OSError:
        return ''


def validate_input_mode(mode, transport):
    if mode not in MOUSE_MODES:
        raise ValueError(f'unsupported input mode: {mode}')
    if mode == 'aoa' and transport != 'usb':
        raise ValueError('AOA requires USB transport')
    return mode


def launch(config, log_path=HOME / 'scrcpy.log', width=120, height=36, *, serial=None,
           progress=lambda _: None, cancelled=lambda: False, port=ADB_PORT, initial=None, deadline=None):
    check_cancelled(cancelled)
    serial = serial or config.get('selected_serial', '')
    if not serial:
        raise SwitcherError('请先选择目标平板。')
    adb_path = resolve_adb_path(config)
    scrcpy_path = resolve_scrcpy_path(config)
    expected = adb_client_protocol(adb_path, deadline=deadline)
    status = require_compatible_adb(port, expected, deadline=deadline)
    target = preflight(serial, port, deadline=deadline, cancelled=cancelled)
    check_cancelled(cancelled)
    progress('正在启动服务…')
    args = command(dict(config, transport=target.transport), width, height, serial=serial,
                   scrcpy_path=scrcpy_path, initial=initial)
    env = dict(adb_environment(), ADB=str(adb_path), ADB_SERVER_SOCKET=f'tcp:{LOOPBACK}:{port}',
               SDL_WINDOW_ACTIVATE_WHEN_SHOWN='0', SDL_WINDOWS_DPI_AWARENESS='permonitorv2')
    try:
        native.rotate_idle_log(log_path, LOG_LIMIT)
        with log_path.open('ab') as log:
            # The external client still performs its own handshake; this check is not atomic with it.
            check_cancelled(cancelled)
            if require_compatible_adb(port, expected, deadline=deadline).owners != status.owners:
                raise SwitcherError('启动前发现 ADB 监听者已变化；未启动平板键鼠服务。')
            check_cancelled(cancelled)
            if deadline is not None and time.monotonic() >= deadline:
                raise SwitcherError('启动前复核平板连接超时；请重试。')
            offset = log.tell()
            proc = subprocess.Popen(args, cwd=scrcpy_path.parent, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            proc.switcher_log = log_path, offset
            return proc
    except SwitcherError:
        raise
    except OSError as exc:
        LOG.warning('Cannot launch scrcpy', exc_info=True)
        raise SwitcherError('无法启动平板键鼠服务；请检查程序路径和日志文件权限。') from exc


def stop_process(proc, hwnd=None):
    """Reap only an owned Popen; call from a worker, not the UI thread."""
    if proc.poll() is not None:
        proc.wait()
        return
    if win.owns_window(hwnd, proc.pid) and proc.poll() is None and win.post(hwnd, 0x0010, 0, 0):
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
    proc.terminate()
    proc.wait(timeout=2)


def edge_region(rect, side, position, span, width):
    x, y, w, h = rect
    size = max(1, h * span // 100)
    top = min(y + h - size, max(y, y + h * position // 100 - size // 2))
    return x if side == 'left' else x + w - width, top, width, size


class Dwell:
    def __init__(self):
        self.started = None
        self.latched = True  # first leave, including after returning from tablet

    def update(self, inside, now, delay):
        if not inside:
            self.started, self.latched = None, False
        elif not self.latched:
            if self.started is None:
                self.started = now
            elif now - self.started >= delay:
                self.latched = True
                return True
        return False
