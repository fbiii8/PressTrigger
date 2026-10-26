"""Windows 输入底层：合成输入（SendInput）+ 物理输入状态跟踪

设计要点
--------
1. 所有由本程序发送的按键都带一个 `dwExtraInfo` 标签（_TAG）。
   低层钩子回调可以据此（以及 LLKHF_INJECTED 标志）丢弃"自己发的"按键。
   这样引擎看到的状态**永远只反映真实物理输入**，不再需要以前那种
   "模拟窗口时间猜测"来防回流 —— 那正是"第一轮能点、后续失灵"的根因。

2. 鼠标左右中键的 VK 与键盘共用同一套编号（0x01/0x02/0x04），
   钩子消息与 VK 之间通过 _MOUSE_MSG_VK 映射，状态集合统一用 VK 存放。

3. 热路径上的 Win32 函数在模块加载时一次性解析并声明 argtypes，
   避免每次调用都走 ctypes 的动态查找与类型推断。
"""

import ctypes
import sys
import threading
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import WinDLL

    user32 = WinDLL("user32", use_last_error=True)
    kernel32 = WinDLL("kernel32", use_last_error=True)

    # 热路径函数句柄（模块级缓存，每次点击都用到）
    _SendInput = user32.SendInput
    _MapVirtualKeyW = user32.MapVirtualKeyW
    _GetModuleHandleW = kernel32.GetModuleHandleW

    _SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
    _SendInput.restype = wintypes.UINT
    _MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _MapVirtualKeyW.restype = wintypes.UINT
    _GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _GetModuleHandleW.restype = wintypes.HMODULE

    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, ctypes.c_void_p,
                                         wintypes.HINSTANCE, wintypes.DWORD)
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)
    user32.CallNextHookEx.restype = ctypes.c_void_p
    user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT)
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM)
    user32.PostThreadMessageW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    from constants import EXTENDED_VKS

    # ---- 高精度等待（Windows 默认时钟粒度 15.6ms，会把 10ms 间隔拉成 15.6ms）----
    TIMER_ALL_ACCESS = 0x1F0003
    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
    #: 内核定时器接管的等待时长；剩下的用紧凑循环收敛到微秒级
    _SPIN_THRESHOLD = 0.0015
    #: SetWaitableTimer + WaitForSingleObject 的固定开销补偿（实测约 0.5ms）
    _TIMER_OVERHEAD = 0.0004

    kernel32.CreateWaitableTimerExW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR,
                                                wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
    kernel32.SetWaitableTimer.argtypes = (ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_longlong),
                                          wintypes.LONG, ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.BOOL)
    kernel32.SetWaitableTimer.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    try:                                    # 提升系统时钟粒度，也让 time.sleep 变准
        from ctypes import WinDLL as _WinDLL
        _winmm = _WinDLL("winmm")
        _winmm.timeBeginPeriod(1)
        _TIMER_PERIOD_RAISED = True
    except Exception:                       # pragma: no cover
        _TIMER_PERIOD_RAISED = False

    # 可等待定时器**池**。
    #
    # 这里绝不能全进程共用一个定时器句柄：等待定时器是"一个"内核对象，
    # 每次 SetWaitableTimer 都会**重置/取消**上一次设定的到期时间。
    # 一旦两个连点线程（例如同时按住 Q 和 E）并发调用：
    #     A: SetWaitableTimer(t) -> WaitForSingleObject(t)
    #     B: SetWaitableTimer(t)      <- 把 A 的定时取消掉了
    # A 就再也等不到自己的信号，`WaitForSingleObject(t, INFINITE)` 永久挂起，
    # 该按键的连点线程卡在 input_win.click() 里再也不返回 —— 表现为
    # "Q 连点途中按了 E，之后 Q 就再也不连点了（同时也收不到松手）"。
    # 所以：每个并发等待者必须拿到**独占**的定时器句柄，用完归还池子。
    _TIMER_POOL_SIZE = 8
    _TIMER_POOL = []
    _timer_lock = threading.Lock()

    def _new_timer():                       # pragma: no cover - 少数调用
        handle = kernel32.CreateWaitableTimerExW(
            None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS)
        if not handle:                      # 旧系统（< Win10 1803）退回普通定时器
            handle = kernel32.CreateWaitableTimerExW(None, None, 0, TIMER_ALL_ACCESS)
        return handle

    for _ in range(_TIMER_POOL_SIZE):
        _h = _new_timer()
        if not _h:
            break
        _TIMER_POOL.append(_h)
    del _h

    def _acquire_timer():
        with _timer_lock:
            if _TIMER_POOL:
                return _TIMER_POOL.pop()
        return None                         # 池空：退化用 time.sleep，绝不死等

    def _release_timer(handle):
        with _timer_lock:
            _TIMER_POOL.append(handle)

    def _precise_sleep(seconds):
        """高精度等待：独占可等待定时器 + 最后的紧凑循环"""
        deadline = time.perf_counter() + seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= _SPIN_THRESHOLD:
                break
            timer = _acquire_timer()
            if timer is None:                                       # pragma: no cover
                _sleep(remaining)
                continue
            try:
                wait = remaining - _TIMER_OVERHEAD
                due = ctypes.c_longlong(-int(wait * 10_000_000))     # 负数=相对时间
                if kernel32.SetWaitableTimer(timer, ctypes.byref(due), 0,
                                             None, None, False):
                    kernel32.WaitForSingleObject(timer, 0xFFFFFFFF)
                else:                                               # pragma: no cover
                    _sleep(remaining)
            finally:
                _release_timer(timer)
        while time.perf_counter() < deadline:   # 亚毫秒收敛
            pass

