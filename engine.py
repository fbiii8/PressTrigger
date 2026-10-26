"""连点引擎

核心设计（v2.2 重写）
--------------------
旧版用 5ms 轮询 `GetAsyncKeyState` + "模拟窗口"时间猜测来防止自己发的按键
被误判成物理按键。问题是它在模拟期间**整键跳过检测**，包括松手检测：
只要松手那一刻落在窗口内，`_clicking` 就永久卡在 True，之后再按也不会响应
—— 这就是"游戏里第一轮能点、之后失灵"。

新版改为事件驱动，三条硬保证：

1. 不会看错：`PhysicalInputTracker` 用低层钩子监听输入，并按注入标志丢弃
   本程序自己发的按键。引擎看到的状态只可能是真实物理输入，无需任何时间猜测。

2. 不会丢事件：工作线程用一个 `Condition` + 明确的 `held` 状态同步，
   "按下"是状态而不是一次性信号，因此不存在信号被吞掉导致的失灵。
   （先前用两个 Event 的实现就有这个竞态：唤醒信号会在一次状态检查中被吃掉。）

3. 不会卡死：线程空闲时零 CPU 阻塞等待，松手/禁用/删除按键都能立刻中断连点；
   每轮结束还用真实状态自纠一次，任何错位都会被下一轮纠正。
"""

import threading
import time

from constants import DEFAULT_INTERVAL, DEFAULT_PRESS
from config import parse_interval, parse_press, AppConfig
from input_win import PhysicalInputTracker, click

#: SendInput 失败后的退避时间，避免异常情况下空转刷 CPU
_ERROR_BACKOFF = 0.2


class _KeyWorker(threading.Thread):
    """单个按键的连点线程：空闲阻塞，按下即点，抬起即停"""

    def __init__(self, engine, vk):
        super().__init__(name=f"clicker-{vk:#04x}", daemon=True)
        self._engine = engine
        self._vk = vk

        self._cond = threading.Condition()
        self._held = False          # 物理键是否按着（状态，不是一次性事件）
        self._quit = False
        self._interval = DEFAULT_INTERVAL
        self._press = DEFAULT_PRESS

    # ---- 外部控制（引擎线程 / 钩子工作线程调用）----
    def configure(self, interval, press):
        with self._cond:
            self._interval = interval
            self._press = press
            self._cond.notify_all()     # 参数变更立即生效，不丢状态

    def begin(self):
        with self._cond:
            self._held = True
            self._cond.notify_all()

    def end(self):
        with self._cond:
            self._held = False
            self._cond.notify_all()

    def shutdown(self):
        with self._cond:
            self._quit = True
            self._held = False
            self._cond.notify_all()

    @property
    def held(self):
        with self._cond:
            return self._held

    # ---- 线程主体 ----
    def run(self):
        engine = self._engine
        vk = self._vk

        while True:
            with self._cond:
                # 空闲：阻塞等待"按下"，零 CPU 占用；1s 超时仅用于兜底退出
                while not self._quit and not (self._held and engine.active):
                    self._cond.wait(1.0)
                if self._quit:
                    return
                interval = self._interval
                press = min(self._press, interval)

            if self._click_while_held(interval, press):
                # 一轮连点自然结束，用真实状态自纠一次：
                # 只要"已停止"与"物理仍按着"出现偏差，这里立刻纠正
                engine._on_worker_idle(vk)

    def _click_while_held(self, interval, press):
        """连点直到松手/禁用/退出；返回是否应当退出线程"""
        engine = self._engine
        vk = self._vk

        while True:
            with self._cond:
                if self._quit:
                    return True
                if not self._held or not engine.active:
                    self._held = False
                    return False
                # 每次点击前重读参数，用户改设置即时生效
                interval = self._interval
                press = min(self._press, interval)

            if engine._should_abort(vk):
                self.end()
                return False

            started = time.perf_counter()
            try:
                click(vk, press)
            except OSError:
                # 投递失败（例如目标窗口以更高完整性级别运行）：
                # 退避后重试，用户松手即可停止，不会空转刷 CPU
                with self._cond:
                    self._cond.wait(_ERROR_BACKOFF)
                continue

            remaining = interval - (time.perf_counter() - started)
            if remaining > 0:
                # 用 Condition.wait 代替 sleep：
                # 松手、禁用、参数变更都能立刻中断剩余等待，
                # 而且醒来后回到循环顶部重新判断状态，不会再多点一次。
                with self._cond:
                    self._cond.wait(remaining)


