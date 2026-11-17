# -*- coding: utf-8 -*-
"""
界面主题与自绘圆角控件。

配色思路（2026 主流 SaaS / 商家工作台风格）：
  浅色 Aurora：雾白底 + 纯白卡片 + 电光紫主色 + 薄荷 / 琥珀 / 珊瑚状态色，
               侧栏用深墨紫渐变，形成「深导航 + 轻内容」的层次。
  深色 Midnight：近黑蓝底 + 抬升卡片 + 高亮紫，长时间盯屏更柔和。

tkinter 没有 CSS 的圆角 / 渐变，这里用 Canvas 自绘，
保证 Windows 与 macOS 看到的是同一套视觉，不依赖系统主题。
"""
import json
import math
import os
import sys
import tkinter as tk
from tkinter import font as tkfont


LIGHT = {
    "name": "light",
    "bg": "#F5F6FB",
    "card": "#FFFFFF",
    "card_alt": "#FAFAFE",
    "border": "#E8EAF4",
    "text": "#1B1D2C",
    "text_2": "#5B6076",
    "muted": "#969BB0",
    "accent": "#6C5CE7",
    "accent_2": "#4F8BFF",
    "accent_soft": "#EFECFF",
    "accent_fg": "#FFFFFF",
    "accent_hover": "#7C6DF0",
    "success": "#0FBA8E",
    "success_soft": "#E4F8F2",
    "warning": "#E79A20",
    "warning_soft": "#FFF3DE",
    "danger": "#EE5570",
    "danger_soft": "#FFE9EE",
    "row_alt": "#F8F9FE",
    "select": "#EFECFF",
    "head_bg": "#F7F8FD",
    "scroll": "#D7DAE8",
    "sidebar_top": "#1A1830",
    "sidebar_bottom": "#27204F",
    "sidebar_text": "#F3F2FF",
    "sidebar_item": "#C4C1DE",
    "sidebar_sub": "#8B88B2",
    "sidebar_active": "#352D66",
    "sidebar_hover": "#2A254F",
    "sidebar_line": "#332E58",
}

DARK = {
    "name": "dark",
    "bg": "#0E1018",
    "card": "#171A25",
    "card_alt": "#1D2130",
    "border": "#272C3B",
    "text": "#EDEFF7",
    "text_2": "#A8AEC2",
    "muted": "#727A91",
    "accent": "#8B7BFF",
    "accent_2": "#5B93FF",
    "accent_soft": "#262244",
    "accent_fg": "#FFFFFF",
    "accent_hover": "#9C8DFF",
    "success": "#2ED3A7",
    "success_soft": "#153A33",
    "warning": "#F7B84B",
    "warning_soft": "#3A2E18",
    "danger": "#FF6B83",
    "danger_soft": "#3C202B",
    "row_alt": "#1B1F2C",
    "select": "#2B2750",
    "head_bg": "#1C202E",
    "scroll": "#333A4D",
    "sidebar_top": "#0C0D16",
    "sidebar_bottom": "#1A1533",
    "sidebar_text": "#F3F2FF",
    "sidebar_item": "#A7A3C6",
    "sidebar_sub": "#716E90",
    "sidebar_active": "#2C2455",
    "sidebar_hover": "#211C3E",
    "sidebar_line": "#242139",
}

PALETTES = {"light": LIGHT, "dark": DARK}


# ---------- 基础工具 ----------
def font_family():
    if sys.platform == "win32":
        return "Microsoft YaHei UI"
    if sys.platform == "darwin":
        return "PingFang SC"
    return "DejaVu Sans"


def mix(c1, c2, ratio):
    """两个 #RRGGBB 之间取插值色，用于渐变与 hover。"""
    ratio = max(0.0, min(1.0, ratio))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    out = [int(a[i] + (b[i] - a[i]) * ratio) for i in range(3)]
    return "#%02X%02X%02X" % (out[0], out[1], out[2])


