"""配置读写 + 单实例锁

配置结构（config.json，位于 exe 同目录）::

    {
      "active": true,
      "items": [
        {"key": "Q", "enabled": true, "interval": 0.1, "press": 0.02}
      ]
    }

* interval: 连点周期（按下->按下），秒
* press:    每次按下的保持时间，秒；必须小于 interval

旧的 "mode"/"type" 字段会被忽略（v2.2 统一使用 SendInput）。
"""

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field

from constants import (
    CONFIG_FILE, ICON_FILE, SOUND_FILE, APP_NAME,
    MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL,
    MIN_PRESS, MAX_PRESS, DEFAULT_PRESS,
    get_vk_code, normalize_key,
)

# ============ 路径 ============
def get_app_dir():
    """打包后取 exe 所在目录，开发时取脚本目录（config.json 写在这里）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_dir():
    """打包后资源文件目录（PyInstaller 的 sys._MEIPASS）"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", get_app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def get_config_path():
    return os.path.join(get_app_dir(), CONFIG_FILE)


def get_sound_path():
    return os.path.join(get_resource_dir(), SOUND_FILE)


def get_icon_path():
    return os.path.join(get_resource_dir(), ICON_FILE)


# ============ 数值解析 ============
def _fmt(value):
    """数值 -> 配置文件里的紧凑字符串（0.05 而不是 0.05000000000000001）"""
    return f"{round(float(value), 6):g}"


def _number(value, default, minimum, maximum):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num or num in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return min(max(num, minimum), maximum)


def parse_interval(value, default=DEFAULT_INTERVAL):
    return _number(value, default, MIN_INTERVAL, MAX_INTERVAL)


def parse_press(value, interval, default=DEFAULT_PRESS):
    """按下时间不能超过周期本身，否则连点会粘连"""
    press = _number(value, default, MIN_PRESS, MAX_PRESS)
    return min(press, interval)


# ============ 配置模型 ============
@dataclass
class ClickItem:
    key: str
    enabled: bool = True
    interval: float = DEFAULT_INTERVAL
    press: float = DEFAULT_PRESS

    @property
    def vk(self):
        return get_vk_code(self.key)

    def to_dict(self):
        return {
            "key": self.key,
            "enabled": bool(self.enabled),
            "interval": _fmt(self.interval),
            "press": _fmt(self.press),
        }

    @classmethod
    def from_dict(cls, raw):
        """从配置字典构造；键名非法返回 None（该条会被丢弃）"""
        if not isinstance(raw, dict):
            return None
        key = normalize_key(raw.get("key"))
        if key is None:
            return None
        interval = parse_interval(raw.get("interval"))
        press = parse_press(raw.get("press"), interval)
        return cls(key=key,
                   enabled=bool(raw.get("enabled", True)),
                   interval=interval,
                   press=press)


@dataclass
class AppConfig:
    items: list = field(default_factory=list)   # list[ClickItem]
    active: bool = True

    # ---- 查询 ----
    def find(self, key):
        key = normalize_key(key)
        if key is None:
            return None
        for item in self.items:
            if item.key == key:
                return item
        return None

    def add(self, item):
        """追加一条；按键重复则返回 False"""
        if self.find(item.key) is not None:
            return False
        self.items.append(item)
        return True

    def remove(self, key):
        item = self.find(key)
        if item is None:
            return False
        self.items.remove(item)
        return True

    # ---- 序列化 ----
    def to_dict(self):
        return {
            "version": 2,
            "active": bool(self.active),
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, raw):
        """容错解析：任何一条坏了都只丢那一条，不让整个配置失效"""
        cfg = cls()
        if not isinstance(raw, dict):
            return cfg
        cfg.active = bool(raw.get("active", True))
        items = raw.get("items")
        if isinstance(items, list):
            seen = set()
            for entry in items:
                item = ClickItem.from_dict(entry)
                if item is None or item.key in seen:
                    continue
                seen.add(item.key)
                cfg.items.append(item)
        return cfg


# ============ 读写 ============
_save_lock = threading.Lock()


def load_config():
    """读取配置；不存在或损坏时返回默认配置"""
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return AppConfig.from_dict(json.load(f))
    except FileNotFoundError:
        return AppConfig()
    except (OSError, ValueError):
        return AppConfig()


def save_config(config):
    """原子写回配置；成功返回 True

    先写临时文件再 os.replace，避免退出瞬间断电/被杀进程留下半个 JSON。
    """
    data = config.to_dict() if isinstance(config, AppConfig) else config
    path = get_config_path()
    tmp = path + ".tmp"
    with _save_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False


# ============ 单实例锁 ============
_ERROR_ALREADY_EXISTS = 183
_lock_handle = None


def acquire_instance_lock():
    """命名互斥体单实例锁；成功返回句柄，已存在返回 None

    旧版绑定固定 TCP 端口 45821，任何占用该端口的程序都会让本程序
    误报"已在运行"。命名互斥体是内核对象，无端口冲突问题，
    且进程被杀时由系统自动释放，不会留下僵尸锁。
    """
    global _lock_handle
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_single_instance")
    if not handle:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    _lock_handle = handle
    return handle