class ClickerEngine:
    """连点引擎：维护按键工作线程，订阅物理输入事件"""

    def __init__(self, config=None, tracker=None):
        self.config = config or AppConfig()
        self.active = bool(self.config.active)
        self.tracker = tracker or PhysicalInputTracker()
        # 回调统一在这里接线：即使外部传入已构造好的 tracker，
        # 也不会出现"引擎收不到按下/抬起"的静默失效
        self.tracker.set_callbacks(on_press=self._on_press,
                                   on_release=self._on_release)
        self.on_state_change = None

        self._lock = threading.RLock()
        self._workers = {}          # vk -> _KeyWorker
        self._started = False

    # ---- 生命周期 ----
    def start(self):
        """安装输入钩子并同步工作线程；失败抛 RuntimeError"""
        if self._started:
            return
        self.tracker.start()
        self._started = True
        self.sync()

    def stop(self):
        """退出：停止钩子与全部连点线程"""
        if not self._started:
            return
        self._started = False
        self.tracker.stop()
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.shutdown()
        for worker in workers:
            worker.join(1.0)

    # ---- 配置同步 ----
    def sync(self):
        """把 config 里的按键集合同步到工作线程

        新增 -> 建线程；删除 -> 关线程；参数变化 -> 就地更新（不重建线程）。
        """
        wanted = {}
        for item in self.config.items:
            vk = item.vk
            if vk is None or not item.enabled:
                continue
            wanted[vk] = item

        with self._lock:
            for vk in list(self._workers):
                if vk not in wanted:
                    self._workers.pop(vk).shutdown()
            for vk, item in wanted.items():
                worker = self._workers.get(vk)
                if worker is None:
                    worker = _KeyWorker(self, vk)
                    self._workers[vk] = worker
                    worker.start()
                interval = parse_interval(item.interval)
                worker.configure(interval, parse_press(item.press, interval))

        # 物理上按着、但刚被删掉又加回来的键：按当前真实状态重新对齐
        self._resync_pressed(wanted)

    def _resync_pressed(self, wanted):
        if not self._started:
            return
        pressed = self.tracker.pressed
        for vk in wanted:
            worker = self._workers.get(vk)
            if worker is None:
                continue
            if vk in pressed:
                worker.begin()
            else:
                worker.end()

    # ---- 状态开关 ----
    def toggle_active(self):
        """切换启用/禁用；返回切换后的状态"""
        self.active = not self.active
        self.config.active = self.active

        with self._lock:
            workers = list(self._workers.values())
        # 无论开还是关都先停止连点：恢复启用后等用户重新按下再开始，
        # 避免"人已松手却还在点"
        for worker in workers:
            worker.end()

        self._play_switch_sound()
        if self.on_state_change is not None:
            try:
                self.on_state_change(self.active)
            except Exception:
                pass
        return self.active

    def set_active(self, active):
        if bool(active) != self.active:
            return self.toggle_active()
        return self.active

    # ---- 输入事件 ----
    def _on_press(self, vk):
        if not self.active:
            return
        worker = self._workers.get(vk)
        # 二次确认物理状态：回调是异步派发的，可能已经松手了
        if worker is not None and self.tracker.is_pressed(vk):
            worker.begin()

    def _on_release(self, vk):
        worker = self._workers.get(vk)
        if worker is not None:
            worker.end()

    # ---- 内部 ----
    def _should_abort(self, vk):
        """该按键是否已不该继续连点（被删掉或禁用）"""
        if self._workers.get(vk) is None:
            return True
        for item in self.config.items:
            if item.vk == vk:
                return not item.enabled
        return True

    def _on_worker_idle(self, vk):
        """工作线程结束一轮连点后，用真实状态自纠一次

        这是旧版缺失的关键一步：只要"连点已停"和"物理键还按着"出现偏差，
        就在这里立刻纠正，而不是像旧版那样永久卡死。
        """
        worker = self._workers.get(vk)
        if worker is None:
            return
        if self.active and not self._should_abort(vk) \
                and self._started and self.tracker.is_pressed(vk):
            worker.begin()
        else:
            worker.end()

    def _play_switch_sound(self):
        """异步播放提示音，绝不阻塞 UI 线程"""
        threading.Thread(target=_play_sound, daemon=True).start()


def _play_sound():
    """播放启停提示音；优先 playsound3（支持 mp3），失败退回 winsound"""
    from config import get_sound_path
    path = get_sound_path()
    try:
        import playsound3
        playsound3.playsound(path)
        return
    except Exception:
        pass
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC |
                           winsound.SND_NODEFAULT)
    except Exception:
        pass
