# -*- coding: utf-8 -*-
"""
后台调度引擎：一条独立线程跑 asyncio 事件循环，驱动所有店铺会话。

GUI（tkinter 主线程）只做两件事：
  - 每 0.8 秒读一次 snapshot()，刷新表格
  - 用 call() 投递命令、用 pop_events() 取弹窗事件
所有 Playwright 操作都在后台线程里，界面不会卡。
"""
import asyncio
import glob
import os
import queue
import sys
import threading
import time
import traceback

from .adapter import ShopSession
from .state import blank_state

HEARTBEAT_SECONDS = 120       # 在线心跳 / cookie 续期
STAR_REFRESH_SECONDS = 600    # 1/2/3 星数量刷新（筛选较重，别太频繁）
TICK_SECONDS = 3
LAUNCH_TIMEOUT_MS = 25000     # 单次浏览器启动上限：不设的话杀软扫描会让 launch 永久挂住


# ---------------------------------------------------------------------------
# 浏览器定位：新装机器（尤其企业版 LTSC）常常既没 Chrome、Edge 又是老版，
# 只能靠 exe 里内置的 Chromium。这里把所有可能的位置都找出来，逐个试。
# ---------------------------------------------------------------------------
def _short(e, limit=110):
    s = str(e).replace("\n", " ").strip()
    return s[:limit]


def _win_app_paths(exe_name):
    """从注册表 App Paths 读浏览器真实安装路径（比猜目录可靠）。"""
    out = []
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except Exception:
        return out
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % exe_name
                with winreg.OpenKey(root, key, 0,
                                    winreg.KEY_READ | view) as k:
                    path = winreg.QueryValue(k, None)
                if path and os.path.isfile(path):
                    out.append(path)
            except Exception:
                continue
    return out


def installed_browsers():
    """返回 [(说明, exe 路径)]，已存在的系统 Chrome / Edge。"""
    if sys.platform != "win32":
        return []
    env = os.environ
    cands = []
    for name in ("chrome.exe", "msedge.exe"):
        cands.extend(_win_app_paths(name))
    roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"),
             env.get("LOCALAPPDATA"), env.get("PROGRAMW6432")]
    rel = {
        "chrome.exe": (r"Google\Chrome\Application\chrome.exe",),
        "msedge.exe": (r"Microsoft\Edge\Application\msedge.exe",),
    }
    for name, subs in rel.items():
        for r in roots:
            if not r:
                continue
            for sub in subs:
                cands.append(os.path.join(r, sub))
    seen, out = set(), []
    for p in cands:
        try:
            if not p or not os.path.isfile(p):
                continue
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            tag = "系统 Chrome" if "chrome" in os.path.basename(p).lower() else "系统 Edge"
            out.append((tag, p))
        except Exception:
            continue
    return out


def bundled_chromium():
    """Playwright 内置 Chromium 的 chrome.exe 路径（打包后位于 _MEIPASS/ms-playwright）。"""
    roots = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        roots.append(env_path)
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(os.path.join(meipass, "ms-playwright"))
    try:
        base = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(os.path.join(base, "ms-playwright"))
        roots.append(os.path.join(base, "runtime", "ms-playwright"))
    except Exception:
        pass
    if sys.platform != "win32":
        return ""
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for pat in ("chromium-*/chrome-win/chrome.exe",
                    "chromium-*/chrome-win64/chrome.exe",
                    "chromium_headless_shell-*/chrome-win/chrome.exe"):
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[-1]
    return ""


