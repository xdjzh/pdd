# -*- coding: utf-8 -*-
"""
tkinter 主界面 · 2026 潮流工作台视觉（浅色 Aurora / 深色 Midnight 双主题）。

左侧：墨紫渐变产品功能栏目（差评举报已上线，DSR 计算器 / 智能直通车 /
利润汇总为待开发占位），底部可一键切换深色 / 浅色。
右侧：页面标题 + 状态胶囊 + 三张数据卡 + 店铺表格 + 圆角操作按钮 + 状态栏。

差评举报页的表格严格只显示 6 列：
店铺名称 / 在线状态 / 1星 / 2星 / 3星 / 本次举报。
浏览器、控制台、日志都不出现在窗口里；扫码登录时才临时弹出可见浏览器，
登录成功立刻关闭，之后全部在后台无头运行。

本次改版只动视觉层；所有举报命令、定时任务、店铺隔离逻辑一行未改。
"""
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .engine import Engine
from .state import ShopStore, SettingsStore, app_base_dir
from . import autostart
from .theme import (PALETTES, Chip, PillButton, StatCard, draw_icon,
                    font_family, gradient, load_theme, mix, round_rect,
                    save_theme)


COLUMNS = (
    ("sel", "选", 44),
    ("name", "店铺名称", 200),
    ("online", "在线状态", 160),
    ("star1", "1星", 68),
    ("star2", "2星", 68),
    ("star3", "3星", 68),
    ("reported", "本次举报", 90),
)

# 左侧产品功能栏目：(key, 显示名, 是否已上线, 图标)
NAV_ITEMS = (
    ("report", "差评举报", True, "shield"),
    ("dsr", "DSR 计算器", False, "calc"),
    ("traffic", "智能直通车", False, "trend"),
    ("profit", "利润汇总", False, "chart"),
)

PLACEHOLDER = {
    "dsr": ("DSR 计算器",
            "按店铺与时间维度计算描述、服务、物流三项动态评分，"
            "给出趋势变化、同行对比与下滑预警。", "calc"),
    "traffic": ("智能直通车",
                "汇总推广计划消耗、点击与转化数据，辅助调整出价与关键词，"
                "并给出投产比异常的提示。", "trend"),
    "profit": ("利润汇总",
               "按订单、商品或店铺汇总货款、成本、佣金与推广费用，"
               "输出净利润与毛利报表。", "chart"),
}

SIDEBAR_W = 228


def _sizes():
    base = 10 if sys.platform == "win32" else 12
    return {"xs": max(8, base - 2), "sm": max(9, base - 1),
            "md": base, "lg": base + 2, "xl": base + 6, "num": base + 12}


# ------------------------------------------------------------------
# 版本与更新：引导壳 updater.py 在启动时静默装补丁，这里负责把结果
# 「说清楚」——显示当前版本、允许手动检查、发现新版主动下载并提示。
# ------------------------------------------------------------------
_BASE_TAG = "v1.0.0 基础版"


def _ver_state():
    """当前真正生效的版本（有补丁读补丁，没有就读 exe 内置基线）。"""
    try:
        import updater
        v = updater.version_info()
        return {
            "version": int(v.get("version") or 0),
            "base": int(v.get("base") or 0),
            "patched": bool(v.get("patched")),
            "label": str(v.get("label") or ""),
            "appliedAt": int(v.get("appliedAt") or 0),
        }
    except Exception:
        return {"version": 0, "base": 0, "patched": False,
                "label": "", "appliedAt": 0}


def _ver_short(v=None):
    v = v or _ver_state()
    m = re.match(r"^(v?\d+(?:\.\d+)+)", (v.get("label") or "").strip())
    tag = m.group(1) if m else _BASE_TAG
    if v.get("patched"):
        return "%s · 热更新 %s" % (tag, v.get("version") or "")
    return "%s · 内置版 %s" % (tag, v.get("version") or "")


def _ver_detail(v=None):
    v = v or _ver_state()
    lines = [
        "当前运行版本：%s" % ((v.get("label") or "").strip() or _BASE_TAG),
        "版本号：%s" % (v.get("version") or "（未读取到）"),
        "代码来源：%s" % ("云端热更新" if v.get("patched") else "exe 内置基线"),
        "exe 内置基线：%s" % (v.get("base") or "—"),
    ]
    if v.get("appliedAt"):
        lines.append("热更新安装时间：%s" %
                     time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(v["appliedAt"])))
    lines.append("更新方式：每次启动自动检查；也可点「检查更新」手动检查。")
    return "\n".join(lines)


def _endpoint():
    try:
        import updater
        ep = updater.endpoint()
        if ep:
            return ep
    except Exception:
        pass
    try:
        from .license import LicenseClient
        ep = LicenseClient().endpoint
        if ep:
            return ep
    except Exception:
        pass
    return ""


