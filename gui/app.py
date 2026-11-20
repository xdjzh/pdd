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
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .engine import Engine
from .state import ShopStore, app_base_dir
from .theme import (PALETTES, Chip, PillButton, StatCard, draw_icon,
                    font_family, gradient, load_theme, mix, round_rect,
                    save_theme)


COLUMNS = (
    ("name", "店铺名称", 210),
    ("online", "在线状态", 170),
    ("star1", "1星", 72),
    ("star2", "2星", 72),
    ("star3", "3星", 72),
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
    def __init__(self, root, store, license_session=None):
        self.root = root
        self.store = store
        self.license_session = license_session
        self._license_blocked = False
        self._license_checking = False
        self._last_license_check = 0.0
        self.engine = Engine(store)
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
        # 启动那一刻真正跑起来的版本（下载完补丁后也不会误显示成新版）
        self._running_ver = _ver_state()
        self._checking_update = False
        self._build_ui()
        self.engine.start()
        if not self._tick_started:
            self._tick_started = True
            self.root.after(400, self._tick)
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
             fam=self.fam, size=self.fs["xs"]).pack(side=tk.RIGHT)

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

        helpm = self._menu(m)
        helpm.add_command(label="检查更新", command=self.do_check_update)
        helpm.add_command(label="版本信息", command=self.do_version_info)
        helpm.add_separator()
        helpm.add_command(label="使用说明", command=self.do_help)
        m.add_cascade(label="帮助", menu=helpm)
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
    def _tick(self):
        if self._closing:
            return
        try:
            self._render()
            if self.license_session and not self._license_checking and \
                    time.time() - self._last_license_check > 900:
                self._last_license_check = time.time()
                self._async_license_check()
            for ev in self.engine.pop_events():
                if ev[0] == "daily_limit":
                    _, sid, name, reported = ev
                    messagebox.showwarning(
                        "已到达当日举报上限",
                        "店铺：%s\n\n平台已触发风控，本店铺已自动暂停。\n"
                        "本次已举报 %d 条。\n\n"
                        "明天额度恢复后，直接打开本程序点「开始举报」即可继续，"
                        "登录态会一直保持在线。" % (name, reported),
                        parent=self.root)
        except Exception:
            pass
        self.root.after(800, self._tick)

    def _render(self):
        states = self.engine.snapshot()
        existing = set(self.tree.get_children())
        wanted = set(states.keys())
        for sid in existing - wanted:
            self.tree.delete(sid)
        for sid, st in states.items():
            dot = "●  " if st.get("online") else "○  "
            stars = [st.get("star1", 0), st.get("star2", 0), st.get("star3", 0)]
            # 没拿到可信读数就显示「—」；旧版会把同一个旧分页值填进三列（7/7/7）
            if not st.get("stars_ok", False):
                stars = ["—", "—", "—"]
            values = (st["name"], dot + str(st["status"]),
                      stars[0], stars[1], stars[2], st["reported"])
            if sid in existing:
                self.tree.item(sid, values=values)
            else:
                self.tree.insert("", tk.END, iid=sid, values=values)
        for i, sid in enumerate(self.tree.get_children()):
            st = states.get(sid)
            if not st:
                continue
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
        self.count_var.set("共 %d 家店铺" % len(states))
        self._show_detail()

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
        self.engine.add_shop(shop, then="login")
        self.status_var.set(
            "已打开扫码登录窗口（默认二维码）· 扫码成功后自动获取店铺名并隐藏浏览器")

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

    def do_login(self):
        if self._call("login"):
            self.status_var.set(
                "已打开扫码登录窗口（默认切到二维码）· 登录成功后自动获取店铺名并关闭窗口")

    def do_refresh(self):
        self._call("refresh")

    def do_star_diag(self):
        """把真实页面结构自动回传，用户不必再手动找文件、发文件。"""
        sid = self.selected()
        if not sid:
            return
        self._call("star_diag", sid)
        messagebox.showinfo(
            "页面诊断",
            "正在后台采集当前评价页信息并自动回传（约 6 秒）。\n\n"
            "回传成功后开发者就能按你后台的真实页面精确适配，\n"
            "不需要你手动找文件、发文件。\n\n"
            "本地也会留一份「星级诊断_*.json」，长数字已打码，\n"
            "不含密码或密钥。",
            parent=self.root)
        self.root.after(6000,
                        lambda p=self.store.shop_dir(sid): self._open(p))

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
        for sid in self.engine.snapshot():
            st = self.engine.state_for(sid)
            if not st.get("online") or st.get("daily_limit"):
                continue
            self.engine.call("start_report", sid)

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
            "2. 登录成功后浏览器窗口自动关闭，店铺转为后台在线；"
            "如果没抓到店铺名，可右键「重命名」。\n"
            "3. 表格自动查 1/2/3 星评价数量，并定时续期登录态（每 2 分钟）。\n"
            "4. 点「开始举报」只走「同行恶意差评」单入口；"
            "触发风控立即暂停并弹「已到达当日举报上限」，明天自动恢复。\n"
            "5. 左侧是产品功能栏目：DSR 计算器 / 智能直通车 / 利润汇总"
            "目前为待开发占位；左下角可切换浅色 / 深色主题。\n"
            "6. 每家店铺的登录态、台账、上限标记互相独立，互不影响。\n"
            "7. 右下角显示当前版本号，点它可看版本详情；「检查更新」会立即查云端，"
            "每次启动也会自动检查，装好新版会弹窗提示，重启即生效。\n"
            "8. 1/2/3 星数量按「接口统计 → 星级标签 → 逐页统计评价卡片 → 筛选分页」"
            "四级读取；读不到时显示「—」（不是 0 条），并可右键「上报页面诊断」，"
            "程序会自动把真实页面结构回传给开发者，无需你手动传文件。\n\n"
            "数据目录：程序同级的 pdd_data 文件夹（可整包拷贝备份）。",
            parent=self.root)

    def on_close(self):
        if not messagebox.askokcancel("退出", "确定退出吗？\n"
                                     "运行中的举报任务会停止，登录态已保存。",
                                     parent=self.root):
            return
        self._closing = True
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.root.destroy()


def main(license_session=None):
    store = ShopStore()
    root = tk.Tk()
    App(root, store, license_session=license_session)
    root.mainloop()