def load_theme(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            name = json.load(f).get("theme")
        return name if name in PALETTES else "light"
    except Exception:
        return "light"


def save_theme(path, name):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"theme": name}, f)
    except Exception:
        pass


def _shape(c, x1, y1, x2, y2, r, color, tags):
    """圆角实心形状：两个矩形 + 四个 1/4 圆弧（PIESLICE）。"""
    r = max(0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    kw = {"fill": color, "outline": color, "width": 1, "tags": tags}
    c.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
    c.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)
    for cx, cy, start in ((x1, y1, 90), (x2 - 2 * r, y1, 0),
                          (x1, y2 - 2 * r, 180), (x2 - 2 * r, y2 - 2 * r, 270)):
        c.create_arc(cx, cy, cx + 2 * r, cy + 2 * r,
                     start=start, extent=90, style=tk.PIESLICE, **kw)


def round_rect(c, x1, y1, x2, y2, r, fill, outline=None, ow=1, tags=None):
    """带 1px 描边的圆角矩形。"""
    tags = tags or ()
    if outline:
        _shape(c, x1, y1, x2, y2, r, outline, tags)
        x1, y1, x2, y2 = x1 + ow, y1 + ow, x2 - ow, y2 - ow
        r = max(1, r - ow)
    _shape(c, x1, y1, x2, y2, r, fill, tags)


def gradient(c, x1, y1, x2, y2, c1, c2, vertical=True, tags=None):
    """逐行/逐列画渐变线；侧栏与顶部色条用，直角区域无圆角遮挡问题。"""
    tags = tags or ()
    total = int((y2 - y1) if vertical else (x2 - x1))
    if total < 1:
        return
    for i in range(total + 1):
        color = mix(c1, c2, float(i) / total)
        if vertical:
            c.create_line(x1, y1 + i, x2, y1 + i, fill=color, tags=tags)
        else:
            c.create_line(x1 + i, y1, x1 + i, y2, fill=color, tags=tags)


def draw_icon(c, x, y, kind, color, size=16):
    """在 (x, y) 中心画线性小图标，不用 emoji，跨平台字形一致。"""
    s = size / 2.0
    if kind == "star":
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            r = s if i % 2 == 0 else s * 0.44
            pts += [x + r * math.cos(a), y + r * math.sin(a)]
        c.create_polygon(pts, fill=color, outline=color)
    elif kind == "shop":
        c.create_line(x - s, y - s * 0.25, x, y - s, x + s, y - s * 0.25,
                      fill=color, width=1.6)
        c.create_rectangle(x - s * 0.72, y - s * 0.25, x + s * 0.72, y + s * 0.8,
                           outline=color, width=1.6)
        c.create_rectangle(x - s * 0.22, y + s * 0.15, x + s * 0.22, y + s * 0.8,
                           outline=color, width=1.4)
    elif kind == "send":
        c.create_polygon([x - s, y - s * 0.55, x + s, y - s,
                          x + s * 0.28, y + s, x - s * 0.1, y + s * 0.25,
                          x - s, y - s * 0.55], outline=color, fill="", width=1.6)
        c.create_line(x + s, y - s, x - s * 0.1, y + s * 0.25,
                      fill=color, width=1.4)
    elif kind == "plus":
        c.create_line(x - s * 0.65, y, x + s * 0.65, y, fill=color, width=1.8)
        c.create_line(x, y - s * 0.65, x, y + s * 0.65, fill=color, width=1.8)
    elif kind == "shield":
        c.create_polygon([x, y - s, x + s * 0.85, y - s * 0.6,
                          x + s * 0.7, y + s * 0.55, x, y + s,
                          x - s * 0.7, y + s * 0.55, x - s * 0.85, y - s * 0.6],
                         outline=color, fill="", width=1.5)
        c.create_line(x, y - s * 0.4, x, y + s * 0.15, fill=color, width=1.6)
        c.create_oval(x - 1.4, y + s * 0.4, x + 1.4, y + s * 0.4 + 2.8,
                      fill=color, outline=color)
    elif kind == "calc":
        c.create_rectangle(x - s * 0.8, y - s, x + s * 0.8, y + s,
                           outline=color, width=1.5)
        c.create_line(x - s * 0.8, y - s * 0.35, x + s * 0.8, y - s * 0.35,
                      fill=color, width=1.4)
        for ox in (-0.4, 0.4):
            for oy in (0.15, 0.65):
                c.create_oval(x + s * ox - 1, y + s * oy - 1,
                              x + s * ox + 1, y + s * oy + 1,
                              fill=color, outline=color)
    elif kind == "trend":
        c.create_line(x - s, y + s * 0.6, x - s * 0.3, y - s * 0.1,
                      x + s * 0.15, y + s * 0.35, x + s, y - s * 0.8,
                      fill=color, width=1.8)
        c.create_oval(x + s - 2, y - s * 0.8 - 2, x + s + 2, y - s * 0.8 + 2,
                      fill=color, outline=color)
    elif kind == "chart":
        for ox, h in ((-0.62, 0.5), (0, 1.0), (0.62, 0.75)):
            c.create_rectangle(x + s * ox - 2.4, y + s - s * h * 1.7,
                               x + s * ox + 2.4, y + s,
                               fill=color, outline=color)


