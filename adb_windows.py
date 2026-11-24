"""Read Windows TCP owners and pin a confirmed process by handle for ADB reset."""
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import socket
import os


def api(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


k = C.WinDLL('kernel32', use_last_error=True)
D = W.DWORD
PD = C.POINTER(D)
get_table = api(C.WinDLL('iphlpapi'), 'GetExtendedTcpTable', D,
                C.c_void_p, PD, W.BOOL, W.ULONG, C.c_int, W.ULONG)
open_process = api(k, 'OpenProcess', W.HANDLE, D, W.BOOL, D)
close_handle = api(k, 'CloseHandle', W.BOOL, W.HANDLE)
query_image = api(k, 'QueryFullProcessImageNameW', W.BOOL, W.HANDLE, D, W.LPWSTR, PD)
get_times = api(k, 'GetProcessTimes', W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)] * 4))
terminate = api(k, 'TerminateProcess', W.BOOL, W.HANDLE, W.UINT)
create_snapshot = api(k, 'CreateToolhelp32Snapshot', W.HANDLE, D, D)
create_file = api(k, 'CreateFileW', W.HANDLE, W.LPCWSTR, D, D, C.c_void_p, D, D, W.HANDLE)


def rotate_idle_log(path, limit):
    # Deny existing/new writers while renaming; allow the rename itself (share delete).
    handle = create_file(str(path), 0x80, 0x4, None, 3, 0, None)  # READ_ATTRIBUTES, OPEN_EXISTING
    if handle == C.c_void_p(-1).value:
        return False
    try:
        if path.stat().st_size >= limit:
            os.replace(path, path.with_suffix(path.suffix + '.1'))
            return True
    except OSError:
        return False  # Keep the original if any handle or filesystem rule prevents renaming.
    finally:
        close_handle(handle)
    return False


class ProcessEntry(C.Structure):
    _fields_ = [('size', D), ('usage', D), ('pid', D), ('heap', C.c_size_t),
                ('module', D), ('threads', D), ('parent', D), ('priority', W.LONG),
                ('flags', D), ('name', W.WCHAR * 260)]


process_first = api(k, 'Process32FirstW', W.BOOL, W.HANDLE, C.POINTER(ProcessEntry))
process_next = api(k, 'Process32NextW', W.BOOL, W.HANDLE, C.POINTER(ProcessEntry))


def adb_process_exists():
    handle = create_snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        row = ProcessEntry(size=C.sizeof(ProcessEntry))
        ok = process_first(handle, C.byref(row))
        while ok:
            if row.name.casefold() == 'adb.exe':
                return True
            ok = process_next(handle, C.byref(row))
        if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise C.WinError(C.get_last_error())
        return False
    finally:
        close_handle(handle)


@dataclass(frozen=True)
class Owner:
    pid: int
    path: str = ''
    started: int = 0

    @property
    def name(self):
        return Path(self.path).name if self.path else '进程名未知'

    @property
    def is_adb(self):
        return self.name.casefold() == 'adb.exe' and bool(self.started)


@contextmanager
def process_handle(pid, allow_stop=False):
    handle = open_process(0x1000 | int(allow_stop), False, pid)  # QUERY_LIMITED_INFORMATION, TERMINATE
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        yield handle
    finally:
        close_handle(handle)


def identify(pid, handle):
    path = C.create_unicode_buffer(32768)
    size = D(len(path))
    times = [W.FILETIME() for _ in range(4)]
    if (not query_image(handle, 0, path, C.byref(size)) or
            not get_times(handle, *(C.byref(t) for t in times))):
        raise C.WinError(C.get_last_error())
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return Owner(pid, path.value, created)


def owner(pid):
    try:
        with process_handle(pid) as handle:
            return identify(pid, handle)
    except OSError:
        return Owner(pid)


class Tcp4(C.Structure):
    _fields_ = [(field, D) for field in ('state', 'local', 'port', 'remote', 'remote_port', 'pid')]


class Tcp6(C.Structure):
    _fields_ = [('local', C.c_ubyte * 16), ('scope', D), ('port', D),
                ('remote', C.c_ubyte * 16), ('remote_scope', D), ('remote_port', D),
                ('state', D), ('pid', D)]


def tcp_rows(family, table_class):
    size = D(16 * 1024)
    for _ in range(4):
        buf = C.create_string_buffer(size.value)
        code = get_table(buf, C.byref(size), False, family, table_class, 0)
        if code == 0:
            n = D.from_buffer(buf).value
            row = Tcp4 if family == socket.AF_INET else Tcp6
            return (row * n).from_buffer(buf, C.sizeof(D))
        if code != 122:  # ERROR_INSUFFICIENT_BUFFER
            break
    raise C.WinError(code)


LOOPBACK = int.from_bytes(socket.inet_aton('127.0.0.1'), 'little')


def listener_pids(port):
    wire_port = socket.htons(port)
    pids = {row.pid for row in tcp_rows(socket.AF_INET, 3)  # TCP_TABLE_OWNER_PID_LISTENER
            if row.port == wire_port and row.local in (0, LOOPBACK)}
    v6 = tuple(socket.inet_pton(socket.AF_INET6, addr) for addr in ('::', '::ffff:127.0.0.1'))
    pids.update(row.pid for row in tcp_rows(socket.AF_INET6, 3)
                if row.port == wire_port and bytes(row.local) in v6)
    return pids


def peer_pid(sock):
    # Match the server half of this established TCP connection, not just a listener
    # which might have been replaced after connect(). A proxy/unknown peer is refused.
    port = socket.htons(sock.getpeername()[1])
    remote_port = socket.htons(sock.getsockname()[1])
    pids = {row.pid for row in tcp_rows(socket.AF_INET, 4)  # OWNER_PID_CONNECTIONS
            if row.state == 5 and row.port == port and row.remote_port == remote_port
            and row.local == row.remote == LOOPBACK}
    return pids.pop() if len(pids) == 1 else None


def find_switcher_windows(title_prefix, exclude_pid=None):
    """Return [(hwnd, owner)] for existing switcher GUI instances belonging to Python."""
    u = C.WinDLL('user32', use_last_error=True)
    CALLBACK = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    enum_win = api(u, 'EnumWindows', W.BOOL, CALLBACK, W.LPARAM)
    get_tid_pid = api(u, 'GetWindowThreadProcessId', D, W.HWND, PD)
    get_text = api(u, 'GetWindowTextW', C.c_int, W.HWND, W.LPWSTR, C.c_int)
    results = []

    @CALLBACK
    def visit(hwnd, _):
        buf = C.create_unicode_buffer(256)
        get_text(hwnd, buf, len(buf))
        text = buf.value
        if text == title_prefix or text.startswith(title_prefix + ' '):
            pid = D()
            get_tid_pid(hwnd, C.byref(pid))
            if pid.value and pid.value != exclude_pid:
                info = owner(pid.value)
                if info.name.casefold() in ('python.exe', 'pythonw.exe'):
                    results.append((hwnd, info))
        return True

    enum_win(visit, 0)
    return results
