# -*- coding: utf-8 -*-
"""
后台调度引擎：一条独立线程跑 asyncio 事件循环，驱动所有店铺会话。

GUI（tkinter 主线程）只做两件事：
  - 每 0.8 秒读一次 snapshot()，刷新表格
  - 用 call() 投递命令、用 pop_events() 取弹窗事件
所有 Playwright 操作都在后台线程里，界面不会卡。
"""
import asyncio
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

    async def launch_browser(self, headless=True):
        """优先系统 Chrome / Edge，最后回退 Playwright 内置 Chromium。"""
        pw = await self._ensure_playwright()
        args = ["--disable-blink-features=AutomationControlled",
                "--force-device-scale-factor=1"]
        if not headless:
            args.append("--start-maximized")
        for channel, label in (("chrome", "Chrome"), ("msedge", "Edge"),
                               (None, "Chromium")):
            try:
                kw = {"headless": headless, "args": args}
                if channel:
                    kw["channel"] = channel
                b = await pw.chromium.launch(**kw)
                return b
            except Exception:
                continue
        return None

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
        if s is None and cmd not in ("noop",):
            return
        try:
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
                if kw.get("new_shop") and s.store.has_login(s.id):
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