# ---------- 自绘控件 ----------
class PillButton(tk.Canvas):
    """圆角按钮：primary / secondary / ghost / danger 四种层级，带 hover。"""

    KINDS = {
        "primary": ("accent", "accent_fg", "accent_hover", None),
        "secondary": ("card", "text_2", "accent_soft", "border"),
        "ghost": ("bg", "muted", "accent_soft", None),
        "danger": ("danger_soft", "danger", "danger_soft", None),
    }

    def __init__(self, master, t, text, command=None, kind="primary",
                 icon=None, height=36, fam=None, size=11, min_width=74,
                 padx=15, center=False):
        self.t = t
        self.text = text
        self.command = command
        self.kind = kind
        self.icon = icon
        self.center = center
        self.hover = False
        self.pressed = False
        self.fam = fam or font_family()
        self.size = size
        self.padx = padx
        measure = tkfont.Font(family=self.fam, size=size).measure(text)
        self.pw = max(min_width, int(measure) + padx * 2 + (20 if icon else 0))
        self.ph = height
        tk.Canvas.__init__(self, master, width=self.pw, height=self.ph,
                           bg=t["bg"], highlightthickness=0, bd=0,
                           cursor="hand2" if command else "arrow")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.draw()

    def _colors(self):
        bgc, fgc, hovc, bdc = self.KINDS[self.kind]
        t = self.t
        if self.hover:
            bg = t[hovc]
            fg = t["accent"] if self.kind in ("secondary", "ghost") else t[fgc]
            border = t["accent"] if self.kind == "secondary" else t.get(bdc or "border", t["border"])
        else:
            bg, fg = t[bgc], t[fgc]
            border = t[bdc] if bdc else None
        if self.pressed and self.kind == "primary":
            bg = mix(bg, t["text"], 0.12)
        return bg, fg, border

    def draw(self):
        c = self
        c.delete("all")
        bg, fg, border = self._colors()
        w, h = self.pw, self.ph
        round_rect(c, 1, 1, w - 1, h - 1, min(10, h / 2.0),
                   bg, outline=border, ow=1)
        left = self.padx + (20 if self.icon else 0)
        if self.icon:
            draw_icon(c, self.padx + 2, h / 2.0, self.icon, fg, size=13)
        if self.center and not self.icon:
            c.create_text(w / 2.0, h / 2.0 + 0.5, text=self.text,
                          anchor="center", fill=fg,
                          font=(self.fam, self.size,
                                "bold" if self.kind == "primary" else "normal"))
        else:
            c.create_text(left, h / 2.0 + 0.5, text=self.text, anchor="w",
                          fill=fg,
                          font=(self.fam, self.size,
                                "bold" if self.kind == "primary" else "normal"))

    def _on_enter(self, _e):
        self.hover = True
        self.draw()

    def _on_leave(self, _e):
        self.hover = False
        self.pressed = False
        self.draw()

    def _on_press(self, _e):
        self.pressed = True
        self.draw()

    def _on_release(self, e):
        was = self.pressed
        self.pressed = False
        self.draw()
        if was and self.command and 0 <= e.x <= self.pw and 0 <= e.y <= self.ph:
            self.command()


