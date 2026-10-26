"""GUI 窗口 + 系统托盘

v2.2 变化
---------
* **删除按键录制功能**：不再用全局键盘钩子捕捉按键录入。
  旧实现用 `keyboard.hook()` 无差别监听，录入期间敲的所有按键
  （包括密码、聊天内容）都会流经本程序，且不拦截、不影响前台窗口，
  既慢又有隐私风险。现在改成下拉框直接选择按键。
* 列表改用 ttk.Treeview：原生外观、支持滚轮滚动、点击单元格即可开关/删除。
* 所有配置改动都即时保存，并调用 engine.sync() 让引擎立即生效
  （旧版删除按键后要等松手才生效）。
* 提示音改为后台播放，切换启用/禁用不再卡住界面。
"""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image
import pystray

from constants import (
    APP_NAME, VERSION, AVAILABLE_KEYS,
    MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL,
    MIN_PRESS, MAX_PRESS, DEFAULT_PRESS,
)
from config import (
    ClickItem, get_icon_path, load_config, save_config,
    parse_interval, parse_press, _fmt,
)
from engine import ClickerEngine

# 短名 -> 完整名（i18n 无关，纯展示用）
_KEY_LABEL = {
    "LCLICK": "鼠标左键", "RCLICK": "鼠标右键", "MCLICK": "鼠标中键",
    "SPACE": "空格", "ENTER": "回车", "BACKSPACE": "退格",
    "PAGE UP": "Page Up", "PAGE DOWN": "Page Down",
    "INSERT": "Insert", "DELETE": "Delete", "HOME": "Home", "END": "End",
    "LEFT": "←", "UP": "↑", "RIGHT": "→", "DOWN": "↓",
}
_KEY_CHOICES = [(k, f"{k}    （{_KEY_LABEL[k]}）" if k in _KEY_LABEL else k)
                for k in AVAILABLE_KEYS]
_LABEL_TO_KEY = {label: key for key, label in _KEY_CHOICES}