else:                                       # pragma: no cover - 非 Windows
    def _precise_sleep(seconds):
        _sleep(seconds)

# ============ 常量 ============

#: 合成输入的签名，出现在 KBDLLHOOKSTRUCT/MOUSEINPUT 的 dwExtraInfo 中
INPUT_TAG = 0x50545247  # 'PTRG'

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

MAPVK_VK_TO_VSC = 0

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

LLKHF_INJECTED = 0x00000010
LLMHF_INJECTED = 0x00000001

_MOUSE_MSG_VK = {
    WM_LBUTTONDOWN: 0x01, WM_LBUTTONUP: 0x01,
    WM_RBUTTONDOWN: 0x02, WM_RBUTTONUP: 0x02,
    WM_MBUTTONDOWN: 0x04, WM_MBUTTONUP: 0x04,
}

_MOUSE_BUTTON_FLAGS = {
    0x01: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    0x02: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    0x04: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_clock = time.perf_counter
_sleep = time.sleep


# ============ 结构体 ============
if IS_WINDOWS:

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p))

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p))

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (("uMsg", wintypes.DWORD),
                    ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD))

    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT),
                    ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT))

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD),
                    ("u", _INPUTUNION))

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p))

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p))

    class InputError(OSError):
        """SendInput 未能投递全部事件"""

    _HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)

else:  # 非 Windows：仅用于语法检查/文档生成，运行会直接报错
    INPUT = KBDLLHOOKSTRUCT = MSLLHOOKSTRUCT = object
    _HOOKPROC = None

    class InputError(OSError):
        pass


def _require_windows():
    if not IS_WINDOWS:
        raise RuntimeError("PressTrigger 仅支持 Windows")


# ============ 合成输入 ============
def _send(inputs):
    """投递一组 INPUT，全部成功才返回"""
    _require_windows()
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = _SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
    if sent != n:
        raise InputError(f"SendInput 投递失败：{sent}/{n}")


def build_click_inputs(vk):
    """构造一次"按下-松开"的 INPUT 对

    Args:
        vk: virtual-key code，鼠标键使用 0x01/0x02/0x04
    """
    _require_windows()
    tag = ctypes.c_void_p(INPUT_TAG)

    if vk in (0x01, 0x02, 0x04):
        down, up = _MOUSE_BUTTON_FLAGS[vk]
        i_down = INPUT(type=INPUT_MOUSE)
        i_down.mi = MOUSEINPUT(0, 0, 0, down, 0, tag)
        i_up = INPUT(type=INPUT_MOUSE)
        i_up.mi = MOUSEINPUT(0, 0, 0, up, 0, tag)
        return i_down, i_up

    scan = _MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFF
    flags = KEYEVENTF_SCANCODE
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY

    i_down = INPUT(type=INPUT_KEYBOARD)
    i_down.ki = KEYBDINPUT(0, scan, flags, 0, tag)
    i_up = INPUT(type=INPUT_KEYBOARD)
    i_up.ki = KEYBDINPUT(0, scan, flags | KEYEVENTF_KEYUP, 0, tag)
    return i_down, i_up


def click(vk, press):
    """执行一次连点：按下 -> 保持 press -> 松开

    返回本次点击实际占用的时间，调用方据此扣减剩余等待，
    使"按下->按下"的周期稳定等于设定值（旧版存在 interval + 5ms 的固定漂移）。
    """
    i_down, i_up = build_click_inputs(vk)

    t0 = _clock()
    _send((i_down,))
    _precise_sleep(press)
    _send((i_up,))
    return _clock() - t0