class Chip(tk.Canvas):
    """只读的圆角状态胶囊。"""

    def __init__(self, master, t, text, color=None, soft=None,
                 fam=None, size=10, height=28, padx=13):
        self.t, self.text = t, text
        self.fam = fam or font_family()
        self.size = size
        self.padx = padx
        self.ph = height
        self.pw = int(tkfont.Font(family=self.fam, size=size).measure(text)) + padx * 2 + 12
        self.color = color or t["accent"]
        self.soft = soft or t["accent_soft"]
        tk.Canvas.__init__(self, master, width=self.pw, height=self.ph,
                           bg=t["bg"], highlightthickness=0, bd=0)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.pw, self.ph
        round_rect(self, 1, 1, w - 1, h - 1, h / 2.0, self.soft)
        self.create_oval(self.padx - 1, h / 2.0 - 3.5,
                         self.padx + 6, h / 2.0 + 3.5,
                         fill=self.color, outline=self.color)
        self.create_text(self.padx + 14, h / 2.0 + 0.5, text=self.text,
                         anchor="w", fill=self.color,
                         font=(self.fam, self.size))


class StatCard(tk.Canvas):
    """顶部数据卡：圆角白卡 + 渐变顶条 + 图标 + 大数字。"""

    def __init__(self, master, t, label, sub, color, soft, icon,
                 fam=None, height=96):
        self.t, self.label, self.sub = t, label, sub
        self.color, self.soft, self.icon = color, soft, icon
        self.fam = fam or font_family()
        self.ph = height
        self.value = "0"
        self.extra = ""
        tk.Canvas.__init__(self, master, height=height, width=220,
                           bg=t["bg"], highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda e: self.draw())
        self.draw()

    def set_value(self, value, extra=""):
        value, extra = str(value), str(extra)
        if value != self.value or extra != self.extra:
            self.value, self.extra = value, extra
            self.draw()

    def draw(self):
        c = self
        c.delete("all")
        # 控件尚未映射时 winfo_* 会返回 1，此时用请求尺寸绘制首帧，
        # 避免第一次出现的布局比最终布局窄（备注被误判为放不下）。
        ww, hh = c.winfo_width(), c.winfo_height()
        w = max(180, ww if ww > 1 else 220)
        h = max(88, hh if hh > 1 else self.ph)
        t = self.t
        round_rect(c, 1, 1, w - 2, h - 2, 14, t["card"],
                   outline=t["border"], ow=1)
        # 顶部 3px 渐变高光条（避开圆角）
        gradient(c, 18, 2, w - 18, 4, self.color, t["accent"],
                 vertical=False)
        # 图标放右上角，与左侧文字彻底分开；标签固定上部、数字固定左下
        bx2, bx1 = w - 16, w - 46
        round_rect(c, bx1, 14, bx2, 44, 9, self.soft)
        draw_icon(c, (bx1 + bx2) / 2.0, 29, self.icon, self.color, size=16)
        c.create_text(16, 22, text=self.label, anchor="w",
                      fill=t["text_2"], font=(self.fam, 10))
        c.create_text(16, h - 12, text=self.value, anchor="sw",
                      fill=t["text"], font=(self.fam, 21, "bold"))
        # 备注与数字同一底部基线，卡片太窄时不画，避免再次重叠
        if w >= 200 and self.extra:
            c.create_text(w - 16, h - 14, text=self.extra, anchor="se",
                          fill=t["muted"], font=(self.fam, 9))