class App:
    def __init__(self):
        self.config = load_config()
        self.engine = ClickerEngine(self.config)
        self.engine.on_state_change = self._on_state_change

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("620x460")
        self.root.minsize(560, 360)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.protocol("WM_ICONIFY", self._on_minimize)

        self._tray_icon = None
        self._closing = False
        self._save_warned = False

        self._init_style()
        self._build_ui()
        self._refresh_list()
        self._apply_active_ui(self.engine.active)

        self._start_engine()
        self._setup_tray()

    # ============ 初始化 ============
    def _init_style(self):
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=28)
        style.configure("State.TLabel", font=("", 11, "bold"))

    def _start_engine(self):
        """引擎/钩子启动失败时给出明确提示，而不是静默失效"""
        try:
            self.engine.start()
        except Exception as exc:
            messagebox.showerror(
                "启动失败",
                f"无法安装输入监听：{exc}\n\n"
                "请以管理员身份运行，或检查安全软件是否拦截了键盘钩子。")
            self.root.after(200, self._quit)

    def _build_ui(self):
        # ---- 顶部状态栏 ----
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill=tk.X)

        self.state_label = ttk.Label(top, text="● 已启用", style="State.TLabel",
                                     foreground="#159c2f")
        self.state_label.pack(side=tk.LEFT)

        self.toggle_btn = ttk.Button(top, text="禁用", width=8,
                                     command=self._toggle_active)
        self.toggle_btn.pack(side=tk.RIGHT)

        tk.Label(top, text="长按下方按键即自动连点，松开即停",
                 fg="#6b6b6b").pack(side=tk.LEFT, padx=(12, 0))

        ttk.Separator(self.root).pack(fill=tk.X)

        # ---- 列表 ----
        mid = ttk.Frame(self.root, padding=(12, 8, 12, 0))
        mid.pack(fill=tk.BOTH, expand=True)

        columns = ("enabled", "key", "interval", "press", "action")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings",
                                 selectmode="browse")
        headings = {
            "enabled": ("启用", 60, "center"),
            "key": ("按键", 130, "center"),
            "interval": ("连点间隔(ms)", 110, "center"),
            "press": ("按下时长(ms)", 110, "center"),
            "action": ("操作", 80, "center"),
        }
        for name, (text, width, anchor) in headings.items():
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, anchor=anchor,
                             stretch=(name in ("key", "interval")))

        scrollbar = ttk.Scrollbar(mid, orient=tk.VERTICAL,
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("odd", background="#f6f7f9")
        self.tree.tag_configure("off", foreground="#9a9a9a")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<Delete>", lambda e: self._delete_selected())

        # ---- 空列表提示 ----
        self.empty_hint = ttk.Label(
            mid, text="暂无连点按键，点击下方「添加按键」开始",
            foreground="#8a8a8a", anchor="center")

        # ---- 底部按钮 ----
        bottom = ttk.Frame(self.root, padding=12)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(bottom, text="+ 添加按键", command=self._add_item
                   ).pack(side=tk.LEFT)
        ttk.Button(bottom, text="删除选中", command=self._delete_selected
                   ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text="恢复默认", command=self._reset_default
                   ).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="打开配置目录", command=self._open_config_dir
                   ).pack(side=tk.RIGHT, padx=(0, 8))

    # ============ 列表渲染 ============
    def _refresh_list(self):
        selected = self.tree.selection()
        keep = selected[0] if selected else None

        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.config.items):
            tags = ["odd"] if index % 2 else []
            if not item.enabled:
                tags.append("off")
            self.tree.insert(
                "", tk.END, iid=item.key,
                values=("●" if item.enabled else "○",
                        item.key,
                        _fmt(item.interval * 1000),
                        _fmt(item.press * 1000),
                        "删除"),
                tags=tuple(tags))

        if self.config.items:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    def _current_key(self):
        """当前选中行对应的按键名"""
        selection = self.tree.selection()
        if not selection:
            return None
        key = selection[0]
        return key if self.config.find(key) else None

    def _sync(self):
        """保存并让引擎立即生效"""
        if not save_config(self.config) and not self._save_warned:
            self._save_warned = True
            from config import get_config_path
            messagebox.showwarning(
                "配置未能保存",
                f"无法写入配置文件：\n{get_config_path()}\n\n"
                "程序所在目录可能没有写权限（例如 Program Files）。\n"
                "请把程序移动到桌面等可写目录后再试。")
        self.engine.sync()

    # ============ 列表交互 ============
    def _on_tree_click(self, event):
        """单击：启用列 -> 切换开关；操作列 -> 删除"""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row:
            return
        self.tree.selection_set(row)

        if column == "#1":
            self._toggle_item(row)
        elif column == "#5":
            self._delete_item(row)

    def _on_tree_double_click(self, event):
        """双击间隔/按下时长列 -> 就地编辑"""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row or column not in ("#3", "#4"):
            return
        self._edit_number(row, column)

    def _on_wheel(self, event):
        self.tree.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _edit_number(self, key, column):
        """在单元格位置弹出输入框修改数值（毫秒）"""
        item = self.config.find(key)
        if item is None:
            return
        bbox = self.tree.bbox(key, column)
        if not bbox:
            return
        x, y, width, height = bbox

        if column == "#3":
            current = item.interval * 1000
            low, high = MIN_INTERVAL * 1000, MAX_INTERVAL * 1000
            title = "连点间隔"
        else:
            current = item.press * 1000
            low, high = MIN_PRESS * 1000, MAX_PRESS * 1000
            title = "按下时长"

        editor = tk.Entry(self.tree, justify="center")
        editor.insert(0, _fmt(current))
        editor.select_range(0, tk.END)
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()

        state = {"done": False}

        def commit(_event=None):
            if state["done"]:
                return
            state["done"] = True
            raw = editor.get()
            editor.destroy()
            try:
                value = float(raw) / 1000.0
            except ValueError:
                return                      # 输入非法：保持原值，静默放弃
            if column == "#3":
                if value < MIN_INTERVAL or value > MAX_INTERVAL:
                    messagebox.showinfo(
                        title, f"{title} 必须在 {low:g}–{high:g} ms 之间，已按边界取值。")
                item.interval = parse_interval(value)
                item.press = min(item.press, item.interval)
            else:
                if value < MIN_PRESS or value > MAX_PRESS:
                    messagebox.showinfo(
                        title, f"{title} 必须在 {low:g}–{high:g} ms 之间，已按边界取值。")
                item.press = parse_press(value, item.interval)
            self._sync()
            self._refresh_list()
            self.tree.selection_set(key)

        def cancel(_event=None):
            if state["done"]:
                return
            state["done"] = True
            editor.destroy()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", cancel)

    # ============ 增删改 ============
    def _toggle_item(self, key):
        item = self.config.find(key)
        if item is None:
            return
        item.enabled = not item.enabled
        self._sync()
        self._refresh_list()
        self.tree.selection_set(key)

    def _delete_item(self, key):
        if self.config.remove(key):
            self._sync()
            self._refresh_list()

    def _delete_selected(self):
        key = self._current_key()
        if key:
            self._delete_item(key)

    def _add_item(self):
        _AddDialog(self.root, self.config, self._sync, self._refresh_list)

    def _reset_default(self):
        if not self.config.items:
            messagebox.showinfo("恢复默认", "当前已经是默认状态。")
            return
        if messagebox.askyesno("确认", "恢复默认设置？将清空所有连点按键。"):
            self.config.items.clear()
            self._sync()
            self._refresh_list()

    def _open_config_dir(self):
        import os
        import subprocess
        from config import get_app_dir
        path = get_app_dir()
        try:
            subprocess.Popen(["explorer", path])
        except OSError:
            messagebox.showinfo("配置目录", path)

    # ============ 启用/禁用 ============
    def _toggle_active(self):
        self.engine.toggle_active()
        save_config(self.config)

    def _on_state_change(self, active):
        """引擎可能从托盘线程回调，必须切回 UI 线程"""
        try:
            self.root.after(0, lambda: self._apply_active_ui(active))
        except Exception:
            pass

    def _apply_active_ui(self, active):
        if active:
            self.state_label.config(text="● 已启用", foreground="#159c2f")
            self.toggle_btn.config(text="禁用")
        else:
            self.state_label.config(text="● 已禁用", foreground="#c0392b")
            self.toggle_btn.config(text="启用")
        if self._tray_icon is not None:
            try:
                self._tray_icon.title = f"{APP_NAME}（{'已启用' if active else '已禁用'}）"
            except Exception:
                pass

    # ============ 托盘 ============
    def _setup_tray(self):
        try:
            image = Image.open(get_icon_path())
        except Exception:
            image = Image.new("RGB", (64, 64), "#1f6feb")

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._show_window, default=True),
            pystray.MenuItem("启用/禁用", self._tray_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(APP_NAME, image,
                                       f"{APP_NAME}（{'已启用' if self.engine.active else '已禁用'}）",
                                       menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _show_window(self, icon=None, item=None):
        try:
            self.root.after(0, self._restore_window)
        except Exception:
            pass

    def _restore_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _tray_toggle(self, icon=None, item=None):
        try:
            self.root.after(0, self._toggle_active)
        except Exception:
            pass

    def _on_minimize(self):
        self.root.withdraw()

    # ============ 退出 ============
    def _tray_quit(self, icon=None, item=None):
        try:
            self.root.after(0, self._quit)
        except Exception:
            pass

    def _quit(self):
        if self._closing:
            return
        self._closing = True
        save_config(self.config)
        self.engine.stop()
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class _AddDialog:
    """添加按键对话框（下拉选择，不再做全局键盘录制）"""

    def __init__(self, parent, config, on_change, on_refresh):
        self.config = config
        self.on_change = on_change
        self.on_refresh = on_refresh

        self.top = tk.Toplevel(parent)
        self.top.title("添加连点按键")
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.top.grab_set()

        body = ttk.Frame(self.top, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        self.key_var = tk.StringVar()
        self.interval_var = tk.StringVar(value=_fmt(DEFAULT_INTERVAL * 1000))
        self.press_var = tk.StringVar(value=_fmt(DEFAULT_PRESS * 1000))

        self._row(body, 0, "按键", self._key_widget(body))
        self._row(body, 1, "连点间隔 (ms)",
                  ttk.Entry(body, textvariable=self.interval_var, width=14,
                            justify="center"))
        self._row(body, 2, "按下时长 (ms)",
                  ttk.Entry(body, textvariable=self.press_var, width=14,
                            justify="center"))

        ttk.Label(body, foreground="#7a7a7a", justify="left",
                  text=(f"间隔范围 {MIN_INTERVAL * 1000:g}–{MAX_INTERVAL * 1000:g} ms；"
                        f"按下时长 ≤ 间隔。\n"
                        "间隔越小连点越快，间隔过小部分程序会丢键。")
                  ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.top.destroy
                   ).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(buttons, text="添加", command=self._submit
                   ).pack(side=tk.RIGHT)

        self.top.bind("<Return>", lambda e: self._submit())
        self.top.bind("<Escape>", lambda e: self.top.destroy())

        self.combo.focus_set()
        self._center(parent)
        self.top.wait_window(self.top)

    def _row(self, parent, row, label, widget):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           pady=4, padx=(0, 10))
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        return widget

    def _key_widget(self, parent):
        # 已使用的按键不再出现在候选里
        used = {item.key for item in self.config.items}
        self._choices = [(k, lbl) for k, lbl in _KEY_CHOICES if k not in used]
        values = [lbl for _, lbl in self._choices] or ["（所有按键均已添加）"]
        self.combo = ttk.Combobox(parent, textvariable=self.key_var,
                                  values=values, state="readonly", width=26)
        if self._choices:
            self.combo.current(0)
        return self.combo

    def _center(self, parent):
        self.top.update_idletasks()
        width = self.top.winfo_width()
        height = self.top.winfo_height()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        self.top.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _submit(self):
        label = self.key_var.get()
        key = _LABEL_TO_KEY.get(label)
        if not key:
            messagebox.showwarning("添加按键", "请选择要连点的按键。", parent=self.top)
            return

        interval = parse_interval(_ms_to_seconds(self.interval_var.get(),
                                                 DEFAULT_INTERVAL))
        press = parse_press(_ms_to_seconds(self.press_var.get(), DEFAULT_PRESS),
                            interval)

        if not self.config.add(ClickItem(key=key, interval=interval, press=press)):
            messagebox.showwarning("添加按键", f"{key} 已在列表中。", parent=self.top)
            return

        self.top.destroy()
        self.on_change()
        self.on_refresh()


def _ms_to_seconds(raw, default):
    try:
        return float(raw) / 1000.0
    except (TypeError, ValueError):
        return default
