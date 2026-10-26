"""常量、按键映射、黑名单、工具函数

本模块只描述"有哪些键、哪些键不允许用"，不含任何状态。
"""

APP_NAME = "PressTrigger"
VERSION = "2.2"
CONFIG_FILE = "config.json"
SOUND_FILE = "switch.mp3"
ICON_FILE = "icon.ico"

# ---- 时间常量（单位：秒）----
MIN_INTERVAL = 0.001        # 连点周期下限 1ms
MAX_INTERVAL = 60.0         # 连点周期上限 60s
DEFAULT_INTERVAL = 0.050    # 新按键默认周期 50ms
MIN_PRESS = 0.001           # 按下保持时间下限 1ms
MAX_PRESS = 1.0             # 按下保持时间上限 1s
DEFAULT_PRESS = 0.020       # 新按键默认按下保持 20ms

# 黑名单：修饰键 + 功能键(F1-F16) + 系统功能键
BLACKLIST = frozenset({
    # 修饰键
    "CTRL", "SHIFT", "ALT",
    "LEFT CTRL", "RIGHT CTRL", "LEFT SHIFT", "RIGHT SHIFT",
    "LEFT ALT", "RIGHT ALT", "LEFT WINDOWS", "RIGHT WINDOWS", "WIN",
    # 功能键 F1-F16
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
    "F9", "F10", "F11", "F12", "F13", "F14", "F15", "F16",
    # 系统功能键
    "TAB", "ESC", "CAPS LOCK", "CAPSLOCK", "CAPSLK",
    "PAUSE", "BREAK", "NUM LOCK", "NUMLOCK", "NUMLK",
    "SCROLL LOCK", "SCRLK", "PRINT SCREEN", "PRTSC", "PRINTSCREEN", "SNAPSHOT",
})

# 按键名 -> Windows virtual-key code。
# 注意：使用主键盘区的 VK，而不是小键盘区的 VK（OEM_PLUS/OEM_2/OEM_MINUS），
# 否则主键盘的 `+` `/` `-` 无法录制。
PRESS_MAP = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45,
    "F": 0x46, "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A,
    "K": 0x4B, "L": 0x4C, "M": 0x4D, "N": 0x4E, "O": 0x4F,
    "P": 0x50, "Q": 0x51, "R": 0x52, "S": 0x53, "T": 0x54,
    "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58, "Y": 0x59, "Z": 0x5A,

    "BACKSPACE": 0x08, "ENTER": 0x0D, "SPACE": 0x20,

    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGE UP": 0x21, "PAGE DOWN": 0x22, "PGUP": 0x21, "PGDN": 0x22,

    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}

# 鼠标按键名 -> virtual-key code（低层钩子的鼠标消息按此映射成统一 VK）
MOUSE_VK = {"LCLICK": 0x01, "RCLICK": 0x02, "MCLICK": 0x04}

# 需要 KEYEVENTF_EXTENDEDKEY 的键（方向键区、小键盘回车、右 Ctrl 等）。
# 缺了这个标志，physical 模式发送的扫描码在部分游戏里会被识别成别的键。
EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PGUP/PGDN/END/HOME/方向键
    0x2D, 0x2E,                                       # INSERT / DELETE
    0x5B, 0x5C, 0x5D,                                 # 左右 WIN / MENU
    0xA3, 0xA5,                                       # 右 Ctrl / 右 Alt
    0x6F,                                             # 小键盘 /
    0x0D,                                             # 小键盘回车（宽松处理，主回车加此标志亦兼容）
})

# 按键录入下拉框的顺序：字母数字 -> 编辑键 -> 导航键 -> 符号 -> 鼠标
_KEY_ORDER = (
    [chr(c) for c in range(0x30, 0x3A)]
    + [chr(c) for c in range(0x41, 0x5B)]
    + ["SPACE", "ENTER", "BACKSPACE"]
    + ["LEFT", "UP", "RIGHT", "DOWN", "HOME", "END", "INSERT", "DELETE",
       "PAGE UP", "PAGE DOWN"]
    + ["-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "`"]
    + list(MOUSE_VK)
)
AVAILABLE_KEYS = tuple(k for k in _KEY_ORDER if k not in BLACKLIST)

_VK_TO_NAME = {}
for _name in _KEY_ORDER:
    _vk = PRESS_MAP.get(_name)
    if _vk is None:
        _vk = MOUSE_VK.get(_name)
    if _vk is not None:
        _VK_TO_NAME[_vk] = _name
del _name, _vk


def get_vk_code(key):
    """按键名 -> VK；未知返回 None"""
    if not key:
        return None
    key = key.upper()
    if key in PRESS_MAP:
        return PRESS_MAP[key]
    return MOUSE_VK.get(key)


def get_key_name(vk):
    """VK -> 首选按键名；未知返回 None"""
    return _VK_TO_NAME.get(vk)


def get_key_type(key):
    """'mouse' 或 'keyboard'"""
    return "mouse" if key and key.upper() in MOUSE_VK else "keyboard"


def is_valid_key(key):
    """是否为可设置为连点键的键（非黑名单且受支持）"""
    return normalize_key(key) is not None


def normalize_key(key):
    """规范化按键名并校验；非法返回 None

    优先匹配带空格的规范名（"PAGE UP"），
    再退回去掉空格的形式（"PAGEUP"），最后尝试大写原样。
    """
    if not key:
        return None
    upper = str(key).strip().upper()
    if upper in AVAILABLE_KEYS:
        return upper
    compact = upper.replace(" ", "")
    for name in AVAILABLE_KEYS:
        if name.replace(" ", "") == compact:
            return name
    return None