class Engine(object):
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings          # SettingsStore，自动任务开关/日期
        self.sessions = {}
        self.states = {}
        self._state_lock = threading.RLock()
        self.events = queue.Queue()
        self.loop = None
        self._stop_event = None
        self._ready = threading.Event()
        self._thread = None
        self.playwright = None
        self.browser = None
        self.browser_log = []          # 最近 40 条浏览器启动日志
        self.browser_errors = []       # 最近一次启动失败的各候选原因
        self._browser_lock = None
        self._closed = False
        for shop in store.list():
            self._add_session(shop)

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._thread_main,
                                        name="pdd-engine", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._browser_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            self.loop.run_until_complete(self._serve())
        except Exception:
            traceback.print_exc()
        finally:
            try:
                self.loop.run_until_complete(self._cleanup())
            except Exception:
                pass
            self.loop.close()

    async def _serve(self):
        # 启动即自动恢复已登录店铺的在线状态并查一次 1/2/3 星数量
        # （并发，共享浏览器由 _browser_lock 保护；不弹任何浏览器窗口）
        await asyncio.gather(*[s.boot() for s in list(self.sessions.values())],
                             return_exceptions=True)
        # 自动任务：开机后若用户开启了开关、且今天还没跑过，自动登录并举报到上限。
        # 放在 boot 之后，确保登录态已恢复、在线状态已就绪。
        try:
            asyncio.ensure_future(self._auto_task())
        except Exception:
            traceback.print_exc()
        while not self._closed:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            now = time.time()
            for s in list(self.sessions.values()):
                if not s.store.has_login(s.id) or s.busy():
                    continue
                try:
                    if now - s._last_heartbeat >= HEARTBEAT_SECONDS:
                        await s.run_exclusive(s.heartbeat(), kind="heartbeat")
                        s._last_heartbeat = time.time()
                    elif now - s._last_star_check >= STAR_REFRESH_SECONDS:
                        await s.run_exclusive(s.refresh_stars(), kind="refresh")
                        s._last_star_check = time.time()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    traceback.print_exc()

    # ---------- 自动任务 ----------
    async def _auto_task(self):
        """开机后自动登录+举报到上限，每个店铺跑到当日上限为止。

        规则：
          - 仅当 settings.auto_task 开启时执行（用户随时可关 = 手动取消）；
          - **按店铺去重**（auto_shop_date[shop_id] = 日期）：某店今天跑过就跳过。
            这样「今天已经自动跑过一轮」不会把白天新增的店铺挡到明天；
          - 逐个店铺串行：跳过未登录、已到上限的；每跑完一个再跑下一个；
          - 全程可随时被用户点「停止」或被关掉开关打断。
        """
        st = self.settings
        if not st:
            return
        # 再等一小段时间，让 boot 的 refresh_stars 收尾、UI 完成初始化
        await asyncio.sleep(2)
        if self._closed or not st.get("auto_task"):
            return
        today = time.strftime("%Y-%m-%d")
        done = dict(st.get("auto_shop_date") or {})
        sessions = [s for s in list(self.sessions.values())
                    if s.store.has_login(s.id)]
        if not sessions:
            self.emit("auto_task", "自动任务：没有已登录店铺，等待下次开机")
            st.update(auto_result="无已登录店铺")
            return
        todo = [s for s in sessions if done.get(s.id) != today]
        if not todo:
            self.emit("auto_task", "今日各店铺都已执行过自动任务，跳过")
            st.update(auto_date=today, auto_result="今日各店铺已完成")
            return
        total = 0
        ran = 0
        for s in todo:
            if self._closed or not st.get("auto_task"):
                break
            # 用户中途把开关关了 = 手动取消，立即停
            if not s.store.has_login(s.id):
                continue
            if s.core.daily_limit_state():
                done[s.id] = today        # 已到上限也算今天处理过
                st.set("auto_shop_date", done)
                continue
            if s.busy():
                continue  # 用户正在手动操作这家，跳过不打断
            try:
                self.emit("auto_task", "自动举报：%s" % s.name)
                await s.run_exclusive(s.run_report(), kind="report")
                total += int(getattr(s, "reported", 0) or 0)
                ran += 1
                done[s.id] = today
                st.set("auto_shop_date", done)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                traceback.print_exc()
        result = "自动举报完成：%d 家店铺，共 %d 条" % (ran, total)
        # 只要开关仍开着、跑完这一轮，就标记今天已执行，避免重复耗尽额度
        if st.get("auto_task"):
            st.update(auto_date=today, auto_result=result)
        self.emit("auto_task", result)

    async def _auto_report_shop(self, s, why="新增店铺"):
        """单店「当天补跑」：只在这一家店铺刚登录成功时触发。

        这是「一天只跑一次」的补丁：用户白天新增店铺时，全局自动任务今天已经跑过，
        新店会被 auto_date 挡到明天、白漏一整天。改成按店铺记录后，新店登录成功
        立刻补跑一次，已跑过/已到上限的直接跳过。
        """
        st = self.settings
        if not st or self._closed or not st.get("auto_task"):
            return
        today = time.strftime("%Y-%m-%d")
        done = dict(st.get("auto_shop_date") or {})
        if done.get(s.id) == today:
            return
        if s.core.daily_limit_state():
            done[s.id] = today
            st.set("auto_shop_date", done)
            self.emit("auto_task", "新增店铺今天已达上限，跳过：%s" % s.name)
            return
        if s.busy():
            return
        self.emit("auto_task", "%s自动举报：%s" % (why, s.name))
        try:
            await s.run_exclusive(s.run_report(), kind="report")
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()
            return
        done = dict(st.get("auto_shop_date") or {})
        done[s.id] = today
        st.set("auto_shop_date", done)
        self.emit("auto_task", "%s自动举报完成：%s，本次 %d 条" % (
            why, s.name, int(getattr(s, "reported", 0) or 0)))

    # ---------- 多选批量举报 ----------
    def report_many(self, shop_ids):
        """GUI 线程调用：按顺序串行举报勾选的多家店铺。

        串行是故意的：多个店铺同时打接口最容易触发风控，一家跑完再跑下一家。
        """
        ids = [i for i in (shop_ids or []) if i]
        if not ids:
            return

        def _do():
            asyncio.ensure_future(self._report_many(ids))
        self._post(_do)

    async def _report_many(self, ids):
        for sid in ids:
            if self._closed:
                break
            s = self.sessions.get(sid)
            if not s or not s.store.has_login(sid):
                self.emit("multi_report", "跳过未登录店铺")
                continue
            if s.busy():
                self.emit("multi_report", "跳过 %s：正在执行其他任务" % s.name)
                continue
            if s.core.daily_limit_state():
                self.emit("multi_report", "跳过 %s：今日已达上限" % s.name)
                continue
            try:
                self.emit("multi_report", "开始举报：%s" % s.name)
                await s.run_exclusive(s.run_report(), kind="report")
            except asyncio.CancelledError:
                break
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(1)
        self.emit("multi_report", "勾选店铺已处理完")

    async def _cleanup(self):
        for s in list(self.sessions.values()):
            try:
                await s.dispose()
            except Exception:
                pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

    def shutdown(self):
        self._closed = True
        if self.loop and self.loop.is_running() and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)

    # ---------- 浏览器 ----------
    async def _ensure_playwright(self):
        if self.playwright:
            return self.playwright
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        return self.playwright

    def _log_browser(self, text):
        """浏览器启动日志：写内存（界面/诊断用）+ 落盘 pdd_data/browser.log。"""
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        self.browser_log.append(line)
        del self.browser_log[:-40]
        try:
            path = os.path.join(self.store.root, "browser.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _launch_attempts(self):
        """按「成功率优先」排出的启动候选：系统 Chrome → Edge → 内置 Chromium。"""
        out = [("系统 Chrome(channel)", {"channel": "chrome"}),
               ("系统 Edge(channel)", {"channel": "msedge"})]
        for label, path in installed_browsers():
            out.append(("%s·指定路径" % label, {"executable_path": path}))
        bundled = bundled_chromium()
        if bundled:
            out.append(("内置 Chromium", {"executable_path": bundled}))
            out.append(("内置 Chromium·兼容模式",
                        {"executable_path": bundled,
                         "extra": ["--no-sandbox", "--disable-gpu",
                                   "--disable-dev-shm-usage"]}))
        return out

    async def launch_browser(self, headless=True):
        """启动浏览器。逐个候选尝试，失败原因全部记下来，最后能说清为什么起不来。"""
        pw = await self._ensure_playwright()
        args = ["--disable-blink-features=AutomationControlled",
                "--force-device-scale-factor=1"]
        if not headless:
            # 可见窗口：最大化 + 给个屏幕内坐标，避免出现在屏幕外/被主窗口完全遮挡
            args += ["--start-maximized", "--new-window",
                     "--window-position=80,60"]
        errors = []
        for label, opt in self._launch_attempts():
            kw = dict(opt)
            extra = kw.pop("extra", None)
            try:
                b = await pw.chromium.launch(
                    headless=headless, args=args + list(extra or []),
                    timeout=LAUNCH_TIMEOUT_MS, **kw)
                self._log_browser("启动成功：%s（headless=%s）" % (label, headless))
                return b
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = "%s 失败：%s" % (label, _short(e))
                errors.append(msg)
                self._log_browser(msg)
        self.browser_errors = errors
        self._log_browser("全部 %d 种方式都没能启动浏览器" % len(errors))
        return None

    def browser_failure_text(self):
        """给用户看的启动失败说明（含每一种方式的具体报错）。"""
        lines = ["这台电脑没能启动浏览器内核，所以扫码窗口弹不出来。", ""]
        lines.append("已尝试 %d 种方式，全部失败：" % len(self.browser_errors))
        for e in self.browser_errors[-6:]:
            lines.append("  · " + e)
        if not self.browser_errors:
            lines.append("  · 没有找到任何浏览器候选（内置 Chromium 可能缺失）")
        lines.append("")
        lines.append("内置 Chromium 路径：%s" % (bundled_chromium() or "未找到"))
        found = installed_browsers()
        lines.append("系统浏览器：%s" % (
            "、".join(label for label, _ in found) if found else "未检测到"))
        lines.append("")
        lines.append("建议：① 装一个 Google Chrome 后重开软件；"
                     "② 或右键托盘「退出程序」再重开（杀软拦截后重试常能恢复）；"
                     "③ 仍不行请把桌面上的浏览器诊断 txt 发给开发者。")
        return "\n".join(lines)

    async def diagnose_browsers(self):
        """浏览器自检：把环境、路径、逐个候选的真实启动结果写成报告。"""
        lines = []
        A = lines.append
        A("=" * 72)
        A("PDD 工作台 · 浏览器启动自检")
        A("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import updater
            ver = updater.version_info()
            A("版本：%s  内核：%s" % (ver.get("label", "?"), ver.get("version", "?")))
        except Exception:
            pass
        A("=" * 72)
        A("")
        A("【1. 运行环境】")
        A("平台：%s   Python：%s" % (sys.platform, sys.version.split()[0]))
        A("打包运行(frozen)：%s" % bool(getattr(sys, "frozen", False)))
        A("_MEIPASS：%s" % (getattr(sys, "_MEIPASS", "") or "-"))
        A("PLAYWRIGHT_BROWSERS_PATH：%s"
          % (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "(未设置)"))
        A("")
        A("【2. 浏览器来源】")
        found = installed_browsers()
        A("系统浏览器：%s" % ("；".join("%s=%s" % (l, p) for l, p in found)
                          if found else "未检测到"))
        bpath = bundled_chromium()
        A("内置 Chromium：%s" % (bpath or "未找到"))
        if bpath:
            try:
                A("  大小：%.1f MB" % (os.path.getsize(bpath) / 1048576.0))
            except Exception as e:
                A("  读取大小失败：%s" % _short(e))
        A("")
        A("【3. 逐个候选实测（无头模式，只验证能不能起来）】")
        ok_any = False
        fails = []
        try:
            pw = await self._ensure_playwright()
        except Exception as e:
            A("Playwright 初始化失败：%s" % _short(e))
            pw = None
        if pw is not None:
            for label, opt in self._launch_attempts():
                kw = dict(opt)
                extra = kw.pop("extra", None)
                t0 = time.time()
                try:
                    b = await pw.chromium.launch(
                        headless=True, args=list(extra or []),
                        timeout=LAUNCH_TIMEOUT_MS, **kw)
                    A("  [OK] %s（%.1fs）" % (label, time.time() - t0))
                    ok_any = True
                    try:
                        await b.close()
                    except Exception:
                        pass
                except Exception as e:
                    line = "  [X ] %s（%.1fs）：%s" % (label, time.time() - t0,
                                                   _short(e, 200))
                    A(line)
                    fails.append(line.strip().replace("[X ] ", ""))
        if not ok_any and fails:
            # 自检结果同步给启动失败说明，用户看到的两个地方口径一致
            self.browser_errors = fails
        A("")
        A("结论：%s" % ("至少一种方式可用；若仍看不到窗口，多半是被挡在后台或被安全软件拦住"
                     if ok_any else "所有方式都起不来 → 本机缺浏览器内核或被安全软件拦截"))
        text = "\n".join(lines)
        return text

    def write_browser_report(self, text, tag="浏览器诊断"):
        """把自检报告写到桌面，方便用户直接发过来。"""
        try:
            home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        except Exception:
            home = os.path.expanduser("~")
        path = os.path.join(home, "PDD诊断_%s_%s.txt" % (
            tag, time.strftime("%Y%m%d_%H%M%S")))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path
        except Exception:
            alt = os.path.join(self.store.root, "browser_report.txt")
            try:
                with open(alt, "w", encoding="utf-8") as f:
                    f.write(text)
                return alt
            except Exception:
                return ""

    async def get_browser(self):
        """所有店铺共享一个无头浏览器实例，彼此用独立 context 隔离 cookie。"""
        async with self._browser_lock:
            if self.browser is None or not self.browser.is_connected():
                self.browser = await self.launch_browser(headless=True)
            return self.browser

    # ---------- 店铺 ----------
    def _add_session(self, shop):
        s = ShopSession(self, shop)
        self.sessions[shop["id"]] = s
        with self._state_lock:
            self.states[shop["id"]] = s.snapshot()
        return s

    def add_shop(self, shop, then=None, **kw):
        """GUI 线程调用：新增店铺后同步登记，并可在同一次回调里立即执行命令。

        then="login" 用于「添加店铺即扫码」，保证 session 先建好、
        login 任务后启动，不存在命令先到被丢弃的竞态。
        """
        def _do():
            if shop["id"] not in self.sessions:
                self._add_session(shop)
            if then:
                asyncio.ensure_future(self._dispatch(then, shop["id"], kw))
        self._post(_do)

    def remove_shop(self, shop_id):
        def _do():
            asyncio.ensure_future(self._remove_shop(shop_id))
        self._post(_do)

    async def _remove_shop(self, shop_id):
        s = self.sessions.pop(shop_id, None)
        if s:
            s.request_stop()
            await s.dispose()
        with self._state_lock:
            self.states.pop(shop_id, None)

    def rename_shop(self, shop_id, new_name):
        def _do():
            s = self.sessions.get(shop_id)
            if s:
                s.name = new_name
                self.publish(s)
        self._post(_do)

    # ---------- 状态 / 事件 ----------
    def publish(self, session):
        with self._state_lock:
            self.states[session.id] = session.snapshot()
        if session.daily_limit and not session._limit_notified and \
                session.status == "今日已达上限":
            session._limit_notified = True
            self.emit("daily_limit", session.id, session.name, session.reported)

    def emit(self, kind, *args):
        self.events.put((kind,) + tuple(args))

    def pop_events(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    def snapshot(self):
        with self._state_lock:
            return dict((k, dict(v)) for k, v in self.states.items())

    def state_for(self, shop_id):
        with self._state_lock:
            s = self.states.get(shop_id)
            return dict(s) if s else blank_state()

    # ---------- 命令 ----------
    def call(self, cmd, shop_id=None, **kw):
        self._post(lambda: asyncio.ensure_future(
            self._dispatch(cmd, shop_id, kw)))

    def _post(self, fn):
        if not self.loop:
            return
        try:
            self.loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass

    async def _dispatch(self, cmd, shop_id, kw):
        s = self.sessions.get(shop_id) if shop_id else None
        if s is None and cmd not in ("noop", "browser_check"):
            return
        try:
            if cmd == "browser_check":
                # 不依赖具体店铺，且不能被 s.busy() 挡住，所以放在最前面
                try:
                    text = await self.diagnose_browsers()
                    path = self.write_browser_report(text)
                except Exception as e:
                    text = "自检本身出错：%s" % str(e)[:200]
                    path = ""
                self.emit("browser_report", text, path)
                return
            if cmd == "stop":
                # 停止不等队列，直接取消当前长任务
                s.request_stop()
                return
            if cmd == "cancel_login":
                s.request_cancel_login()
                return
            if cmd == "clear_limit":
                s.core.clear_daily_limit()
                s.daily_limit = False
                s._limit_notified = False
                s.set("在线", "当日上限标记已清除，可以重新点「开始举报」")
                return
            if s.busy():
                s.set(message="已有任务在运行，请等它结束或点「停止」")
                return
            if cmd == "login":
                await s.run_exclusive(s.login(timeout=kw.get("timeout", 300)),
                                      kind="login")
                # 新增店铺扫码成功后：若自动任务开着，立刻给这家补跑一次
                # （否则「一天只跑一次」会让新店白漏一天）
                # 只有这次真的登上了（online=True 且不在登录页）才补跑，
                # 别拿上一次残留的登录态文件当成功。
                if (kw.get("new_shop") and s.online
                        and s.page_kind != "login"
                        and s.store.has_login(s.id)):
                    try:
                        await self._auto_report_shop(s, why="新增店铺")
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        traceback.print_exc()
            elif cmd == "refresh":
                await s.run_exclusive(s.heartbeat(), kind="heartbeat")
                if not s.busy():
                    await s.run_exclusive(s.refresh_stars(), kind="refresh")
                s._last_star_check = time.time()
                s._last_heartbeat = time.time()
                # 每次刷新都无条件写一份桌面诊断（纯本地，不依赖云回传），
                # 这样即使读数成功也能核对，读不到时用户可直接把文件发来。
                try:
                    await s.write_desktop_diag(tag="刷新数量")
                except Exception:
                    pass
            elif cmd == "shop_name":
                await s.run_exclusive(s.refresh_shop_name(overwrite=True),
                                      kind="refresh")
            elif cmd == "star_diag":
                await s.run_exclusive(s.export_star_diag(), kind="refresh")
                try:
                    await s.write_desktop_diag(tag="手动诊断")
                except Exception:
                    pass
            elif cmd == "browser_check":
                pass          # 已在最前面处理
            elif cmd == "start_report":
                await s.run_exclusive(s.run_report(), kind="report")
            else:
                return
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()
            if s:
                s.set("异常", "命令执行失败，详见 gui.log")
