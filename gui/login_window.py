# -*- coding: utf-8 -*-
"""
登录 / 注册 / 激活窗口（沿用主界面的 Aurora / Midnight 配色）。

未授权时由 run_gui.py 在加载主界面之前弹出；窗口关闭或点「退出」即结束程序。
网络请求同步执行，单次最多 8 秒，期间先用 update() 把「正在连接」刷出来。

界面约定（2026-11-17 重排）：
  - 表单放在独立容器里，永远位于「主按钮」之上；按钮下方才是状态提示。
  - 窗口高度按当前模式的字段数量自适应，任何模式都不会裁掉输入框。
  - 「服务器设置」是底部折叠区，默认收起，不夹在表单中间。
  - 注册 / 登录后进入待激活状态时，只显示一个激活码输入框。
  - 激活码统一为 PDD-XXXX-XXXX-XXXX，云端哈希包含 PDD 前缀，不能截断。
"""
import os
import sys
import tkinter as tk
from tkinter import messagebox

from .theme import (PALETTES, PillButton, draw_icon, font_family,
                    gradient, load_theme, round_rect)

MODES = (("login", "登录"), ("register", "注册"), ("activate", "激活码"))

SECTIONS = {
    "login": ("欢迎回来", "用注册时的手机号和密码登录，不发短信验证码。"),
    "register": ("创建账号", "设置手机号与密码；有激活码可以现在填，也可以稍后再激活。"),
    "activate": ("输入激活码", "已注册、到期续费或换了新码，在这里完成激活。"),
}

BUTTONS = {"login": "登 录", "register": "注册并激活", "activate": "激 活"}

HINTS = {
    "login": "登录后自动校验授权状态，无需短信验证码。",
    "register": "没有激活码也能先注册，注册后会自动跳到激活步骤。",
    "activate": "填注册时的手机号和密码，再输入新的激活码。",
}

CODE_HELPER = "前缀 PDD- 已固定，只需填后面 12 位 · 支持整段粘贴"
NAV_KEYS = {"Left", "Right", "Up", "Down", "Home", "End", "Prior",
            "Next", "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Caps_Lock", "Tab", "Escape"}


def mono_family():
    if sys.platform == "win32":
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "DejaVu Sans Mono"


def code_body(value):
    """去掉分隔符 / 大小写 / 可选 PDD 前缀，取出 12 位码体。"""
    s = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return (s[3:] if s.startswith("PDD") else s)[:12]


def body_display(value):
    """码体的可见分组形式：R83M-D76V-7G39（前缀由界面固定显示）。"""
    body = code_body(value)
    return "-".join(body[i:i + 4] for i in range(0, len(body), 4))


def canonical_code(value):
    """返回 (完整展示文本, 提交给云端的纯码, 是否完整)。

    云端 normalizeCode() 保留 PDD 前缀后算 SHA256，所以提交值必须是
    'PDD' + 12 位码体，共 15 位；旧版在这里截到 12 位导致永远激活失败。
    """
    body = code_body(value)
    shown = "PDD-" + body_display(value) if body else "PDD-"
    return shown, "PDD" + body, len(body) == 12


