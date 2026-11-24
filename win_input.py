"""Windows bridge for a PID-owned SDL capture window."""
import ctypes as C
from ctypes import wintypes as W
import time
from dataclasses import dataclass

u = C.WinDLL('user32', use_last_error=True)
CALLBACK = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


def api(name, result, *args):
    fn = getattr(u, name)
    fn.restype, fn.argtypes = result, args
    return fn


foreground = api('GetForegroundWindow', W.HWND)
focus = api('SetForegroundWindow', W.BOOL, W.HWND)
show = api('ShowWindowAsync', W.BOOL, W.HWND, C.c_int)
visible = api('IsWindowVisible', W.BOOL, W.HWND)
post = api('PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
enum = api('EnumWindows', W.BOOL, CALLBACK, W.LPARAM)
window_pid = api('GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD))
title = api('GetWindowTextW', C.c_int, W.HWND, W.LPWSTR, C.c_int)
rect = api('GetWindowRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
clip = api('GetClipCursor', W.BOOL, C.POINTER(W.RECT))
cursor = api('GetCursorPos', W.BOOL, C.POINTER(W.POINT))
move = api('SetCursorPos', W.BOOL, C.c_int, C.c_int)
key_down = api('GetAsyncKeyState', C.c_short, C.c_int)
register = api('RegisterHotKey', W.BOOL, W.HWND, C.c_int, W.UINT, W.UINT)
unregister = api('UnregisterHotKey', W.BOOL, W.HWND, C.c_int)
z_order = api('SetWindowPos', W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
is_window = api('IsWindow', W.BOOL, W.HWND)
root_window = api('GetAncestor', W.HWND, W.HWND, W.UINT)
monitor_at = api('MonitorFromPoint', W.HANDLE, W.POINT, W.DWORD)
monitor_from_window = api('MonitorFromWindow', W.HANDLE, W.HWND, W.DWORD)
system_metric = api('GetSystemMetrics', C.c_int, C.c_int)
TITLE = 'TabletUHIDCapture'
MOD_KEYS = (0x10, 0x11, 0x12, 0x5b, 0x5c)
BUTTON_KEYS = (1, 2, 4, 5, 6)


def activate(hwnd, expected):
    current = foreground()
    return bool(current == hwnd or (current and current == expected and focus(hwnd)))


class MonitorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('area', W.RECT), ('work', W.RECT),
                ('flags', W.DWORD)]


get_monitor_info = api('GetMonitorInfoW', W.BOOL, W.HANDLE, C.POINTER(MonitorInfo))


def monitor_info(handle):
    info = MonitorInfo(size=C.sizeof(MonitorInfo))
    if handle and get_monitor_info(handle, C.byref(info)):
        return info


def monitor_for_qt_rect(rect, scale):
    # Qt/Windows preserves the native origin of each "island", scaling its size only.
    # QScreen.name() may be a friendly name, not MONITORINFOEX.szDevice.
    x, y, width, height = rect
    handle = monitor((x, y))
    info = monitor_info(handle)
    if info:
        r = info.area
        if ((r.left, r.top) == (x, y) and abs(r.right - x - width * scale) <= scale
                and abs(r.bottom - y - height * scale) <= scale):
            return handle


def adjacent_monitor(handle, side, fraction):
    info = monitor_info(handle)
    if info is None:
        return None
    r = info.area
    x = r.left - 1 if side == 'left' else r.right
    y = r.top + min(r.bottom - r.top - 1, int(fraction * (r.bottom - r.top)))
    return bool(monitor((x, y)))


def capture_position(handle, side, position, width=120, height=36, scale=1):
    info = monitor_info(handle)
    if info is None:
        return None
    a, r = info.area, info.work
    gap = round(22 * scale)  # 14 DIP hit band + 8 DIP clearance
    left, right = max(r.left, a.left + gap), min(r.right, a.right - gap)
    if right - left < width or r.bottom - r.top < height:
        return None
    x = left if side == 'left' else right - width
    bar_y = a.top + (a.bottom - a.top) * position // 100
    y = min(r.bottom - height, max(r.top, bar_y - height // 2))
    return x, y


def place_window(hwnd, handle, side='right', position=50, scale=1):
    box = bounds(hwnd)
    if not box:
        return False
    w, h = box[2] - box[0], box[3] - box[1]
    point = capture_position(handle or monitor_from_window(hwnd, 2), side, position, w, h, scale)
    return bool(point and z_order(hwnd, -1, *point, 0, 0, 0x0011))  # NOACTIVATE | NOSIZE


def owns_window(hwnd, pid):
    if not hwnd or not is_window(hwnd):
        return False
    actual = W.DWORD()
    return bool(window_pid(hwnd, C.byref(actual)) and actual.value == pid)


def find_window(pid, expected=TITLE):
    found = None

    @CALLBACK
    def visit(hwnd, _):
        nonlocal found
        actual = W.DWORD()
        window_pid(hwnd, C.byref(actual))
        if actual.value == pid:
            text = C.create_unicode_buffer(256)
            title(hwnd, text, len(text))
            if text.value == expected:
                found = hwnd
                return False
        return True

    enum(visit, 0)
    return found


def bounds(hwnd):
    r = W.RECT()
    if rect(hwnd, C.byref(r)):
        return r.left, r.top, r.right, r.bottom


def clip_bounds():
    r = W.RECT()
    if clip(C.byref(r)):
        return r.left, r.top, r.right, r.bottom


def clip_inside(r, c):
    return bool(r and c and r[0] <= c[0] < c[2] <= r[2] and r[1] <= c[1] < c[3] <= r[3])


@dataclass(frozen=True)
class CaptureSnapshot:
    monotonic: float
    pid_alive: bool
    hwnd_valid: bool
    fg_before: int | None
    bounds: tuple | None
    clip: tuple | None
    fg_after: int | None
    fg_kind: str

    @property
    def captured(self):
        return (self.pid_alive and self.hwnd_valid and self.fg_before == self.fg_after
                and self.fg_kind == 'self' and clip_inside(self.bounds, self.clip))


def capture_snapshot(hwnd, proc):
    now = time.monotonic()
    before = foreground()
    alive = proc is not None and proc.poll() is None
    box = bounds(hwnd) if alive and owns_window(hwnd, proc.pid) else None
    clipping = clip_bounds()
    after = foreground()
    valid = bool(box and owns_window(hwnd, proc.pid))
    kind = 'none' if not after else 'self' if valid and after == hwnd else 'other'
    return CaptureSnapshot(now, alive, valid, before, box, clipping, after, kind)


def position():
    p = W.POINT()
    if cursor(C.byref(p)):
        return p.x, p.y


def monitor(pos):
    return monitor_at(W.POINT(*pos), 0) if pos is not None else None


def desktop_bounds():
    left, top, width, height = (system_metric(k) for k in (76, 77, 78, 79))
    return left, top, left + width, top + height


def held(keys):
    return any(key_down(k) < 0 for k in keys)


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('data', W.DWORD),
                ('flags', W.DWORD), ('time', W.DWORD), ('extra', W.WPARAM)]


class KEYINPUT(C.Structure):
    _fields_ = [('vk', W.WORD), ('scan', W.WORD), ('flags', W.DWORD),
                ('time', W.DWORD), ('extra', W.WPARAM)]


class PAYLOAD(C.Union):
    _fields_ = [('mouse', MOUSEINPUT), ('key', KEYINPUT)]


class INPUT(C.Structure):
    _fields_ = [('kind', W.DWORD), ('value', PAYLOAD)]


send = api('SendInput', W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)


def tap(vk):
    events = (INPUT * 2)(*(INPUT(1, PAYLOAD(key=KEYINPUT(vk, 0, flags, 0, 0)))
                           for flags in (0, 2)))
    if send(1, C.byref(events[0]), C.sizeof(INPUT)) != 1:
        return False
    time.sleep(.025)
    return send(1, C.byref(events[1]), C.sizeof(INPUT)) == 1


def click(flags=(2, 4), data=0):
    events = (INPUT * len(flags))(*(INPUT(0, PAYLOAD(mouse=MOUSEINPUT(0, 0, data, f, 0, 0)))
                                    for f in flags))
    return send(len(events), events, C.sizeof(INPUT)) == len(events)


def window_at(x, y):
    return api('WindowFromPoint', W.HWND, W.POINT)(W.POINT(x, y))


def click_window(hwnd, proc, keys=()):
    """Return (injected, moved_to); never retry or click through an obstruction."""
    def valid():
        return (proc.poll() is None and owns_window(hwnd, proc.pid)
                and foreground() == hwnd and not held(MOD_KEYS + BUTTON_KEYS + keys))

    box = bounds(hwnd) if valid() else None
    if not box:
        return False, None
    l, t, r, b = box
    x, y = (l + r) // 2, (t + b) // 2

    def hits_target():
        hit = window_at(x, y)
        return bool(hit and root_window(hit, 2) == hwnd and owns_window(hit, proc.pid))

    if not hits_target() or not valid():
        return False, None
    before = position()
    if before is None or not move(x, y):
        return False, None
    pos = (x, y)
    moved_to = pos if before != pos else None
    if not valid() or not hits_target() or position() != pos:
        return False, moved_to
    return click(), moved_to


def session_notifications(hwnd, enabled):
    wts = C.WinDLL('wtsapi32', use_last_error=True)
    fn = wts.WTSRegisterSessionNotification if enabled else wts.WTSUnRegisterSessionNotification
    fn.restype = W.BOOL
    fn.argtypes = (W.HWND, W.DWORD) if enabled else (W.HWND,)
    return fn(hwnd, 0) if enabled else fn(hwnd)  # NOTIFY_FOR_THIS_SESSION