# ============ 物理输入跟踪 ============
class PhysicalInputTracker:
    """用低层钩子跟踪**真实物理**键鼠按下状态

    钩子在独立线程里安装（Windows 要求安装线程自身跑消息循环）。
    每次状态变化回调都在新的工作线程中执行，避免拖慢钩子回调
    —— 钩子回调超时会被系统摘除，那正是"游戏里第一次能用、之后就没了"的另一诱因。

    回调签名: on_press(vk) / on_release(vk)
    """

    def __init__(self, on_press=None, on_release=None, name="input-tracker"):
        self._on_press = on_press
        self._on_release = on_release
        self._name = name

        self._lock = threading.RLock()
        self._pressed = set()

        self._thread = None
        self._thread_id = 0
        self._h_kb = None
        self._h_ms = None
        self._running = False
        self._ready = threading.Event()
        self._error = None

        # 必须保住 ctypes 回调对象的引用，否则会被 GC 掉导致进程崩溃
        self._kb_proc = None
        self._ms_proc = None

    # ---- 生命周期 ----
    def set_callbacks(self, on_press=None, on_release=None):
        """设置（或替换）按下/抬起回调；线程安全"""
        with self._lock:
            if on_press is not None:
                self._on_press = on_press
            if on_release is not None:
                self._on_release = on_release

    def start(self, timeout=3.0):
        """启动并在钩子就绪后返回；失败抛 RuntimeError"""
        _require_windows()
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("输入钩子启动超时")
        if self._error is not None:
            raise RuntimeError(f"输入钩子安装失败：{self._error}")

    def stop(self, timeout=2.0):
        """卸载钩子并等待线程退出（必须在钩子线程之外调用）"""
        if not IS_WINDOWS:
            return
        thread = self._thread
        if thread is None:
            return
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout)
        self._thread = None
        self._thread_id = 0

    @property
    def pressed(self):
        """当前按下的物理键 VK 集合（快照）"""
        with self._lock:
            return set(self._pressed)

    def is_pressed(self, vk):
        with self._lock:
            return vk in self._pressed

    # ---- 钩子线程 ----
    def _run(self):
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            hmod = _GetModuleHandleW(None)
            self._kb_proc = _HOOKPROC(self._kb_callback)
            self._ms_proc = _HOOKPROC(self._ms_callback)

            self._h_kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc,
                                                  hmod, 0)
            if not self._h_kb:
                self._error = f"WH_KEYBOARD_LL err={ctypes.get_last_error()}"
                return
            self._h_ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc,
                                                 hmod, 0)
            if not self._h_ms:
                self._error = f"WH_MOUSE_LL err={ctypes.get_last_error()}"
                user32.UnhookWindowsHookEx(self._h_kb)
                self._h_kb = None
                return

            self._running = True
            self._ready.set()

            msg = wintypes.MSG()
            while self._running:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:            # pragma: no cover - 防御性
            self._error = repr(exc)
        finally:
            self._running = False
            if self._h_kb:
                user32.UnhookWindowsHookEx(self._h_kb)
                self._h_kb = None
            if self._h_ms:
                user32.UnhookWindowsHookEx(self._h_ms)
                self._h_ms = None
            self._ready.set()

    # ---- 回调 ----
    def _kb_callback(self, n_code, w_param, l_param):
        if n_code == 0:
            try:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (kb.flags & LLKHF_INJECTED) and kb.dwExtraInfo != INPUT_TAG:
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self._apply(kb.vkCode, True)
                    elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                        self._apply(kb.vkCode, False)
            except Exception:
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _ms_callback(self, n_code, w_param, l_param):
        if n_code == 0:
            try:
                vk = _MOUSE_MSG_VK.get(w_param)
                if vk is not None:
                    ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if not (ms.flags & LLMHF_INJECTED) and ms.dwExtraInfo != INPUT_TAG:
                        self._apply(vk, w_param in (WM_LBUTTONDOWN, WM_RBUTTONDOWN,
                                                    WM_MBUTTONDOWN))
            except Exception:
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _apply(self, vk, down):
        """处理一次状态变化；只在真正发生变化时才派发回调"""
        with self._lock:
            if down:
                if vk in self._pressed:
                    return                      # 键盘自动重复，忽略
                self._pressed.add(vk)
            else:
                if vk not in self._pressed:
                    return                      # 我们没有看到按下过，忽略
                self._pressed.discard(vk)
            callback = self._on_press if down else self._on_release

        if callback is None:
            return
        # 钩子回调必须尽快返回，交给工作线程执行真正的逻辑
        threading.Thread(target=callback, args=(vk,), daemon=True).start()