class LicenseWindow(object):
    def __init__(self, client, reason=""):
        self.client = client
        self.result = None
        self.mode = "login"
        self.pending_ticket = ""
        self.pending_key = ""
        self.pending_phone = ""
        self.pending_password = ""
        self._last_values = {}
        self._pwd_visible = False
        self.fam = font_family()
        self.mono = mono_family()
        theme = load_theme(os.path.join(client.data_dir, "ui_theme.json"))
        self.t = PALETTES[theme]

        root = tk.Tk()
        self.root = root
        root.title("%s · 账号授权" % client.app_name)
        root.geometry("440x520")
        root.resizable(False, False)
        root.configure(bg=self.t["bg"])
        root.protocol("WM_DELETE_WINDOW", self._quit)
        self._build()
        self._fit(center=True)
        if reason:
            self._set_msg(reason, "danger")

    # ---------- 界面 ----------
    def _build(self):
        t = self.t
        head = tk.Canvas(self.root, height=104, bg=t["sidebar_top"],
                         highlightthickness=0, bd=0)
        head.pack(fill=tk.X)
        self.head = head
        head.bind("<Configure>", lambda _e: self._draw_head())
        self.root.after(60, self._draw_head)

        body = tk.Frame(self.root, bg=t["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=26)
        self.body = body

        tabs = tk.Canvas(body, height=42, bg=t["bg"],
                         highlightthickness=0, bd=0)
        tabs.pack(fill=tk.X, pady=(12, 10))
        tabs.bind("<Button-1>", self._on_tab)
        tabs.bind("<Configure>", lambda _e: self._draw_tabs())
        self.tabs = tabs
        self._tab_hit = []
        self._draw_tabs()

        self.sec_title = tk.Label(body, bg=t["bg"], fg=t["text"],
                                  font=(self.fam, 14, "bold"), anchor="w")
        self.sec_title.pack(fill=tk.X)
        self.sec_desc = tk.Label(body, bg=t["bg"], fg=t["muted"],
                                 font=(self.fam, 10), wraplength=384,
                                 justify="left", anchor="w")
        self.sec_desc.pack(fill=tk.X, pady=(4, 0))

        # 「账号已验证」条：刚注册 / 刚登录被要求激活时才出现
        self.verified_var = tk.StringVar(value="")
        vf = tk.Frame(body, bg=t["accent_soft"])
        vf.pack_forget()
        self.verified_frame = vf
        tk.Label(vf, textvariable=self.verified_var, bg=t["accent_soft"],
                 fg=t["accent"], font=(self.fam, 10, "bold"),
                 anchor="w").pack(side=tk.LEFT, padx=12, pady=10)
        switch = tk.Label(vf, text="换个账号", bg=t["accent_soft"],
                          fg=t["text_2"], font=(self.fam, 9, "underline"),
                          cursor="hand2")
        switch.pack(side=tk.RIGHT, padx=12)
        switch.bind("<Button-1>", lambda _e: self._switch_account())

        # 关键：字段独立容器，永远先于主按钮 pack，不会再跑到按钮下面
        fields_box = tk.Frame(body, bg=t["bg"])
        fields_box.pack(fill=tk.X, pady=(10, 0))
        self.fields_box = fields_box
        self.fields = {}
        self.rows = {}

        self._make_row("phone", "手机号")
        self._make_row("password", "密码", secret=True,
                       action=("显示", self._toggle_pwd))
        self._make_row("confirm", "确认密码", secret=True)
        self._make_row("code", "激活码", mono=True, helper=CODE_HELPER,
                       action=("粘贴", self._paste_code))

        self.ok_btn = PillButton(body, t, "登 录", command=self._submit,
                                 kind="primary", height=40, fam=self.fam,
                                 size=11, min_width=388, center=True)
        self.ok_btn.pack(fill=tk.X, pady=(2, 0))

        self.msg = tk.Label(body, text="", bg=t["bg"], fg=t["muted"],
                            font=(self.fam, 10), wraplength=384,
                            justify="left", anchor="w")
        self.msg.pack(fill=tk.X, pady=(9, 0))

        # ---------- 底部：折叠的服务器设置 + 退出 ----------
        foot = tk.Frame(self.root, bg=t["bg"])
        foot.pack(fill=tk.X, padx=26, pady=(6, 12))
        foot.columnconfigure(0, weight=1)
        tk.Frame(foot, height=1, bg=t["border"]).grid(
            row=0, column=0, sticky="ew", pady=(0, 8))

        self.show_adv = tk.BooleanVar(value=not bool(self.client.endpoint))
        self.endpoint_var = tk.StringVar(value=self.client.endpoint)
        self.adv_box = tk.Frame(foot, bg=t["bg"])
        tk.Label(self.adv_box, text="授权服务器地址", bg=t["bg"],
                 fg=t["text_2"], font=(self.fam, 9),
                 anchor="w").pack(fill=tk.X)
        tk.Entry(self.adv_box, textvariable=self.endpoint_var,
                 relief="flat", bg=t["card"], fg=t["text"],
                 font=(self.fam, 9), insertbackground=t["text"],
                 highlightthickness=1, highlightcolor=t["border"],
                 highlightbackground=t["border"]).pack(
                     fill=tk.X, ipady=6, pady=(4, 0))
        tk.Label(self.adv_box,
                 text="由软件提供方给出，一般不用修改",
                 bg=t["bg"], fg=t["muted"], font=(self.fam, 9),
                 anchor="w").pack(fill=tk.X, pady=(4, 0))

        bar = tk.Frame(foot, bg=t["bg"])
        bar.grid(row=2, column=0, sticky="ew")
        self.adv_link = tk.Label(bar, text="▸ 服务器设置", bg=t["bg"],
                                 fg=t["muted"], font=(self.fam, 9),
                                 cursor="hand2")
        self.adv_link.pack(side=tk.LEFT)
        self.adv_link.bind("<Button-1>", lambda _e: self._toggle_adv(True))
        PillButton(bar, t, "退出", command=self._quit, kind="ghost",
                   height=30, fam=self.fam, size=10,
                   min_width=76).pack(side=tk.RIGHT)
        tk.Label(foot, text="账号与激活码由软件提供方管理",
                 bg=t["bg"], fg=t["muted"],
                 font=(self.fam, 9), anchor="w").grid(
                     row=3, column=0, sticky="ew", pady=(6, 0))

        self.root.bind("<Return>", self._on_enter)
        self._toggle_adv()
        self._apply_mode()

    def _make_row(self, key, label, secret=False, mono=False,
                  helper=None, action=None):
        t = self.t
        row = tk.Frame(self.fields_box, bg=t["bg"])
        top = tk.Frame(row, bg=t["bg"])
        top.pack(fill=tk.X)
        tk.Label(top, text=label, bg=t["bg"], fg=t["text_2"],
                 font=(self.fam, 10), anchor="w").pack(side=tk.LEFT)
        if action:
            text, cmd = action
            link = tk.Label(top, text=text, bg=t["bg"], fg=t["accent"],
                            font=(self.fam, 9), cursor="hand2")
            link.pack(side=tk.RIGHT)
            link.bind("<Button-1>", lambda _e, f=cmd: f())
            row._action_link = link

        box = tk.Frame(row, bg=t["card"], highlightthickness=1,
                       highlightbackground=t["border"],
                       highlightcolor=t["accent"])
        box.pack(fill=tk.X, pady=(5, 0))
        ent = tk.Entry(box, show="●" if secret else "", relief="flat", bd=0,
                       bg=t["card"], fg=t["text"],
                       font=(self.mono if mono else self.fam,
                             12 if mono else 11),
                       insertbackground=t["text"], highlightthickness=0)
        if key == "code":
            # PDD- 固定显示、不可编辑：手打整段码时不会把前缀重复进去
            tk.Label(box, text="PDD-", bg=t["card"], fg=t["muted"],
                     font=(self.mono, 12, "bold")).pack(
                         side=tk.LEFT, padx=(11, 0))
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True,
                     padx=(2, 11), pady=7)
        else:
            ent.pack(fill=tk.X, padx=11, pady=7)
        ent.bind("<FocusIn>", lambda _e, b=box: b.configure(
            highlightbackground=t["accent"]))
        ent.bind("<FocusOut>", lambda _e, b=box: b.configure(
            highlightbackground=t["border"]))
        if key == "code":
            ent.bind("<KeyRelease>", self._format_code)
        self.fields[key] = ent
        self.rows[key] = row
        if helper:
            tk.Label(row, text=helper, bg=t["bg"], fg=t["muted"],
                     font=(self.fam, 9), wraplength=384,
                     justify="left", anchor="w").pack(
                         fill=tk.X, pady=(4, 0))
        return row

    def _pwd_link_widget(self):
        return getattr(self.rows["password"], "_action_link", None)

    def _draw_head(self):
        c = self.head
        w = max(440, c.winfo_width())
        t = self.t
        c.delete("all")
        gradient(c, 0, 0, w, 104, t["sidebar_top"], t["sidebar_bottom"],
                 vertical=False)
        round_rect(c, 24, 26, 70, 72, 12, t["accent"])
        draw_icon(c, 47, 49, "shield", "#FFFFFF", size=20)
        c.create_text(84, 44, text=self.client.app_name, anchor="w",
                      fill=t["sidebar_text"],
                      font=(self.fam, 15, "bold"))
        c.create_text(84, 68, text="账号登录 · 激活码授权", anchor="w",
                      fill=t["sidebar_sub"], font=(self.fam, 10))
        round_rect(c, w - 96, 36, w - 24, 62, 13, t["sidebar_active"])
        c.create_text(w - 60, 49, text="安全授权", anchor="center",
                      fill=t["sidebar_item"], font=(self.fam, 9))

    def _draw_tabs(self):
        c = self.tabs
        c.delete("all")
        t = self.t
        w = max(388, c.winfo_width())
        self._tab_hit = []
        seg = w / 3.0
        for i, (key, label) in enumerate(MODES):
            x1, x2 = i * seg + 2, (i + 1) * seg - 2
            active = key == self.mode
            fill = t["accent"] if active else t["card"]
            fg = t["accent_fg"] if active else t["text_2"]
            round_rect(c, x1, 2, x2, 40, 10, fill,
                       outline=None if active else t["border"], ow=1)
            c.create_text((x1 + x2) / 2.0, 21, text=label, fill=fg,
                          font=(self.fam, 11, "bold" if active else "normal"))
            self._tab_hit.append((x1, x2, key))

    def _fit(self, center=False):
        """按当前内容自适应高度，避免 4 个字段时被窗口裁掉。"""
        try:
            self.root.update_idletasks()
            h = max(430, min(780, self.root.winfo_reqheight() + 12))
            self.root.geometry("440x%d" % h)
            if center:
                self.root.eval("tk::PlaceWindow . center")
        except Exception:
            pass

    # ---------- 模式切换 ----------
    def _on_tab(self, event):
        for x1, x2, key in self._tab_hit:
            if x1 <= event.x <= x2:
                if key != self.mode:
                    self.mode = key
                    self._set_msg("", "muted")
                    self._draw_tabs()
                    # 手动切换 = 放弃上一次的待激活票据，避免误绑到旧账号
                    self._apply_mode(manual=True)
                return

    def _apply_mode(self, manual=False):
        if manual:
            self.pending_ticket = ""
            self.pending_key = ""
            self.pending_phone = ""
            self.pending_password = ""
        quick = self.mode == "activate" and bool(self.pending_ticket)

        for row in self.rows.values():
            row.pack_forget()
        show = {
            "login": ("phone", "password"),
            "register": ("phone", "password", "confirm", "code"),
            "activate": ("phone", "password", "code"),
        }[self.mode]
        if quick:
            show = ("code",)
            self.verified_var.set("账号 %s 已验证，只需填激活码" %
                                  self._mask_phone(self.pending_phone))
            self.verified_frame.pack(fill=tk.X, pady=(10, 0),
                                     after=self.sec_desc)
        else:
            self.verified_frame.pack_forget()
        for key in show:
            self.rows[key].pack(fill=tk.X, pady=(0, 9))

        title, desc = SECTIONS[self.mode]
        self.sec_title.configure(text=title)
        self.sec_desc.configure(text=desc)
        self.ok_btn.text = BUTTONS[self.mode]
        self.ok_btn.draw()
        self._set_msg(HINTS[self.mode], "muted")
        first = show[0]
        self.root.after(30, lambda k=first: self.fields[k].focus_set())
        self._fit()

    @staticmethod
    def _mask_phone(p):
        p = str(p or "")
        return "%s****%s" % (p[:3], p[-4:]) if len(p) >= 7 else (p or "本账号")

    def _switch_account(self):
        self.pending_ticket = ""
        self.pending_key = ""
        self.pending_phone = ""
        self.pending_password = ""
        self._apply_mode()
        self._set_msg("请填写手机号、密码和新的激活码", "muted")

    def _toggle_adv(self, from_click=False):
        if from_click:
            self.show_adv.set(not self.show_adv.get())
        if self.show_adv.get():
            self.adv_box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            self.adv_link.configure(text="▾ 收起服务器设置")
        else:
            self.adv_box.grid_remove()
            self.adv_link.configure(text="▸ 服务器设置")
        self._fit()

    def _toggle_pwd(self):
        self._pwd_visible = not self._pwd_visible
        for key in ("password", "confirm"):
            self.fields[key].configure(show="" if self._pwd_visible else "●")
        link = self._pwd_link_widget()
        if link is not None:
            link.configure(text="隐藏" if self._pwd_visible else "显示")

    def _on_enter(self, _event):
        self._submit()
        return "break"

    # ---------- 激活码 ----------
    def _format_code(self, event):
        """实时整理成 PDD-XXXX-XXXX-XXXX，并保持光标位置。"""
        if event is not None and getattr(event, "keysym", "") in NAV_KEYS:
            return
        ent = self.fields["code"]
        old = ent.get()
        try:
            caret = int(ent.index(tk.INSERT))
            head = "".join(ch for ch in old[:caret].upper() if ch.isalnum())
            if head.startswith("PDD"):
                head = head[3:]
            target = len(head[:12])
        except Exception:
            target = len(code_body(old))
        shown = body_display(old)
        if shown == old:
            return
        ent.delete(0, tk.END)
        ent.insert(0, shown)
        pos = len(shown)
        seen = 0
        for i, ch in enumerate(shown):
            if ch.isalnum():
                seen += 1
                if seen == target:
                    pos = i + 1
                    break
        try:
            ent.icursor(min(pos, len(shown)))
        except Exception:
            pass

    def _paste_code(self):
        try:
            text = self.root.clipboard_get()
        except Exception:
            self._set_msg("剪贴板里没有可读取的文本", "danger")
            return
        ent = self.fields["code"]
        ent.delete(0, tk.END)
        ent.insert(0, text)
        self._format_code(None)
        self._set_msg("已粘贴激活码，核对后点「激 活」", "muted")

    def _set_field(self, key, value):
        ent = self.fields[key]
        ent.delete(0, tk.END)
        ent.insert(0, str(value or ""))

    def _set_msg(self, text, kind="muted"):
        colors = {"muted": self.t["muted"], "danger": self.t["danger"],
                  "success": self.t["success"]}
        self.msg.configure(text=text, fg=colors.get(kind, self.t["muted"]))

    # ---------- 逻辑 ----------
    def _values(self):
        return {
            "phone": self.fields["phone"].get().strip(),
            "password": self.fields["password"].get(),
            "confirm": self.fields["confirm"].get(),
            "code": self.fields["code"].get().strip(),
        }

    def _prepare_server(self):
        url = self.endpoint_var.get().strip()
        if url != self.client.endpoint:
            ok, err = self.client.save_endpoint(url)
            if not ok:
                self._set_msg(err, "danger")
                return False
        if not self.client.endpoint:
            self._set_msg("请先填授权服务器地址", "danger")
            self.show_adv.set(True)
            self._toggle_adv()
            return False
        return True

    def _busy(self, text):
        self._set_msg(text, "muted")
        self.root.update()

    def _finish(self, res):
        self.result = self.client.save_result(res)
        self.root.quit()

    def _need_activate(self, res, hint):
        self.pending_ticket = res.get("ticket") or ""
        self.pending_key = res.get("publicKey") or ""
        v = self._last_values or self._values()
        self.pending_phone = v.get("phone", "")
        self.pending_password = v.get("password", "")
        self._set_field("phone", self.pending_phone)
        self._set_field("password", self.pending_password)
        self._set_field("code", "")
        self.mode = "activate"
        self._draw_tabs()
        self._apply_mode()
        self._set_msg(hint or "账号已验证，输入激活码即可完成授权", "muted")

    def _submit(self):
        if not self._prepare_server():
            return
        v = self._values()
        if self.mode == "activate" and self.pending_ticket:
            v["phone"] = self.pending_phone or v["phone"]
            v["password"] = self.pending_password or v["password"]
            v["confirm"] = v["password"]
        if not v["phone"] or not v["password"]:
            self._set_msg("请填写手机号和密码", "danger")
            return
        if self.mode == "register" and v["password"] != v["confirm"]:
            self._set_msg("两次输入的密码不一致", "danger")
            return
        if self.mode in ("register", "activate"):
            if not code_body(v["code"]):
                v["code"] = ""
            else:
                _shown, canon, valid = canonical_code(v["code"])
                if not valid:
                    self._set_msg(
                        "激活码不完整，应为 PDD-XXXX-XXXX-XXXX（12 位）",
                        "danger")
                    return
                v["code"] = canon
        self._last_values = dict(v)
        self._busy("正在连接授权服务器…")

        if self.mode == "login":
            res = self.client.call("login", {
                "phone": v["phone"], "password": v["password"]})
        elif self.mode == "register":
            res = self.client.call("register", {
                "phone": v["phone"], "password": v["password"],
                "code": v["code"]})
        else:
            res = self._activate(v)

        if res.get("ok"):
            self._busy("授权通过，正在进入工作台…")
            self._finish(res)
            return
        if res.get("needActivate"):
            self._need_activate(res, res.get("msg") or "请输入激活码完成激活")
            return
        self._set_msg(res.get("msg") or "操作失败，请重试", "danger")

    def _activate(self, v):
        ticket = self.pending_ticket
        if not ticket:
            base = self.client.call("login", {
                "phone": v["phone"], "password": v["password"]})
            if base.get("ok"):
                return base
            ticket = base.get("ticket") or ""
            if not ticket:
                return {"ok": False,
                        "msg": base.get("msg") or "请先注册或登录"}
            self.pending_ticket = ticket
            self.pending_key = base.get("publicKey") or ""
        if not v["code"]:
            return {"ok": False, "msg": "请输入激活码"}
        return self.client.call("activate", {
            "ticket": ticket, "code": v["code"]})

    def _quit(self):
        self.result = None
        self.root.quit()


def show_login_window(client, reason=""):
    """阻塞显示登录窗，返回 LicenseSession 或 None（用户放弃 / 关闭窗口）。"""
    try:
        win = LicenseWindow(client, reason)
        win.root.mainloop()
        result = win.result
        try:
            win.root.destroy()
        except Exception:
            pass
        return result
    except Exception as e:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("授权窗口异常", str(e)[:200])
            root.destroy()
        except Exception:
            pass
        return None