def _post_update(url, action, payload=None, timeout=8):
    import json
    import urllib.request
    body = json.dumps(dict(action=action, **(payload or {})),
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    if isinstance(d, dict) and isinstance(d.get("body"), str):
        try:
            d = json.loads(d["body"])
        except Exception:
            pass
    return d if isinstance(d, dict) else {}


class App(object):
    def __init__(self, root, store, license_session=None, minimized=False):
        self.root = root
        self.store = store
        self.license_session = license_session
        self.start_minimized = bool(minimized)
        self._license_blocked = False
        self._license_checking = False
        self._last_license_check = 0.0
        self.settings = SettingsStore(root=store.root)
        self.engine = Engine(store, settings=self.settings)
        self.fam = font_family()
        self.fs = _sizes()
        self.theme_file = os.path.join(store.root, "ui_theme.json")
        self.theme_name = load_theme(self.theme_file)
        self.t = PALETTES[self.theme_name]
        self.current_page = "report"
        self._nav_hit = []
        self._nav_hover = None
        self._closing = False
        self._tick_started = False
        self.picked = set()          # 表格里勾选的店铺 id（批量举报用）
        # 启动那一刻真正跑起来的版本（下载完补丁后也不会误显示成新版）
        self._running_ver = _ver_state()
        self._checking_update = False
        # 右下角托盘图标（纯 ctypes 实现）；起不来会自动退回「最小化到任务栏」
        self._tray = None
        self._locked = False
        self._tray_tip_at = 0.0
        self._tray_polling = False
        self._build_ui()
        self._init_tray()
        self.engine.start()
        if not self._tick_started:
            self._tick_started = True
            self.root.after(400, self._tick)
        # 开机自启时直接收到右下角托盘：不打扰用户，点托盘图标即可打开
        if getattr(self, "start_minimized", False):
            try:
                self.root.after(500, lambda: self.hide_to_tray(notify=False))
            except Exception:
                pass
        # 启动后静默检查一次；刚装过热更新或云端有新版都会主动弹窗告知
        self.root.after(1200, lambda: self._check_update(manual=False))

    # ---------- 界面骨架（可重复调用，用于切换主题时整体重绘） ----------
    def _build_ui(self):
        for w in list(self.root.winfo_children()):
            w.destroy()
        t = self.t
        self.root.title("PDD 工作台 · 差评举报")
        self.root.geometry("1140x660")
        self.root.minsize(1000, 540)
        self.root.configure(bg=t["bg"])
        self._setup_styles()

        outer = tk.Frame(self.root, bg=t["bg"])
        outer.pack(fill=tk.BOTH, expand=True)
        # 用 grid 锁死侧栏列宽：窗口被拉小时不再把侧栏压扁导致图标与标签叠字
        outer.grid_columnconfigure(0, minsize=SIDEBAR_W, weight=0)
        outer.grid_columnconfigure(1, weight=1)
        outer.grid_rowconfigure(0, weight=1)

        # ===== 左侧：渐变产品功能栏目 =====
        self.nav = tk.Canvas(outer, width=SIDEBAR_W, bg=t["sidebar_top"],
                             highlightthickness=0, bd=0)
        self.nav.grid(row=0, column=0, sticky="ns")
        self.nav.bind("<Configure>", lambda e: self._draw_sidebar())
        self.nav.bind("<Button-1>", self._on_nav_click)
        self.nav.bind("<Motion>", self._on_nav_motion)
        self.nav.bind("<Leave>", self._on_nav_leave)

        # ===== 右侧：内容区 =====
        right = tk.Frame(outer, bg=t["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        # 用 grid 行权重分配高度：内容区吸收剩余空间，状态栏永远不会被挤掉
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=0)
        right.grid_rowconfigure(2, weight=0)

        self.content = tk.Frame(right, bg=t["bg"])
        self.content.grid(row=0, column=0, sticky="nsew",
                          padx=26, pady=(22, 0))

        # 底部状态栏（所有页面共用）
        sep = tk.Frame(right, bg=t["border"], height=1)
        sep.grid(row=1, column=0, sticky="ew", padx=26, pady=(6, 0))
        status = tk.Frame(right, bg=t["bg"])
        status.grid(row=2, column=0, sticky="ew", padx=26, pady=(9, 11))
        tk.Label(status, text="●", bg=t["bg"], fg=t["accent"],
                 font=(self.fam, self.fs["sm"])).pack(side=tk.LEFT, padx=(0, 6))
        self.status_var = tk.StringVar(value="准备就绪")
        tk.Label(status, textvariable=self.status_var, bg=t["bg"],
                 fg=t["muted"], font=(self.fam, self.fs["xs"]),
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.account_var = tk.StringVar(
            value=self.license_session.label() if self.license_session
            else "数据保存在 pdd_data")
        tk.Label(status, textvariable=self.account_var, bg=t["bg"],
                 fg=t["muted"], font=(self.fam, self.fs["xs"])).pack(side=tk.RIGHT)
        self.version_var = tk.StringVar(value=_ver_short(self._running_ver))
        ver_label = tk.Label(status, textvariable=self.version_var, bg=t["bg"],
                             fg=t["accent"], cursor="hand2",
                             font=(self.fam, self.fs["xs"]))
        ver_label.pack(side=tk.RIGHT, padx=(0, 14))
        ver_label.bind("<Button-1>", lambda e: self.do_version_info())

        # 状态栏「锁定」「退出程序」：点 X 只是躲到右下角托盘，真退出走这里
        self.lock_label = tk.Label(status, text="锁定", bg=t["bg"],
                                   fg=t["muted"], cursor="hand2",
                                   font=(self.fam, self.fs["xs"]))
        self.lock_label.pack(side=tk.RIGHT, padx=(0, 14))
        self.lock_label.bind("<Button-1>", lambda e: self.lock_app())
        self.lock_label.bind("<Enter>",
                             lambda e: self.lock_label.configure(fg=t["accent"]))
        self.lock_label.bind("<Leave>",
                             lambda e: self.lock_label.configure(fg=t["muted"]))

        self.quit_label = tk.Label(status, text="退出程序", bg=t["bg"],
                                   fg=t["muted"], cursor="hand2",
                                   font=(self.fam, self.fs["xs"]))
        self.quit_label.pack(side=tk.RIGHT, padx=(0, 14))
        self.quit_label.bind("<Button-1>", lambda e: self.do_quit())
        self.quit_label.bind("<Enter>",
                             lambda e: self.quit_label.configure(fg=t["danger"]))
        self.quit_label.bind("<Leave>",
                             lambda e: self.quit_label.configure(fg=t["muted"]))

        self.pages = {"report": self._build_report_page(self.content)}
        for key in ("dsr", "traffic", "profit"):
            self.pages[key] = self._build_placeholder(self.content, key)
        self.switch_page(self.current_page)

        self._build_menu()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._draw_sidebar()

    def _setup_styles(self):
        t = self.t
        style = ttk.Style()
        try:
            style.theme_use("clam")     # 强制统一，避免各平台原生灰色皮肤
        except Exception:
            pass
        style.configure("Shop.Treeview",
                        background=t["card"], fieldbackground=t["card"],
                        foreground=t["text"], borderwidth=0, rowheight=40,
                        font=(self.fam, self.fs["md"]))
        style.map("Shop.Treeview",
                  background=[("selected", t["select"])],
                  foreground=[("selected", t["text"])])
        style.configure("Shop.Treeview.Heading",
                        background=t["head_bg"], foreground=t["muted"],
                        font=(self.fam, self.fs["sm"], "bold"),
                        relief="flat", borderwidth=0, padding=(10, 11))
        style.map("Shop.Treeview.Heading",
                  background=[("active", t["head_bg"])])
        try:
            style.layout("Shop.Treeview",
                         [("Treeview.treearea", {"sticky": "nswe"})])
        except Exception:
            pass
        try:
            style.layout("Slim.Vertical.TScrollbar",
                         [("Vertical.trough",
                           {"children": [("Vertical.thumb",
                                         {"sticky": "nswe"})],
                            "sticky": "ns"})])
            style.configure("Slim.Vertical.TScrollbar",
                            background=t["scroll"], troughcolor=t["card"],
                            borderwidth=0, relief="flat", arrowsize=0, width=9)
        except Exception:
            pass

    # ---------- 左侧渐变侧栏（全部 Canvas 绘制） ----------
    def _draw_sidebar(self):
        c = self.nav
        if not c.winfo_exists():
            return
        t = self.t
        w = c.winfo_width() or SIDEBAR_W
        h = c.winfo_height() or 660
        c.delete("all")
        gradient(c, 0, 0, w, h, t["sidebar_top"], t["sidebar_bottom"],
                 vertical=True)

        # 极端窄窗口下的防御布局：隐藏次要文字，避免和主标题叠在一起
        narrow = w < 200
        if narrow:
            round_rect(c, 14, 24, 42, 52, 9, t["accent"])
            c.create_rectangle(21, 44, 24, 48, fill="#FFFFFF", outline="#FFFFFF")
            c.create_rectangle(27, 40, 30, 48, fill="#FFFFFF", outline="#FFFFFF")
            c.create_rectangle(33, 35, 36, 48, fill="#FFFFFF", outline="#FFFFFF")
            c.create_text(50, 36, text="PDD 工作台", anchor="w",
                          fill=t["sidebar_text"],
                          font=(self.fam, self.fs["sm"], "bold"))
        else:
            round_rect(c, 22, 24, 58, 60, 11, t["accent"])
            c.create_rectangle(31, 48, 35, 53, fill="#FFFFFF", outline="#FFFFFF")
            c.create_rectangle(38, 42, 42, 53, fill="#FFFFFF", outline="#FFFFFF")
            c.create_rectangle(45, 35, 49, 53, fill="#FFFFFF", outline="#FFFFFF")
            c.create_text(70, 36, text="PDD 工作台", anchor="w",
                          fill=t["sidebar_text"],
                          font=(self.fam, self.fs["md"] + 1, "bold"))
            c.create_text(70, 53, text="商家运营工具箱", anchor="w",
                          fill=t["sidebar_sub"], font=(self.fam, self.fs["xs"]))

        c.create_line(22, 82, w - 22, 82, fill=t["sidebar_line"])
        c.create_text(22, 102,
                      text="功能模块" if narrow else "功能模块  ·  MODULES",
                      anchor="w", fill=t["sidebar_sub"],
                      font=(self.fam, self.fs["xs"]))

        self._nav_hit = []
        y0 = 122
        for key, label, ready, icon in NAV_ITEMS:
            active = (key == self.current_page)
            hovered = (self._nav_hover == key)
            y1, y2 = y0, y0 + 46
            x1, x2 = 14, w - 14
            if active:
                round_rect(c, x1, y1, x2, y2, 11, t["sidebar_active"])
                round_rect(c, x1 + 8, y1 + 13, x1 + 11, y2 - 13, 2,
                           t["accent_2"])
            elif hovered:
                round_rect(c, x1, y1, x2, y2, 11, t["sidebar_hover"])
            fg = t["sidebar_text"] if active or hovered else t["sidebar_item"]
            ic = t["accent_2"] if active else t["sidebar_item"]
            draw_icon(c, x1 + (16 if narrow else 26), y1 + 23, icon, ic,
                      size=14 if narrow else 16)
            c.create_text(x1 + (32 if narrow else 46), y1 + 23, text=label,
                          anchor="w", fill=fg,
                          font=(self.fam, self.fs["md"],
                                "bold" if active else "normal"))
            if not narrow:
                tag = "已上线" if ready else "待开发"
                c.create_text(x2 - 12, y1 + 23, text=tag, anchor="e",
                              fill=t["accent_2"] if ready else t["sidebar_sub"],
                              font=(self.fam, self.fs["xs"]))
            self._nav_hit.append((x1, y1, x2, y2, key))
            y0 += 52

        # 底部主题切换
        ty = max(y0 + 20, h - 88)
        c.create_line(22, ty - 14, w - 22, ty - 14, fill=t["sidebar_line"])
        if self._nav_hover == "theme":
            bg = mix(t["sidebar_hover"], t["accent"], 0.18)
        else:
            bg = t["sidebar_hover"]
        round_rect(c, 14, ty, w - 14, ty + 40, 11, bg)
        draw_icon(c, 32 if narrow else 38, ty + 20, "star",
                  t["accent_2"], size=14)
        label = "切换浅色" if self.theme_name == "dark" else "切换深色"
        c.create_text(48 if narrow else 56, ty + 20,
                      text="主题" if narrow else label, anchor="w",
                      fill=t["sidebar_text"], font=(self.fam, self.fs["sm"]))
        if not narrow:
            c.create_text(w - 26, ty + 20, text="◐", anchor="e",
                          fill=t["sidebar_sub"], font=(self.fam, self.fs["md"]))
        self._nav_hit.append((14, ty, w - 14, ty + 40, "theme"))
        c.create_text(22, h - 18, text=_ver_short(self._running_ver),
                      anchor="w", fill=t["sidebar_sub"],
                      font=(self.fam, self.fs["xs"]))

    @staticmethod
    def _hit(hits, x, y):
        for x1, y1, x2, y2, key in hits:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return key
        return None

    def _on_nav_motion(self, event):
        key = self._hit(self._nav_hit, event.x, event.y)
        if key != self._nav_hover:
            self._nav_hover = key
            self._draw_sidebar()
        self.nav.configure(cursor="hand2" if key else "arrow")

    def _on_nav_leave(self, _event):
        if self._nav_hover:
            self._nav_hover = None
            self._draw_sidebar()
        self.nav.configure(cursor="arrow")

    def _on_nav_click(self, event):
        key = self._hit(self._nav_hit, event.x, event.y)
        if key == "theme":
            self.toggle_theme()
        elif key:
            self.switch_page(key)

    def toggle_theme(self):
        """切换浅色 / 深色并整体重建界面，业务状态与后台任务不受影响。"""
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.t = PALETTES[self.theme_name]
        self._nav_hover = None
        save_theme(self.theme_file, self.theme_name)
        self._build_ui()
        self.status_var.set("已切换到%s主题，偏好已保存" %
                            ("深色" if self.theme_name == "dark" else "浅色"))

    def switch_page(self, key):
        """切换左侧功能栏目；非差评举报页显示「待开发」。"""
        if key not in self.pages:
            return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill=tk.BOTH, expand=True)
        self.current_page = key
        self._draw_sidebar()
        label = dict((k, v) for k, v, _r, _i in NAV_ITEMS).get(key, "")
        self.root.title("PDD 工作台 · " + label)

    # ---------- 差评举报页 ----------
    def _build_report_page(self, parent):
        t = self.t
        page = tk.Frame(parent, bg=t["bg"])
        # 同样用 grid 行权重：表格行吸收剩余高度，按钮栏永远保留自己的位置
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        # 页面标题 + 策略胶囊（固定高度留足两行文字，避免副标题被下方卡片盖住）
        head = tk.Frame(page, bg=t["bg"], height=58)
        head.grid(row=0, column=0, sticky="ew")
        head.pack_propagate(False)
        left = tk.Frame(head, bg=t["bg"])
        left.pack(side=tk.LEFT)
        tk.Label(left, text="差评举报", bg=t["bg"], fg=t["text"],
                 font=(self.fam, self.fs["xl"], "bold")).pack(anchor="w")
        tk.Label(left, text="多店铺在线管理 · 自动监测 1 / 2 / 3 星差评",
                 bg=t["bg"], fg=t["muted"],
                 font=(self.fam, self.fs["sm"])).pack(anchor="w", pady=(3, 0))
        Chip(head, t, "单入口模式 · 遇风控自动暂停",
             fam=self.fam, size=self.fs["xs"]).pack(side=tk.RIGHT, padx=(8, 0))
        # 自动任务开关（开机后自动登录+举报到上限）。点击切换，文字/配色随状态变化。
        self.auto_btn = PillButton(head, t, "自动任务：关", self.toggle_auto_task,
                                   kind="secondary", fam=self.fam, size=self.fs["xs"],
                                   icon="shield")
        self.auto_btn.pack(side=tk.RIGHT)
        self._refresh_auto_task_ui()

        # 三张数据卡（固定 96px 高，防止窗口变小时 pack 压扁卡片）
        stats = tk.Frame(page, bg=t["bg"], height=96)
        stats.grid(row=1, column=0, sticky="ew", pady=(16, 12))
        stats.pack_propagate(False)
        self.stat_online = StatCard(stats, t, "在线店铺", "保持后台在线",
                                    t["success"], t["success_soft"], "shop",
                                    fam=self.fam)
        self.stat_pending = StatCard(stats, t, "待处理差评", "1/2/3 星合计",
                                     t["warning"], t["warning_soft"], "star",
                                     fam=self.fam)
        self.stat_reported = StatCard(stats, t, "本次举报", "当前会话累计",
                                      t["accent"], t["accent_soft"], "send",
                                      fam=self.fam)
        for i, card in enumerate((self.stat_online, self.stat_pending,
                                  self.stat_reported)):
            card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                      padx=(0, 12 if i < 2 else 0))

        # 店铺表格卡片
        box = tk.Frame(page, bg=t["card"], highlightbackground=t["border"],
                       highlightthickness=1, highlightcolor=t["border"])
        box.grid(row=2, column=0, sticky="nsew")
        inner = tk.Frame(box, bg=t["card"])
        inner.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        cap = tk.Frame(inner, bg=t["card"])
        cap.pack(fill=tk.X, pady=(0, 10))
        tk.Label(cap, text="店铺列表", bg=t["card"], fg=t["text"],
                 font=(self.fam, self.fs["md"], "bold")).pack(side=tk.LEFT)
        self.count_var = tk.StringVar(value="共 0 家店铺")
        tk.Label(cap, textvariable=self.count_var, bg=t["card"],
                 fg=t["muted"], font=(self.fam, self.fs["xs"])).pack(side=tk.RIGHT)

        tf = tk.Frame(inner, bg=t["card"])
        tf.pack(fill=tk.BOTH, expand=True)
        cols = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(tf, columns=cols, show="headings",
                                 selectmode="browse", style="Shop.Treeview")
        for key, title, width in COLUMNS:
            if key == "sel":
                # 点表头「选」= 全选 / 全不选
                self.tree.heading(key, text=title, command=self.toggle_pick_all)
            else:
                self.tree.heading(key, text=title)
            anchor = tk.CENTER if key not in ("name", "online") else tk.W
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "name"))
        sb = ttk.Scrollbar(tf, orient=tk.VERTICAL,
                           command=self.tree.yview,
                           style="Slim.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))

        self.tree.tag_configure("online", foreground=t["text"])
        self.tree.tag_configure("working", foreground=t["accent"])
        self.tree.tag_configure("offline", foreground=t["danger"])
        self.tree.tag_configure("idle", foreground=t["muted"])
        self.tree.tag_configure("stripe", background=t["row_alt"])
        # 勾选行用主题色底 + 主题色字，和普通行区分开（用户反馈「选择不明显」）
        self.tree.tag_configure("picked", background=t["accent_soft"],
                               foreground=t["accent"])
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: self.do_login())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Button-3>", self._popup_menu)

        # 圆角操作按钮（同样固定高度）
        bar = tk.Frame(page, bg=t["bg"], height=42)
        bar.grid(row=3, column=0, sticky="ew", pady=(12, 4))
        bar.pack_propagate(False)
        kw = {"fam": self.fam, "size": self.fs["md"]}
        PillButton(bar, t, "添加店铺", self.do_add, kind="primary",
                   icon="plus", **kw).pack(side=tk.LEFT, padx=(0, 8))
        PillButton(bar, t, "开始举报", self.do_start, kind="primary",
                   **kw).pack(side=tk.LEFT, padx=(0, 8))
        PillButton(bar, t, "全部开始", self.do_start_all, kind="secondary",
                   **kw).pack(side=tk.LEFT, padx=(0, 8))
        PillButton(bar, t, "扫码登录", self.do_login, kind="secondary",
                   **kw).pack(side=tk.LEFT, padx=(0, 8))
        PillButton(bar, t, "刷新数量", self.do_refresh, kind="secondary",
                   **kw).pack(side=tk.LEFT, padx=(0, 8))
        PillButton(bar, t, "停止", self.do_stop, kind="danger",
                   **kw).pack(side=tk.LEFT)
        PillButton(bar, t, "检查更新", self.do_check_update,
                   kind="ghost", **kw).pack(side=tk.RIGHT, padx=(0, 8))
        PillButton(bar, t, "使用说明", self.do_help, kind="ghost",
                   **kw).pack(side=tk.RIGHT)
        return page

    # ---------- 待开发占位页 ----------
    def _build_placeholder(self, parent, key):
        title, desc, icon = PLACEHOLDER[key]
        t = self.t
        page = tk.Frame(parent, bg=t["bg"])
        c = tk.Canvas(page, bg=t["bg"], highlightthickness=0, bd=0)
        c.pack(fill=tk.BOTH, expand=True)
        btn = PillButton(c, t, "返回差评举报",
                         lambda: self.switch_page("report"),
                         kind="secondary", fam=self.fam, size=self.fs["md"])
        win = [None]

        def draw(_event=None):
            w = max(400, c.winfo_width())
            h = max(340, c.winfo_height())
            cw = min(620, w - 40)
            ch = 336
            x1 = (w - cw) / 2.0
            y1 = max(12, (h - ch) / 2.0 - 12)
            cx = w / 2.0
            c.delete("all")
            round_rect(c, x1, y1, x1 + cw, y1 + ch, 20, t["card"],
                       outline=t["border"], ow=1)
            gradient(c, x1 + 24, y1 + 2, x1 + cw - 24, y1 + 5,
                     t["accent"], t["accent_2"], vertical=False)
            round_rect(c, cx - 32, y1 + 32, cx + 32, y1 + 96, 18,
                       t["accent_soft"])
            draw_icon(c, cx, y1 + 64, icon, t["accent"], size=30)
            c.create_text(cx, y1 + 122, text=title, fill=t["text"],
                          font=(self.fam, self.fs["lg"] + 2, "bold"))
            chip_w = 116
            round_rect(c, cx - chip_w / 2, y1 + 146,
                       cx + chip_w / 2, y1 + 174, 13, t["warning_soft"])
            c.create_text(cx, y1 + 160, text="功能待开发", fill=t["warning"],
                          font=(self.fam, self.fs["sm"], "bold"))
            c.create_text(cx, y1 + 208, text=desc, fill=t["text_2"],
                          font=(self.fam, self.fs["md"]), width=cw - 110,
                          justify=tk.CENTER)
            c.create_text(cx, y1 + 250,
                          text="该模块已在产品规划中，当前版本请继续使用左侧「差评举报」。",
                          fill=t["muted"], font=(self.fam, self.fs["xs"]),
                          width=cw - 90, justify=tk.CENTER)
            if win[0] is None:
                win[0] = c.create_window(cx, y1 + ch - 36, window=btn,
                                         anchor=tk.CENTER)
            else:
                c.coords(win[0], cx, y1 + ch - 36)
                c.tag_raise(win[0])

        c.bind("<Configure>", draw)
        draw()
        return page

    # ---------- 菜单 ----------
    def _menu(self, parent, tearoff=0):
        t = self.t
        try:
            return tk.Menu(parent, tearoff=tearoff, bg=t["card"],
                           fg=t["text"], activebackground=t["accent_soft"],
                           activeforeground=t["accent"], bd=0, relief="flat")
        except tk.TclError:
            return tk.Menu(parent, tearoff=tearoff)

    def _build_menu(self):
        m = self._menu(self.root)
        shop = self._menu(m)
        shop.add_command(label="添加店铺（直接扫码）", command=self.do_add)
        shop.add_command(label="重命名", command=self.do_rename)
        shop.add_command(label="重新获取店铺名", command=self.do_shop_name)
        shop.add_command(label="删除店铺", command=self.do_remove)
        shop.add_separator()
        shop.add_command(label="扫码登录", command=self.do_login)
        shop.add_command(label="取消扫码", command=lambda: self._call("cancel_login"))
        shop.add_separator()
        shop.add_command(label="打开店铺目录", command=self.do_open_dir)
        shop.add_command(label="查看举报日志", command=self.do_open_log)
        m.add_cascade(label="店铺", menu=shop)

        ops = self._menu(m)
        ops.add_command(label="刷新 1/2/3 星数量", command=self.do_refresh)
        ops.add_command(label="上报页面诊断（自动回传）",
                        command=self.do_star_diag)
        ops.add_command(label="开始举报", command=self.do_start)
        ops.add_command(label="全部开始", command=self.do_start_all)
        ops.add_command(label="停止", command=self.do_stop)
        ops.add_command(label="全部停止", command=self.do_stop_all)
        ops.add_separator()
        ops.add_command(label="清除当日上限标记", command=self.do_clear_limit)
        m.add_cascade(label="操作", menu=ops)

        auto = self._menu(m)
        self._auto_menu_var = tk.BooleanVar(value=bool(self.settings.get("auto_task")))
        auto.add_checkbutton(label="启用自动任务（每天开机自动登录+举报到上限）",
                             variable=self._auto_menu_var,
                             command=self.toggle_auto_task)
        self._startup_menu_var = tk.BooleanVar(value=autostart.is_enabled())
        auto.add_checkbutton(label="开机自动启动本软件",
                             variable=self._startup_menu_var,
                             command=self.toggle_auto_startup)
        auto.add_separator()
        auto.add_command(label="查看自动任务说明", command=self.do_auto_help)
        m.add_cascade(label="自动任务", menu=auto)

        nav = self._menu(m)
        for key, label, _ready, _icon in NAV_ITEMS:
            nav.add_command(label=label,
                            command=lambda k=key: self.switch_page(k))
        m.add_cascade(label="功能", menu=nav)

        view = self._menu(m)
        view.add_command(label="切换浅色 / 深色主题", command=self.toggle_theme)
        m.add_cascade(label="外观", menu=view)

        acc = self._menu(m)
        acc.add_command(label="账号与授权信息", command=self.do_account_info)
        acc.add_command(label="立即校验授权状态", command=self.do_license_check)
        acc.add_separator()
        acc.add_command(label="退出登录（重启后重新登录）", command=self.do_logout)
        m.add_cascade(label="账号", menu=acc)

        setting = self._menu(m)
        setting.add_command(label="打开设置窗口…", command=self.do_settings)
        setting.add_separator()
        setting.add_command(label="隐藏到右下角托盘",
                            command=lambda: self.hide_to_tray(notify=False))
        setting.add_command(label="锁定软件（需密码解锁）", command=self.lock_app)
        m.add_cascade(label="设置", menu=setting)

        helpm = self._menu(m)
        helpm.add_command(label="检查更新", command=self.do_check_update)
        helpm.add_command(label="版本信息", command=self.do_version_info)
        helpm.add_separator()
        helpm.add_command(label="浏览器自检（扫码窗口打不开时）",
                          command=self.do_browser_check)
        helpm.add_command(label="使用说明", command=self.do_help)
        m.add_cascade(label="帮助", menu=helpm)

        # 点窗口 X 只是躲到右下角托盘，真正退出走这里（状态栏也有同名入口）
        m.add_separator()
        m.add_command(label="退出程序", command=self.do_quit)
        self.root.config(menu=m)

        self.popup = self._menu(self.root)
        self.popup.add_command(label="扫码登录", command=self.do_login)
        self.popup.add_command(label="刷新数量", command=self.do_refresh)
        self.popup.add_command(label="上报页面诊断", command=self.do_star_diag)
        self.popup.add_command(label="开始举报", command=self.do_start)
        self.popup.add_command(label="停止", command=self.do_stop)
        self.popup.add_separator()
        self.popup.add_command(label="重命名", command=self.do_rename)
        self.popup.add_command(label="重新获取店铺名", command=self.do_shop_name)
        self.popup.add_command(label="删除店铺", command=self.do_remove)

    # ---------- 定时刷新 ----------
    def _visible(self):
        """窗口是否正显示着（隐藏到托盘 / 最小化时为 False）。"""
        try:
            return self.root.state() not in ("withdrawn", "iconic")
        except Exception:
            return True

    def _toast(self, title, text):
        """窗口不可见时的提醒：只发托盘气泡 + 状态栏，绝不弹模态框
        （父窗口隐藏时弹框会卡住 Tk 事件循环，界面看起来就死掉了）。"""
        try:
            self.status_var.set("%s：%s" % (title, str(text).split("\n")[0]))
        except Exception:
            pass
        if self._tray:
            try:
                self._tray.notify(title, text)
            except Exception:
                pass

    def _tick(self):
        if self._closing:
            return
        try:
            self._render()
            now = time.time()
            if now - self._tray_tip_at > 5:
                self._tray_tip_at = now
                self._update_tray_tip()
            if self.license_session and not self._license_checking and \
                    time.time() - self._last_license_check > 900:
                self._last_license_check = time.time()
                self._async_license_check()
            for ev in self.engine.pop_events():
                if ev[0] in ("auto_task", "multi_report"):
                    # 自动任务 / 批量举报的进度与结果只写状态栏，不弹窗打扰
                    try:
                        self.status_var.set(ev[1])
                    except Exception:
                        pass
                    continue
                if ev[0] == "login_failed":
                    # 扫码窗口没弹出来：把真实原因摆到用户面前，别再只写一行状态栏
                    _, sid, name, detail = ev
                    self._browser_fail_detail = detail
                    self.status_var.set("扫码窗口打不开：%s" % detail.split("\n")[0])
                    if self._locked or not self._visible():
                        self._toast("扫码窗口打不开", detail)
                    else:
                        messagebox.showerror(
                            "扫码窗口打不开",
                            "店铺：%s\n\n%s" % (name, detail),
                            parent=self.root)
                    continue
                if ev[0] == "login_failed_path":
                    _, sid, path = ev
                    self.status_var.set("浏览器诊断已保存：%s" % path)
                    try:
                        self._browser_fail_path = path
                    except Exception:
                        pass
                    continue
                if ev[0] == "browser_report":
                    _, text, path = ev
                    if self._locked or not self._visible():
                        self._toast("浏览器自检完成", "报告已保存：%s" % path)
                    else:
                        messagebox.showinfo(
                            "浏览器自检完成",
                            "%s\n\n报告已保存到：\n%s" % (text[-1200:], path),
                            parent=self.root)
                    continue
                if ev[0] == "daily_limit":
                    _, sid, name, reported = ev
                    title = "已到达当日举报上限"
                    text = ("店铺：%s\n\n平台已触发风控，本店铺已自动暂停。\n"
                            "本次已举报 %d 条。\n\n"
                            "明天额度恢复后点「开始举报」即可继续，"
                            "登录态会一直保持在线。" % (name, reported))
                    if self._locked or not self._visible():
                        self._toast(title, text)
                    else:
                        messagebox.showwarning(title, text, parent=self.root)
        except Exception:
            pass
        self.root.after(800, self._tick)

    def _render(self):
        states = self.engine.snapshot()
        existing = set(self.tree.get_children())
        wanted = set(states.keys())
        for sid in existing - wanted:
            self.tree.delete(sid)
        # 被删掉的店铺从勾选集里清掉，避免拿旧 id 去举报
        if self.picked - wanted:
            self.picked &= wanted
        for sid, st in states.items():
            dot = "●  " if st.get("online") else "○  "
            stars = [st.get("star1", 0), st.get("star2", 0), st.get("star3", 0)]
            # 没拿到可信读数就显示「—」；旧版会把同一个旧分页值填进三列（7/7/7）
            if not st.get("stars_ok", False):
                stars = ["—", "—", "—"]
            mark = "☑" if sid in self.picked else "☐"
            values = (mark, st["name"], dot + str(st["status"]),
                      stars[0], stars[1], stars[2], st["reported"])
            if sid in existing:
                self.tree.item(sid, values=values)
            else:
                self.tree.insert("", tk.END, iid=sid, values=values)
        for i, sid in enumerate(self.tree.get_children()):
            st = states.get(sid)
            if not st:
                continue
            if sid in self.picked:
                tags = ["picked"]          # 勾选行只上 picked，保证底色/字色压过状态色
            else:
                tags = [self._tag(st)]
                if i % 2:
                    tags.append("stripe")
            self.tree.item(sid, tags=tuple(tags))

        online = sum(1 for st in states.values() if st.get("online"))
        pending = reported = 0
        for st in states.values():
            if st.get("stars_ok", False):
                for k in ("star1", "star2", "star3"):
                    pending += self._as_int(st.get(k))
            reported += self._as_int(st.get("reported"))
        self.stat_online.set_value(online, "当前保持在线")
        self.stat_pending.set_value(pending, "1/2/3 星合计")
        self.stat_reported.set_value(reported, "本次会话累计")
        self._update_pick_hint()
        self._show_detail()

    # ---------- 勾选（批量举报） ----------
    def _update_pick_hint(self):
        """店铺列表右上角实时显示「共几家 / 已勾选几家」，让选择看得见。"""
        try:
            total = len(self.tree.get_children())
            n = len(self.picked)
            if n:
                self.count_var.set("共 %d 家店铺 · 已勾选 %d 家" % (total, n))
            else:
                self.count_var.set("共 %d 家店铺 · 点左侧方框可多选" % total)
        except Exception:
            pass

    def _toggle_pick(self, sid):
        if not sid:
            return
        if sid in self.picked:
            self.picked.discard(sid)
        else:
            self.picked.add(sid)
        self._render()

    def toggle_pick_all(self):
        """点表头「选」：全部勾选 / 全部取消。"""
        ids = list(self.tree.get_children())
        if ids and all(i in self.picked for i in ids):
            self.picked.clear()
            self.status_var.set("已取消全部勾选")
        else:
            self.picked.update(ids)
            self.status_var.set("已勾选全部 %d 家店铺，点「开始举报」批量执行" % len(ids))
        self._render()

    def _on_tree_click(self, event):
        """点第一列的方框勾选/取消；点其它列仍按普通选中处理。"""
        try:
            if self.tree.identify_region(event.x, event.y) != "cell":
                return None
            if self.tree.identify_column(event.x) != "#1":
                return None
            self._toggle_pick(self.tree.identify_row(event.y))
            return "break"
        except Exception:
            return None

    @staticmethod
    def _as_int(value):
        # 兼容「7?」这类待确认标记，数据卡仍能按数字累加
        m = re.search(r"-?\d+", str(value))
        return int(m.group()) if m else 0

    @staticmethod
    def _tag(st):
        s = st["status"]
        if st["online"] and ("举报中" in s or "扫码中" in s or "连接中" in s
                             or "刷新" in s):
            return "working"
        if "上限" in s or "掉线" in s or "失败" in s or "异常" in s or "超时" in s:
            return "offline"
        if st["online"]:
            return "online"
        return "idle"

    def _popup_menu(self, event):
        """右键菜单：先选中光标所在行，再弹出。"""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        try:
            self.popup.tk_popup(event.x_root, event.y_root)
        finally:
            self.popup.grab_release()

    def _show_detail(self):
        sid = self.selected()
        if not sid:
            return
        st = self.engine.state_for(sid)
        msg = st.get("message") or ""
        if not st.get("stars_ok", False):
            note = st.get("star_note") or ""
            msg = ("未读到评价数量（显示 —）：" + note) if note else \
                  "未读到评价数量（显示 —），点「刷新数量」重试"
            msg += "  ·  诊断已自动回传"
        extra = ("最近查询 %s" % st["last_check"]) if st.get("last_check") else ""
        self.status_var.set(("  ·  ".join(x for x in (msg, extra) if x))
                            or "准备就绪")

    # ---------- 操作（与旧版完全一致） ----------
    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _call(self, cmd, sid=None, **kw):
        sid = sid or self.selected()
        if not sid:
            messagebox.showinfo("提示", "请先选中一个店铺", parent=self.root)
            return False
        self.engine.call(cmd, sid, **kw)
        return True

    def do_add(self):
        """点「添加店铺」= 直接打开扫码登录，店铺名登录成功后自动获取。"""
        if any("扫码中" in (st.get("status") or "")
               for st in self.engine.snapshot().values()):
            messagebox.showinfo(
                "提示", "已有店铺正在扫码，请先完成或取消后再添加。",
                parent=self.root)
            return
        existing = set(s["name"] for s in self.store.list())
        seq = len(existing) + 1
        name = "待登录店铺 %d" % seq
        while name in existing:
            seq += 1
            name = "待登录店铺 %d" % seq
        try:
            shop = self.store.add(name, auto_name=True)
        except ValueError as e:
            messagebox.showwarning("无法添加", str(e), parent=self.root)
            return
        # 添加后立即进入扫码，不需要再点一次「扫码登录」
        # new_shop=True：扫码成功后若自动任务开着，引擎会立刻给这家补跑一次举报
        self.engine.add_shop(shop, then="login", new_shop=True)
        self.status_var.set(
            "正在打开扫码登录窗口（默认二维码）· 若 10 秒内没弹出来，"
            "看错误提示或用菜单「帮助 → 浏览器自检」"
            + ("· 自动任务已开，登录成功后将自动补跑举报"
               if self.settings.get("auto_task") else ""))

    def do_rename(self):
        sid = self.selected()
        if not sid:
            return
        old = self.store.get(sid)
        name = simpledialog.askstring("重命名", "新的店铺名称：",
                                      initialvalue=old["name"] if old else "",
                                      parent=self.root)
        if not name:
            return
        try:
            self.store.rename(sid, name)
        except ValueError as e:
            messagebox.showwarning("无法重命名", str(e), parent=self.root)
            return
        self.engine.rename_shop(sid, name)

    def do_shop_name(self):
        """重新从商家后台抓取店铺名，可覆盖之前抓错的宣传语。"""
        sid = self.selected()
        if sid:
            self._call("shop_name", sid)
            self.status_var.set("正在从商家后台重新读取店铺名…")

    def do_remove(self):
        sid = self.selected()
        if not sid:
            return
        shop = self.store.get(sid)
        name = shop["name"] if shop else sid
        if not messagebox.askyesno(
                "删除店铺", "确定移除「%s」吗？\n\n"
                "登录态和已举报台账会保留在 pdd_data 目录里，"
                "以后用同名重新添加仍可继续。" % name, parent=self.root):
            return
        self.engine.remove_shop(sid)
        self.store.remove(sid)
        self.picked.discard(sid)

    def do_login(self):
        if self._call("login"):
            self.status_var.set(
                "已打开扫码登录窗口（默认切到二维码）· 登录成功后自动获取店铺名并关闭窗口")

    def do_refresh(self):
        self._call("refresh")

    def do_star_diag(self):
        """生成桌面诊断报告：纯本地文本 + 页面截图，不依赖云回传。"""
        sid = self.selected()
        if sid:
            self._call("star_diag", sid)
        messagebox.showinfo(
            "页面诊断（桌面文件）",
            "正在生成诊断报告（约 6 秒）。\n\n"
            "完成后会在桌面出现两个文件：\n"
            "  PDD诊断_<店铺名>_<时间>.txt  （页面结构、导航、接口）\n"
            "  PDD诊断_<店铺名>_<时间>_页面截图.png\n\n"
            "请把这两个文件直接发给开发者，不需要改动内容。\n"
            "诊断完全在本机生成，即使软件显示『离线』也能用。",
            parent=self.root)
        self.root.after(6000,
                        lambda p=self.store.shop_dir(sid or ""): self._open(p))

    def _license_ok(self):
        """开始举报前复核授权：吊销 / 到期会即时挡住。"""
        if not self.license_session:
            return True                      # 源码调试模式不挡
        if self._license_blocked:
            messagebox.showerror("授权已失效",
                                 "账号授权已失效，请重启软件重新登录，"
                                 "或联系软件提供方。", parent=self.root)
            return False
        ok, msg = self.license_session.check_online()
        if not ok:
            self._license_blocked = True
            messagebox.showerror("授权已失效",
                                 "%s\n\n请重启软件重新登录，或联系软件提供方。" % msg,
                                 parent=self.root)
            return False
        self.account_var.set(self.license_session.label())
        return True

    def _async_license_check(self):
        """每 15 分钟在后台复核一次，不卡界面。"""
        self._license_checking = True

        def worker():
            try:
                ok, msg = self.license_session.check_online()
            except Exception as e:
                ok, msg = False, str(e)[:120]

            def done():
                self._license_checking = False
                if ok:
                    self.account_var.set(self.license_session.label())
                else:
                    self._license_blocked = True
                    self.status_var.set("授权已失效：%s" % msg)
                    if self._locked or not self._visible():
                        self._toast("授权已失效", "%s\n\n请重启软件重新登录。"
                                    % msg)
                    else:
                        messagebox.showerror("授权已失效",
                                             "%s\n\n请重启软件重新登录。" % msg,
                                             parent=self.root)
            try:
                self.root.after(0, done)
            except Exception:
                self._license_checking = False
        threading.Thread(target=worker, daemon=True).start()

    def do_account_info(self):
        if not self.license_session:
            messagebox.showinfo(
                "账号", "源码运行模式：未启用授权校验。\n\n" +
                _ver_detail(self._running_ver), parent=self.root)
            return
        messagebox.showinfo(
            "账号与授权",
            self.license_session.detail() +
            "\n\n离线可用 72 小时，联网后自动续期。\n\n" +
            _ver_detail(self._running_ver), parent=self.root)

    # ---------- 版本与更新 ----------
    def do_version_info(self):
        messagebox.showinfo(
            "版本信息",
            _ver_detail(self._running_ver) +
            "\n\n云端发布新版后，关闭并重新打开软件即自动生效；"
            "也可以点「检查更新」立即下载。",
            parent=self.root)

    def do_check_update(self):
        self._check_update(manual=True)

    def do_browser_check(self):
        """浏览器自检：扫码窗口打不开时，一键生成明确原因（并保存到桌面）。"""
        self.status_var.set("正在自检浏览器内核，请稍候…")
        self.engine.call("browser_check")

    def _check_update(self, manual=False):
        """后台查云端；发现新版直接下载安装，下次启动生效。"""
        if self._checking_update:
            if manual:
                messagebox.showinfo("检查更新", "正在检查中，请稍候…",
                                    parent=self.root)
            return
        self._checking_update = True

        def worker():
            cur = self._running_ver
            out = {"cur": cur, "remote": None, "error": "",
                   "downloaded": 0, "download_err": ""}
            try:
                ep = _endpoint()
                if not ep:
                    raise RuntimeError("未配置更新服务器地址")
                out["remote"] = _post_update(
                    ep, "check_update", {"appVersion": cur["version"]})
            except Exception as e:
                out["error"] = str(e)[:140]
            r = out["remote"] or {}
            try:
                remote_ver = int(r.get("version") or 0)
            except (TypeError, ValueError):
                remote_ver = 0
            if r.get("hasUpdate") and remote_ver > cur["version"]:
                try:
                    import updater
                    out["downloaded"] = int(
                        updater.apply_update(timeout_check=6,
                                             timeout_get=25) or 0)
                except Exception as e:
                    out["download_err"] = str(e)[:140]
            try:
                self.root.after(0,
                                lambda: self._on_update_checked(out, manual))
            except Exception:
                self._checking_update = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, out, manual):
        self._checking_update = False
        cur = out["cur"]
        r = out["remote"] or {}
        try:
            self.version_var.set(_ver_short(cur))
        except Exception:
            pass
        label = ((r.get("label") or "").strip() or
                 ("版本 " + str(r.get("version") or "")))
        changelog = (r.get("changelog") or "").strip()

        if (self._locked or not self._visible()) and not manual:
            # 藏在托盘/锁定时不弹模态框（父窗口不可见会卡住界面），
            # 改成状态栏 + 托盘气泡，用户点开主界面时再问也不迟。
            if out["downloaded"] and out["downloaded"] > cur["version"]:
                self._toast("更新已下载",
                            "新版 %s 已就绪，关闭重开软件即生效。" % label)
            elif out["error"]:
                try:
                    self.status_var.set("检查更新失败：%s" % out["error"][:60])
                except Exception:
                    pass
            elif r.get("hasUpdate"):
                self._toast("发现新版本 %s" % label,
                            "下载未完成，关闭重开软件会自动重试。")
            elif (cur["patched"] and cur["appliedAt"]
                    and time.time() - cur["appliedAt"] < 300):
                self._toast("已自动更新",
                            "本次启动已加载 %s" % (cur["label"] or "最新版"))
            return

        if out["downloaded"] and out["downloaded"] > cur["version"]:
            messagebox.showinfo(
                "更新已下载",
                "已下载新版：%s\n版本号：%s\n\n"
                "关闭并重新打开软件即可生效。当前窗口继续运行现有版本，"
                "不影响正在进行的任务。%s"
                % (label, out["downloaded"],
                   ("\n\n本次更新：\n" + changelog[:200]) if changelog else ""),
                parent=self.root)
            return

        # 用户反馈「关掉重开也没提示」：启动阶段刚装上补丁时主动告知
        just_applied = bool(cur["patched"] and cur["appliedAt"] and
                            time.time() - cur["appliedAt"] < 300)
        if just_applied and not manual and not out["error"]:
            messagebox.showinfo(
                "已自动更新",
                "本次启动已自动加载热更新：\n\n%s\n版本号 %s\n安装时间 %s\n\n"
                "右下角可随时查看当前版本；点「检查更新」可手动检查。"
                % (cur["label"] or "最新版", cur["version"],
                   time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(cur["appliedAt"]))),
                parent=self.root)
            return

        if not manual:
            return  # 已是最新，安静不打扰

        if out["error"]:
            messagebox.showwarning(
                "检查更新", "检查失败：%s\n\n当前仍是 %s" %
                (out["error"], _ver_short(cur)), parent=self.root)
            return
        if r.get("hasUpdate"):
            messagebox.showwarning(
                "检查更新",
                "发现新版 %s，但下载未完成%s。\n\n"
                "关闭并重新打开软件会自动重试。" %
                (label,
                 "（" + out["download_err"] + "）" if out["download_err"] else ""),
                parent=self.root)
            return
        messagebox.showinfo(
            "检查更新",
            "已是最新版本。\n\n" + _ver_detail(cur) +
            "\n\n云端版本：%s" % (r.get("version") or "未返回"),
            parent=self.root)

    def do_license_check(self):
        if not self.license_session:
            return
        ok, msg = self.license_session.check_online()
        if ok:
            self._license_blocked = False
            self.account_var.set(self.license_session.label())
            messagebox.showinfo("授权状态", "授权有效\n\n" +
                                self.license_session.label(), parent=self.root)
        else:
            self._license_blocked = True
            messagebox.showerror("授权已失效", msg, parent=self.root)

    def do_logout(self):
        if not messagebox.askyesno(
                "退出登录", "退出后本机将不再保留登录状态，"
                "下次打开需要重新登录并验证激活码。\n\n现在退出软件吗？",
                parent=self.root):
            return
        if self.license_session:
            self.license_session.logout()
        self._closing = True
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.root.destroy()

    def do_start(self):
        if not self._license_ok():
            return
        # 1) 有勾选 → 批量串行举报（一家跑完再跑下一家，避免同时打接口被风控）
        #    按表格里的顺序跑，和用户看到的排序一致
        order = list(self.tree.get_children())
        ids = [sid for sid in order if sid in self.picked
               and self.engine.state_for(sid).get("online")]
        if ids:
            offline = len(self.picked) - len(ids)
            limited = [self.engine.state_for(sid)["name"] for sid in ids
                       if self.engine.state_for(sid).get("daily_limit")]
            msg = ("将依次举报已勾选的 %d 家店铺（串行执行，一家跑完再跑下一家）。"
                   % len(ids))
            if offline:
                msg += "\n\n另有 %d 家未登录，已跳过。" % offline
            if limited:
                msg += "\n\n其中 %d 家今天已触发风控，会自动跳过：\n%s" % (
                    len(limited), "、".join(limited[:5]))
            if not messagebox.askokcancel("批量举报", msg, parent=self.root):
                return
            self.engine.report_many(ids)
            self.status_var.set("已开始批量举报 %d 家店铺…" % len(ids))
            return
        # 2) 没勾选 → 保持原来的单选行为
        if self.selected():
            st = self.engine.state_for(self.selected())
            if st.get("daily_limit") and not messagebox.askyesno(
                    "今日已达上限", "该店铺今天已触发风控。\n"
                    "仍要尝试吗？（可能需要先清除上限标记）", parent=self.root):
                return
        self._call("start_report")

    def do_start_all(self):
        if not self._license_ok():
            return
        ids = [sid for sid, st in self.engine.snapshot().items()
               if st.get("online") and not st.get("daily_limit")]
        if not ids:
            self.status_var.set("没有可举报的在线店铺（未登录或已到上限）")
            return
        self.engine.report_many(ids)
        self.status_var.set("已开始全部举报：%d 家店铺（串行执行）…" % len(ids))

    # ---------- 自动任务 / 开机自启 ----------
    def _refresh_auto_task_ui(self):
        """按当前设置刷新自动任务按钮文字与配色。"""
        try:
            on = bool(self.settings.get("auto_task"))
            txt = "自动任务：开" if on else "自动任务：关"
            kind = "primary" if on else "secondary"
            if hasattr(self, "auto_btn"):
                self.auto_btn.set_text(txt, kind=kind)
        except Exception:
            pass

    def toggle_auto_task(self):
        """一键开/关自动任务。关掉即「手动取消」，引擎下轮立即停止。"""
        on = bool(self.settings.get("auto_task"))
        new = not on
        self.settings.set("auto_task", new)
        self._refresh_auto_task_ui()
        try:
            self._auto_menu_var.set(new)
        except Exception:
            pass
        if new:
            # 开启时顺带询问是否注册开机自启
            if not autostart.is_enabled():
                if messagebox.askyesno(
                        "开机自启", "自动任务已开启。\n\n"
                        "是否同时设置「开机自动启动本软件」？\n"
                        "这样每天开机后会自动登录并举报到上限。",
                        parent=self.root):
                    ok, msg = autostart.enable()
                    if ok:
                        self.settings.set("auto_startup", True)
                    messagebox.showinfo("开机自启", msg, parent=self.root)
            self.status_var.set("自动任务已开启：明天开机后自动登录并举报到上限")
        else:
            self.status_var.set("自动任务已关闭（手动取消）")

    def toggle_auto_startup(self):
        """单独开/关开机自启。"""
        if autostart.is_enabled():
            ok, msg = autostart.disable()
            if ok:
                self.settings.set("auto_startup", False)
        else:
            ok, msg = autostart.enable()
            if ok:
                self.settings.set("auto_startup", True)
        try:
            self._startup_menu_var.set(autostart.is_enabled())
        except Exception:
            pass
        messagebox.showinfo("开机自启", msg, parent=self.root)

    def do_auto_help(self):
        last = self.settings.get("auto_result") or "尚未执行"
        adate = self.settings.get("auto_date") or "—"
        on = "已开启" if self.settings.get("auto_task") else "已关闭"
        boot = "已开启" if autostart.is_enabled() else "未开启"
        messagebox.showinfo(
            "自动任务说明",
            "当前状态\n"
            "  自动任务：%s\n"
            "  开机自启：%s\n"
            "  上次执行日期：%s\n"
            "  上次结果：%s\n\n"
            "工作方式：\n"
            "  1. 开启后，每天开机软件会自动登录所有已保存登录态的店铺；\n"
            "  2. 逐个自动举报 1/2/3 星差评，直到该店当日上限；\n"
            "  3. 按「每家店铺 / 每天一次」去重，不会重复消耗举报额度；\n"
            "     当天新增的店铺登录成功后会自动补跑一次，不用等到明天；\n"
            "  4. 想停掉：点标题区「自动任务」按钮，或菜单里取消勾选，"
            "随时生效、无需重启。" % (on, boot, adate, last),
            parent=self.root)

    def do_stop(self):
        self._call("stop")

    def do_stop_all(self):
        for sid in list(self.engine.snapshot().keys()):
            self.engine.call("stop", sid)

    def do_clear_limit(self):
        if self._call("clear_limit"):
            self.status_var.set("已清除该店铺当日上限标记")

    def do_open_dir(self):
        sid = self.selected()
        if not sid:
            return
        self._open(self.store.shop_dir(sid))

    def do_open_log(self):
        sid = self.selected()
        if not sid:
            return
        p = os.path.join(self.store.shop_dir(sid), "report_log.jsonl")
        if not os.path.exists(p):
            messagebox.showinfo("提示", "该店铺还没有举报日志", parent=self.root)
            return
        self._open(p)

    @staticmethod
    def _open(path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def do_help(self):
        messagebox.showinfo(
            "使用说明",
            "1. 点「添加店铺」会直接打开扫码登录窗口（默认切到二维码），"
            "用商家版 APP 扫码即可；店铺名会自动从商家后台获取，不用手输。\n"
            "   如果扫码窗口没弹出来：先看软件弹的错误框（会写明具体原因），"
            "或点菜单「帮助 → 浏览器自检」生成桌面诊断文件；"
            "新电脑常见原因是既没装 Chrome、Edge 又是老版，装个 Chrome 后重开即可。\n"
            "2. 登录成功后浏览器窗口自动关闭，店铺转为后台在线；"
            "如果没抓到店铺名，可右键「重命名」。\n"
            "3. 表格自动查 1/2/3 星评价数量，并定时续期登录态（每 2 分钟）。\n"
            "4. 点「开始举报」只走「同行恶意差评」单入口；"
            "触发风控立即暂停并弹「已到达当日举报上限」，明天自动恢复。\n"
            "   店铺列表左侧方框可以多选（点表头「选」= 全选），"
            "勾选后点「开始举报」会按顺序一家家跑完。\n"
            "5. 左侧是产品功能栏目：DSR 计算器 / 智能直通车 / 利润汇总"
            "目前为待开发占位；左下角可切换浅色 / 深色主题。\n"
            "6. 每家店铺的登录态、台账、上限标记互相独立，互不影响。\n"
            "7. 右下角显示当前版本号，点它可看版本详情；「检查更新」会立即查云端，"
            "每次启动也会自动检查，装好新版会弹窗提示，重启即生效。\n"
            "8. 1/2/3 星数量按「接口统计 → 星级标签 → 逐页统计评价卡片 → 筛选分页」"
            "四级读取；读不到时显示「—」（不是 0 条），并可右键「上报页面诊断」，"
            "程序会自动把真实页面结构回传给开发者，无需你手动传文件。\n"
            "9. 点窗口右上角 X 会隐藏到右下角托盘（任务栏不留按钮），程序继续"
            "后台续期登录、跑自动任务；左键点托盘图标打开主界面，右键可锁定、"
            "设置、检测版本、退出；想改回最小化到任务栏，在「设置」里切换。\n"
            "10. 自动任务按「每家店铺 / 每天一次」记录：当天新增的店铺，"
            "扫码登录成功后会自动补跑一次举报，不会因为「今天已经跑过」等到明天。\n\n"
            "数据目录：程序同级的 pdd_data 文件夹（可整包拷贝备份）。",
            parent=self.root)

    # ---------- 右下角托盘 ----------
    def _tray_log(self, msg):
        """托盘相关异常写进 pdd_data/tray.log，出问题能查。"""
        try:
            path = os.path.join(self.store.root, "tray.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    def _init_tray(self, retry=1):
        """启动右下角托盘图标；失败就静默退回任务栏方案，不影响主功能。"""
        if self._tray and self._tray.is_running():
            return
        try:
            from . import tray as tray_mod
        except Exception as e:
            self._tray_log("导入托盘模块失败：%s" % e)
            return
        try:
            if self._tray is None:
                self._tray = tray_mod.TrayIcon(
                    title="PDD 工作台 · 差评举报",
                    icon_path=os.path.join(app_base_dir(), "app.ico"),
                    log=self._tray_log)
            if not self._tray.start():
                self._tray = None
                if retry > 0:
                    # 开机自启时资源管理器可能还没就绪，8 秒后再试一次
                    self._tray_log("托盘首次启动失败，8 秒后重试")
                    try:
                        self.root.after(8000,
                                        lambda: self._init_tray(retry - 1))
                    except Exception:
                        pass
                return
            self._update_tray_tip()
            if not self._tray_polling:
                self._tray_polling = True
                self.root.after(150, self._poll_tray)
        except Exception as e:
            self._tray = None
            self._tray_log("托盘初始化异常：%s" % e)

    def _poll_tray(self):
        """在 Tk 线程里取托盘事件（绝不在托盘线程里操作 tkinter）。"""
        if self._closing:
            return
        try:
            if self._tray:
                for ev in self._tray.pop_events():
                    self._on_tray_event(ev)
        except Exception as e:
            self._tray_log("处理托盘事件失败：%s" % e)
        try:
            self.root.after(150, self._poll_tray)
        except Exception:
            self._tray_polling = False

    def _on_tray_event(self, ev):
        if ev == "open":
            self.show_main()
        elif ev == "lock":
            self.lock_app()
        elif ev == "unlock":
            self._unlock_dialog()
        elif ev == "settings":
            self.do_settings()
        elif ev == "update":
            if self.show_main():
                self.do_check_update()
        elif ev == "quit":
            self.do_quit()

    def _update_tray_tip(self):
        if not self._tray:
            return
        try:
            if self._locked:
                tip = "PDD 工作台 · 已锁定（右键可解锁）"
            else:
                states = self.engine.snapshot()
                online = sum(1 for st in states.values() if st.get("online"))
                pending = 0
                for st in states.values():
                    if st.get("stars_ok", False):
                        for k in ("star1", "star2", "star3"):
                            pending += self._as_int(st.get(k))
                tip = "PDD 工作台 · %d 家在线 · 待举报 %d 条" % (online, pending)
            self._tray.set_locked(self._locked)
            self._tray.set_tip(tip)
        except Exception:
            pass

    # ---------- 显示 / 隐藏 / 锁定 ----------
    def show_main(self):
        """从托盘唤起主界面；锁定时先要密码。返回 True=已显示。"""
        if self._locked and not self._unlock_dialog(open_window=False):
            return False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(400, self._drop_topmost)
        except Exception:
            pass
        self._update_tray_tip()
        return True

    def _drop_topmost(self):
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

    def hide_to_tray(self, notify=True):
        """隐藏到右下角托盘：任务栏不留按钮，后台继续续期登录、跑自动任务。"""
        if not (self._tray and self._tray.is_running()):
            # 托盘没起来就退回最小化到任务栏，绝不会把窗口弄丢
            self.minimize_to_taskbar(notify=notify)
            return
        first = notify and not self.settings.get("tray_tip_shown")
        try:
            self.root.withdraw()
        except Exception:
            pass
        if first:
            try:
                self.settings.set("tray_tip_shown", True)
                self._tray.notify(
                    "程序已隐藏到右下角",
                    "图标在右下角通知区域（时钟旁边）。\n"
                    "左键单击打开主界面，右键可锁定、设置、检测版本、退出。")
            except Exception:
                pass
        try:
            self.status_var.set("已隐藏到右下角托盘 · 左键点图标打开，右键看菜单")
        except Exception:
            pass
        self._update_tray_tip()

    @staticmethod
    def _sha(text):
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def lock_app(self):
        """锁定：窗口立即隐藏，重新打开必须输密码。"""
        if not (self._tray and self._tray.is_running()):
            messagebox.showinfo(
                "锁定", "托盘图标没有启动，暂时无法锁定。\n"
                "重启软件后即可使用（托盘不可用时会自动改为最小化到任务栏）。",
                parent=self.root)
            return
        if not self.settings.get("lock_pin_sha"):
            if not self._pin_dialog(
                    mode="set", title="先设置解锁密码",
                    subtitle="锁定后打开主界面要输这个密码（至少 4 位）"):
                return
        self._locked = True
        try:
            self.root.withdraw()
        except Exception:
            pass
        self._update_tray_tip()
        try:
            self.status_var.set("已锁定 · 点托盘图标输密码解锁")
            self._tray.notify("已锁定", "窗口已隐藏，需要密码才能重新打开。")
        except Exception:
            pass

    def _unlock_dialog(self, open_window=True):
        """弹出解锁密码框；成功返回 True。"""
        if not self._locked:
            return True
        if not self.settings.get("lock_pin_sha"):
            self._locked = False
            return True
        if not self._pin_dialog(mode="verify", title="已锁定",
                                subtitle="输入解锁密码后打开主界面"):
            return False
        self._locked = False
        self._update_tray_tip()
        try:
            self.status_var.set("已解锁")
        except Exception:
            pass
        if open_window:
            self.show_main()
        return True

    def _pin_dialog(self, mode="verify", title="已锁定", subtitle=""):
        """密码框：verify=校验解锁密码，set=设置/修改密码。返回 True=成功。"""
        t = self.t
        mode = "set" if mode == "set" else "verify"
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=t["card"])
        dlg.resizable(False, False)
        wd = 430
        ht = 330 if mode == "set" else 272
        try:
            dlg.geometry("%dx%d+%d+%d" % (
                wd, ht,
                max(0, (dlg.winfo_screenwidth() - wd) // 2),
                max(0, (dlg.winfo_screenheight() - ht) // 3)))
            dlg.attributes("-topmost", True)
        except Exception:
            pass

        tk.Label(dlg, text=title, bg=t["card"], fg=t["text"],
                 font=(self.fam, self.fs["lg"], "bold")).pack(
            anchor="w", padx=24, pady=(18, 2))
        if subtitle:
            tk.Label(dlg, text=subtitle, bg=t["card"], fg=t["muted"],
                     font=(self.fam, self.fs["sm"]), wraplength=wd - 48,
                     justify="left").pack(anchor="w", padx=24)

        def make_entry():
            e = tk.Entry(dlg, show="*", bg=t["card_alt"], fg=t["text"],
                         insertbackground=t["text"], relief="flat", bd=0,
                         font=(self.fam, self.fs["md"]), highlightthickness=1,
                         highlightbackground=t["border"],
                         highlightcolor=t["accent"])
            e.pack(fill=tk.X, padx=24, pady=(10, 4), ipady=7)
            return e

        e1 = make_entry()
        e2 = make_entry() if mode == "set" else None
        msg = tk.Label(dlg, text="", bg=t["card"], fg=t["danger"],
                       font=(self.fam, self.fs["sm"]), wraplength=wd - 48,
                       justify="left")
        msg.pack(anchor="w", padx=24, pady=(2, 0))

        result = {"ok": False}

        def close():
            try:
                dlg.grab_release()
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass

        def submit(_event=None):
            val = e1.get()
            if mode == "set":
                if len(val) < 4:
                    msg.config(text="密码至少 4 位")
                    return
                if e2 is not None and val != e2.get():
                    msg.config(text="两次输入不一致，请重新输入")
                    e2.delete(0, tk.END)
                    e2.focus_set()
                    return
                self.settings.set("lock_pin_sha", App._sha(val))
                result["ok"] = True
                close()
                return
            if App._sha(val) == str(self.settings.get("lock_pin_sha") or ""):
                result["ok"] = True
                close()
            else:
                msg.config(text="密码不正确；忘了可以右键托盘「退出程序」，"
                                "重新打开时用账号密码登录")
                e1.delete(0, tk.END)
                e1.focus_set()

        def button(text, cmd, primary=False):
            return tk.Button(
                dlg, text=text, command=cmd,
                bg=t["accent"] if primary else t["card_alt"],
                fg=t["accent_fg"] if primary else t["text_2"],
                activebackground=(t["accent_hover"] if primary
                                  else t["accent_soft"]),
                activeforeground=(t["accent_fg"] if primary else t["accent"]),
                relief="flat", bd=0, font=(self.fam, self.fs["md"]),
                padx=18, pady=6, cursor="hand2")

        row = tk.Frame(dlg, bg=t["card"])
        row.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=14)
        button("取消", close).pack(side=tk.RIGHT)
        tk.Frame(row, bg=t["card"], width=8).pack(side=tk.RIGHT)
        button("确定", submit, True).pack(side=tk.RIGHT)

        for widget in (e1, e2):
            if widget is not None:
                widget.bind("<Return>", submit)
        dlg.bind("<Escape>", lambda e: close())
        dlg.protocol("WM_DELETE_WINDOW", close)
        try:
            dlg.grab_set()
        except Exception:
            pass
        e1.focus_set()
        try:
            dlg.wait_window(dlg)
        except Exception:
            pass
        return bool(result["ok"])

    # ---------- 设置窗口 ----------
    def _mk_link(self, parent, text, command):
        t = self.t
        lb = tk.Label(parent, text=text, bg=parent["bg"], fg=t["accent"],
                      font=(self.fam, self.fs["sm"]), cursor="hand2")
        lb.bind("<Button-1>", lambda e: command())
        lb.bind("<Enter>", lambda e: lb.config(fg=t["accent_hover"]))
        lb.bind("<Leave>", lambda e: lb.config(fg=t["accent"]))
        return lb

    def do_settings(self):
        if self._locked and not self.show_main():
            return
        t = self.t
        win = getattr(self, "_settings_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                return
        except Exception:
            pass
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("设置")
        win.configure(bg=t["card"])
        win.resizable(False, False)
        wd, ht = 470, 500
        try:
            win.geometry("%dx%d+%d+%d" % (
                wd, ht,
                max(0, (win.winfo_screenwidth() - wd) // 2),
                max(0, (win.winfo_screenheight() - ht) // 3)))
        except Exception:
            pass

        tk.Label(win, text="设置", bg=t["card"], fg=t["text"],
                 font=(self.fam, self.fs["xl"], "bold")).pack(
            anchor="w", padx=24, pady=(18, 0))
        tk.Label(win, text="改完立即生效，保存在 pdd_data/settings.json",
                 bg=t["card"], fg=t["muted"],
                 font=(self.fam, self.fs["sm"])).pack(anchor="w", padx=24)

        box = tk.Frame(win, bg=t["card"])
        box.pack(fill=tk.BOTH, expand=True, padx=24, pady=(12, 0))

        def section(text):
            tk.Label(box, text=text, bg=t["card"], fg=t["text_2"],
                     font=(self.fam, self.fs["sm"], "bold")).pack(
                anchor="w", pady=(10, 2))

        # 1) 关闭窗口行为
        section("点窗口右上角 X 时")
        self._close_var = tk.StringVar(
            value=str(self.settings.get("close_action") or "tray"))
        for val, label in (("tray", "隐藏到右下角托盘（推荐，任务栏不留按钮）"),
                           ("taskbar", "最小化到任务栏（任务栏保留按钮）")):
            tk.Radiobutton(box, text=label, value=val,
                           variable=self._close_var,
                           command=self._on_close_action,
                           bg=t["card"], fg=t["text"], selectcolor=t["card"],
                           activebackground=t["card"],
                           activeforeground=t["accent"], bd=0,
                           highlightthickness=0, anchor="w",
                           font=(self.fam, self.fs["md"])).pack(fill=tk.X)

        # 2) 自动任务与开机自启
        section("自动运行")
        self._s_auto = tk.BooleanVar(value=bool(self.settings.get("auto_task")))
        self._s_boot = tk.BooleanVar(value=autostart.is_enabled())
        for var, label, cmd in (
                (self._s_auto, "启用自动任务（每天自动登录并举报到店铺上限）",
                 self._on_settings_auto),
                (self._s_boot, "开机自动启动本软件（启动后直接收到托盘）",
                 self._on_settings_boot)):
            tk.Checkbutton(box, text=label, variable=var, command=cmd,
                           bg=t["card"], fg=t["text"], selectcolor=t["card"],
                           activebackground=t["card"],
                           activeforeground=t["accent"], bd=0,
                           highlightthickness=0, anchor="w",
                           font=(self.fam, self.fs["md"])).pack(fill=tk.X)

        # 3) 锁定
        section("锁定（窗口隐藏，重新打开要输密码）")
        lockrow = tk.Frame(box, bg=t["card"])
        lockrow.pack(fill=tk.X)
        self._pin_state_var = tk.StringVar(
            value="已设置密码" if self.settings.get("lock_pin_sha") else "未设置密码")
        tk.Label(lockrow, textvariable=self._pin_state_var, bg=t["card"],
                 fg=t["muted"], font=(self.fam, self.fs["sm"])).pack(side=tk.LEFT)
        self._mk_link(lockrow, "清除密码", self._settings_clear_pin).pack(
            side=tk.RIGHT)
        tk.Label(lockrow, text="  |  ", bg=t["card"], fg=t["border"]).pack(
            side=tk.RIGHT)
        self._mk_link(lockrow, "设置/修改密码", self._settings_set_pin).pack(
            side=tk.RIGHT)

        # 4) 常用操作
        section("常用操作")
        tools = tk.Frame(box, bg=t["card"])
        tools.pack(fill=tk.X)

        def tool_btn(text, cmd):
            b = tk.Button(tools, text=text, command=cmd, bg=t["card_alt"],
                          fg=t["text_2"], activebackground=t["accent_soft"],
                          activeforeground=t["accent"], relief="flat", bd=0,
                          font=(self.fam, self.fs["sm"]), padx=12, pady=5,
                          cursor="hand2")
            b.pack(side=tk.LEFT, padx=(0, 8))
            return b

        tool_btn("清除当日上限标记", self.do_clear_limit)
        tool_btn("刷新 1/2/3 星", self.do_refresh)
        tool_btn("使用说明", self.do_help)

        # 5) 底部
        foot = tk.Frame(win, bg=t["card"])
        foot.pack(fill=tk.X, padx=24, pady=(12, 16))
        tk.Button(foot, text="关闭", command=win.destroy,
                  bg=t["accent"], fg=t["accent_fg"],
                  activebackground=t["accent_hover"],
                  activeforeground=t["accent_fg"], relief="flat", bd=0,
                  font=(self.fam, self.fs["md"]), padx=20, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT)
        self._mk_link(foot, "检测版本", self.do_check_update).pack(
            side=tk.LEFT, pady=8)

    def _on_close_action(self):
        val = self._close_var.get()
        self.settings.set("close_action", val)
        try:
            self.status_var.set("点 X 时：%s" % (
                "隐藏到右下角托盘" if val == "tray" else "最小化到任务栏"))
        except Exception:
            pass

    def _on_settings_auto(self):
        want = bool(self._s_auto.get())
        if want != bool(self.settings.get("auto_task")):
            self.toggle_auto_task()
        try:
            self._s_auto.set(bool(self.settings.get("auto_task")))
        except Exception:
            pass

    def _on_settings_boot(self):
        want = bool(self._s_boot.get())
        if want != autostart.is_enabled():
            self.toggle_auto_startup()
        try:
            self._s_boot.set(autostart.is_enabled())
        except Exception:
            pass

    def _settings_set_pin(self):
        if self._pin_dialog(mode="set", title="设置解锁密码",
                            subtitle="锁定后打开主界面要输这个密码（至少 4 位）"):
            try:
                self._pin_state_var.set("已设置密码")
                self.status_var.set("解锁密码已设置")
            except Exception:
                pass

    def _settings_clear_pin(self):
        if not self.settings.get("lock_pin_sha"):
            messagebox.showinfo("清除密码", "当前没有设置锁定密码。",
                                parent=self.root)
            return
        if not messagebox.askyesno("清除密码",
                                   "清除后「锁定」功能不可用，确定吗？",
                                   parent=self.root):
            return
        self.settings.set("lock_pin_sha", "")
        self._locked = False
        try:
            self._pin_state_var.set("未设置密码")
        except Exception:
            pass
        self._update_tray_tip()

    # ---------- 关闭 / 最小化 ----------
    def minimize_to_taskbar(self, notify=False):
        """最小化到任务栏（托盘不可用时兜底，也可在设置里主动选）。"""
        visible = True
        try:
            visible = self.root.state() != "iconic"
        except Exception:
            pass
        if notify and visible and not self.settings.get("close_tip_shown"):
            try:
                self.settings.set("close_tip_shown", True)
                messagebox.showinfo(
                    "已最小化到任务栏",
                    "程序没有退出，正在后台继续运行：\n"
                    "· 登录态每 2 分钟自动续期\n"
                    "· 自动任务开启时会继续自动举报\n\n"
                    "点任务栏图标即可重新打开窗口。\n"
                    "要彻底关闭，请点窗口底部状态栏的「退出程序」，"
                    "或菜单栏「退出程序」。",
                    parent=self.root)
            except Exception:
                pass
        try:
            self.root.iconify()          # Windows：收到任务栏，图标仍在
        except Exception:
            try:
                self.root.withdraw()     # 兜底：某些窗口管理器不支持 iconify
            except Exception:
                pass
        try:
            self.status_var.set("已最小化到任务栏，后台继续运行；"
                                "退出请点右下角「退出程序」")
        except Exception:
            pass
        self._update_tray_tip()

    def on_close(self):
        """点窗口右上角 X：默认隐藏到右下角托盘，不退出程序。"""
        if self._closing:
            try:
                self.root.destroy()
            except Exception:
                pass
            return
        action = str(self.settings.get("close_action") or "tray")
        if action == "taskbar":
            self.minimize_to_taskbar(notify=True)
        else:
            self.hide_to_tray(notify=True)

    def do_quit(self, confirm=None):
        """真正退出程序（菜单 / 状态栏 / 托盘右键）。"""
        if confirm is None:
            # 锁定时不弹确认框：窗口是隐藏的，弹框反而会把界面露出来
            confirm = not self._locked
        if confirm:
            try:
                self.root.deiconify()    # 弹确认框前先还原窗口
            except Exception:
                pass
            if not messagebox.askokcancel(
                    "退出程序",
                    "确定退出吗？\n"
                    "运行中的举报任务会停止，登录态已保存。\n"
                    "（只想隐藏到托盘，点窗口右上角 X 即可）",
                    parent=self.root):
                return
        self._closing = True
        try:
            self.engine.shutdown()
        except Exception:
            pass
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

def main(license_session=None, minimized=False):
    store = ShopStore()
    root = tk.Tk()
    App(root, store, license_session=license_session, minimized=minimized)
    root.mainloop()
