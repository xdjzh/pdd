# -*- coding: utf-8 -*-
"""
把 scripts/report_reviews.py 原样复用到 GUI 多店铺场景。

关键手法：**每个店铺动态加载一份独立的模块实例**。
核心脚本大量使用模块级全局（USER_DATA_DIR / LOG_DIR / DAILY_LIMIT_FILE / DRY_RUN ...），
如果直接 import 一次，多店铺会共用同一份台账和当日上限标记。
用 spec_from_file_location 给每家店一个唯一模块名，就得到彼此隔离的全局命名空间，
核心脚本一行都不用改。
"""
import asyncio
import importlib.util
import os
import re
import sys
import time
import traceback

from .state import core_script_path

# 登录页特征：命中任一关键词就认为还没登录
LOGIN_KEYWORDS = ("扫码登录", "验证码登录", "请输入手机号", "密码登录",
                  "商家版APP", "请登录", "账号登录")

# ------------------------------------------------------------------
# 店铺名自动获取：扫码成功后从商家后台页面读取，避免让用户手输。
# 四个来源按可信度排序：明确选择器 > storage/全局数据 > 页面标题 > 顶部文本。
# ------------------------------------------------------------------
SHOP_NAME_JS = r"""
() => {
  const out = [], seen = new Set();
  const push = (v, src, key) => {
    if (v === null || v === undefined) return;
    let s = String(v).replace(/\s+/g, ' ').trim();
    if (!s || s.length < 2 || s.length > 80) return;
    s = s.split(/[|｜]/)[0].trim();
    s = s.replace(/[\s\-—_]*(拼多多|商家后台|管理后台|首页|登录)\s*$/g, '').trim();
    s = s.replace(/[\s\-—_|｜]+$/, '').trim();
    if (s.length < 2 || s.length > 40 || seen.has(s)) return;
    seen.add(s);
    out.push({v: s, src: src, key: key || ''});
  };
  const strongKey = /^(mallName|shopName|mall_name|shop_name|storeName|store_name|merchantName|mallNick|shopNick|mallTitle|shopTitle)$/i;
  const nameKey = /(mall|shop|store|merchant).*name|name.*(mall|shop|store|merchant)/i;
  const scanObj = (o, depth, src) => {
    if (!o || depth > 4 || out.length >= 60) return;
    if (typeof o === 'string') {
      if (o.length > 4 && /^[{[]/.test(o.trim())) {
        try { scanObj(JSON.parse(o), depth + 1, src); } catch (e) {}
      }
      return;
    }
    if (typeof o !== 'object') return;
    let n = 0;
    for (const k of Object.keys(o)) {
      if (++n > 60 || out.length >= 60) break;
      let v;
      try { v = o[k]; } catch (e) { continue; }
      if (typeof v === 'string' && (strongKey.test(k) || (v.length < 40 && nameKey.test(k)))) {
        push(v, src, k);
      } else if (v && typeof v === 'object' && depth < 4) {
        scanObj(v, depth + 1, src);
      }
    }
  };
  try {
    const re = /"(?:mallName|shopName|mall_name|shop_name|storeName|merchantName|mallNick|shopNick)"\s*:\s*"([^"]{2,60})"/g;
    for (const st of [window.localStorage, window.sessionStorage]) {
      for (let i = 0; i < st.length && i < 80; i++) {
        const v = st.getItem(st.key(i));
        if (!v || v.length > 60000) continue;
        let m; re.lastIndex = 0;
        while ((m = re.exec(v)) !== null) push(m[1], 'storage', 'json');
        const tv = v.trim();
        if (tv.startsWith('{') || tv.startsWith('[')) {
          try { scanObj(JSON.parse(tv), 0, 'storage'); } catch (e) {}
        }
      }
    }
    const globals = ['__INITIAL_STATE__','__NEXT_DATA__','__NUXT__','__PDD_STATE__',
                     'mallInfo','shopInfo','userInfo','loginInfo','merchantInfo','storeInfo'];
    for (const k of globals) {
      if (window[k]) scanObj(window[k], 0, 'state');
    }
  } catch (e) {}
  const sels = [
    '[data-testid*="shopName"]','[data-testid*="mallName"]','[data-testid*="storeName"]',
    '[class*="shopName"]','[class*="shopname"]','[class*="shop-name"]','[class*="shop_name"]',
    '[class*="mallName"]','[class*="mallname"]','[class*="mall-name"]','[class*="mall_name"]',
    '[class*="storeName"]','[class*="merchantName"]','[id*="shopName"]','[id*="mallName"]',
    '[class*="mallInfo"] [class*="name"]','[class*="userInfo"] [class*="name"]'
  ];
  for (const sel of sels) {
    try {
      for (const el of document.querySelectorAll(sel)) {
        if (el.offsetParent === null) continue;
        const t = (el.innerText || el.textContent || el.value || '').trim();
        if (t) push(t.split('\n')[0], 'selector', sel);
      }
    } catch (e) {}
  }
  const zones = document.querySelectorAll('header, [class*="header"], [class*="Header"], [class*="topbar"], [class*="topBar"]');
  for (const z of zones) {
    for (const el of z.querySelectorAll('*')) {
      if (el.offsetParent === null || el.children.length > 0) continue;
      const t = (el.innerText || el.textContent || '').trim();
      if (t && t.length <= 30) push(t, 'header');
    }
  }
  push(document.title, 'title');
  return out.slice(0, 60);
}
"""

# 列表快照：总条数 / 页码 / 每页条数 / 订单指纹 / 选中星级 / 星级标签数量。
# 用于判断「筛选切换后列表是否真的刷新」，避免读到上一次筛选的旧分页数据。
STAR_SNAPSHOT_JS = r"""
() => {
  const ul = document.querySelector('ul[data-testid="beast-core-pagination"]');
  let total = null, totalText = '', page = null, size = null;
  if (ul) {
    const tEl = ul.querySelector('li[class*="PGT_totalText_"]');
    totalText = tEl ? (tEl.innerText || '') : '';
    const m = totalText.match(/([0-9]+)\s*条/);
    if (m) total = parseInt(m[1], 10);
    const st = ul.getAttribute('data-status') || '';
    const sm = st.match(/pagination-([0-9]+)-([0-9]+)/);
    if (sm) { size = parseInt(sm[1], 10); page = parseInt(sm[2], 10); }
    const act = ul.querySelector('li[class*="PGT_pagerItemActive_"]');
    if (page === null && act) page = parseInt((act.textContent || '').trim(), 10);
  }
  if (total === null) {
    const m = (document.body.innerText || '').match(/共(?:有)?\s*([0-9]+)\s*条/);
    if (m) total = parseInt(m[1], 10);
  }
  const tabCounts = {};
  for (const el of document.querySelectorAll('span,div,a,button,li,label,p,td,th')) {
    if (el.offsetParent === null || el.children.length > 2) continue;
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 24) continue;
    const m = t.match(/^([1-5])\s*星[^0-9]{0,6}([0-9]+)/);
    if (!m) continue;
    const n = parseInt(m[2], 10);
    if (n >= 0 && n < 100000) tabCounts[m[1]] = n;
  }
  const body = document.body.innerText || '';
  const ids = body.match(/订单编号[:：]?\s*([0-9A-Za-z-]{8,})/g) || [];
  const activeStars = [];
  for (const el of document.querySelectorAll('[data-pdd-star]')) {
    const cls = (el.className || '').toString().toLowerCase();
    const pcls = el.parentElement ? (el.parentElement.className || '').toString().toLowerCase() : '';
    if (/active|selected|checked|current/.test(cls + ' ' + pcls) ||
        el.getAttribute('aria-checked') === 'true' ||
        el.getAttribute('aria-selected') === 'true') {
      activeStars.push((el.textContent || '').trim());
    }
  }
  return {
    total: total, totalText: totalText.trim(), page: page, size: size,
    cards: (body.match(/举报/g) || []).length,
    tabCounts: tabCounts,
    orderSig: ids.slice(0, 3).join('|'),
    starSig: activeStars.sort().join(','),
    bodyLen: body.length
  };
}
"""

QR_VISIBLE_JS = r"""
() => {
  const els = document.querySelectorAll(
    'img[src*="qr"], img[src*="Qr"], canvas, [class*="qrcode"], ' +
    '[class*="qrCode"], [class*="qr-code"], [class*="QRCode"], ' +
    '[data-testid*="qr"], [class*="qrImg"]');
  for (const el of els) {
    if (el.offsetParent !== null) return true;
  }
  return false;
}
"""

QR_CLICK_JS = r"""
() => {
  const kws = ['扫码登录', '二维码登录', 'APP扫码', '扫码'];
  let best = null, bestLen = Infinity;
  for (const el of document.querySelectorAll('*')) {
    if (el.offsetParent === null) continue;
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 12 || !kws.some(k => t.includes(k))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) continue;
    if (t.length < bestLen) { best = el; bestLen = t.length; }
  }
  if (best) { best.click(); return (best.innerText || '').trim().slice(0, 20); }
  return '';
}
"""

# 来源可信度：结构化状态 > 明确选择器 > 顶部文本 > 页面标题
_SRC_SCORE = {"state": 70, "storage": 65, "selector": 45,
              "header": 30, "title": 25}
# 命中即淘汰：导航词、功能词、登录相关文案
_HARD_BAD = ("登录", "扫码", "验证码", "请输入", "请选择", "帮助中心",
             "官方", "欢迎", "商家版", "后台", "首页", "退出", "公告",
             "系统通知", "立即", "点击", "查看", "更多", "升级", "新版",
             "上线", "下载", "复制", "搜索", "工作台", "拼多多")
# 命中扣分：宣传语常见词（真实店名偶尔也会带，不直接杀）
_SOFT_BAD = ("高效", "同时", "经营", "工具", "客服", "多店", "管理",
             "数据", "订单", "评价", "商品", "营销", "中心", "平台",
             "服务", "助手", "系统")
_NAME_SENTENCE = "，。！？、；：,.!?;:"
_NAME_SHOP_HINTS = ("店", "旗舰", "专营", "专卖", "商贸", "百货", "商行",
                    "优选", "家居", "日用", "纸", "实业", "贸易",
                    "工厂", "厂", "铺", "馆", "坊", "旗舰店")
_NAME_BLACKLIST = frozenset((
    "店铺管理", "店铺设置", "店铺信息", "我的店铺", "店铺首页", "店铺评分",
    "店铺装修", "店铺概况", "店铺资料", "店铺中心", "店铺名称", "店铺账号",
    "商家后台", "管理后台", "拼多多", "拼多多商家后台", "首页", "退出",
    "帮助", "帮助中心", "消息", "客服", "订单管理", "评价管理", "商品管理",
    "数据中心", "营销中心", "登录", "扫码登录", "店铺", "店",
))
_MIN_NAME_SCORE = 38


def _clean_name(v):
    """去掉页面标题/文案常带的后台后缀，JS 侧已洗过一次，这里做防御性兜底。"""
    v = re.sub(r"^[\s\-_|｜]+", "", str(v or "")).strip()
    v = re.sub(r"[\s\-_|｜]+(拼多多|商家后台|管理后台|首页|登录).*$",
               "", v).strip()
    return re.sub(r"[\s\-_|｜]+$", "", v).strip()


def _shop_name_score(v, src, key):
    """给候选店铺名打分；低于阈值一律不回填，宁可显示临时名也不显示乱码。"""
    if not v:
        return -999
    v = _clean_name(v)
    if len(v) < 2 or len(v) > 40 or v in _NAME_BLACKLIST:
        return -999
    if any(w in v for w in _HARD_BAD):
        return -999
    if re.search(r"^https?://|^\d+$|^[{[]", v):
        return -999
    # 带句读的长句基本是页面宣传语（如「高效客服工具，多店同时经营」）
    if any(ch in v for ch in _NAME_SENTENCE):
        return -999
    s = _SRC_SCORE.get(src, 0)
    if key and re.search(r"mall|shop|store|merchant|nick|title", key, re.I):
        s += 12
    if any(k in v for k in _NAME_SHOP_HINTS):
        s += 14
    if any(w in v for w in _SOFT_BAD):
        s -= 30
    if 2 <= len(v) <= 20:
        s += 8
    elif len(v) > 30:
        s -= 12
    return s


def pick_shop_name(candidates):
    """从 DOM 候选里挑分数最高的真实店铺名；挑不出返回空串。"""
    if not isinstance(candidates, list):
        return ""
    best, best_score = "", -999
    for c in candidates:
        if not isinstance(c, dict):
            continue
        v = str(c.get("v") or "")
        score = _shop_name_score(v, c.get("src"), c.get("key"))
        if score > best_score:
            best, best_score = _clean_name(v), score
    return best if best_score >= _MIN_NAME_SCORE else ""


async def extract_shop_name(page):
    """尽力读取当前页面的店铺名；失败一律返回空串，绝不阻断登录流程。"""
    try:
        return pick_shop_name(await page.evaluate(SHOP_NAME_JS))
    except Exception:
        return ""


def load_core_module(shop_id, store):
    """为单个店铺加载独立的核心脚本模块，并注入该店铺专属路径。"""
    path = core_script_path()
    if not os.path.exists(path):
        raise RuntimeError("找不到核心脚本: %s" % path)

    # 核心脚本在 import 时解析 sys.argv，这里临时替换成「正式运行 + 静默」配置
    argv_backup = sys.argv[:]
    sys.argv = ["report_reviews.py", "--run", "--no-alert", "--headless"]
    mod_name = "pdd_core_%s" % "".join(
        c if c.isalnum() else "_" for c in str(shop_id))
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载核心脚本模块")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv_backup

    shop_dir = store.shop_dir(shop_id)
    # 每店铺独立的登录态 / 台账 / 日志 / 当日上限标记
    mod.USER_DATA_DIR = store.browser_dir(shop_id)
    mod.LOG_DIR = shop_dir
    mod.REPORTED_FILE = os.path.join(shop_dir, "reported_ids.json")
    mod.LOG_FILE = os.path.join(shop_dir, "report_log.jsonl")
    mod.PROBE_FILE = os.path.join(shop_dir, "probe_dialog.html")
    mod.DAILY_LIMIT_FILE = os.path.join(shop_dir, "daily_limit.json")
    # GUI 模式固定：正式提交、无头、静默（桌面提醒由 GUI 统一弹）
    mod.DRY_RUN = False
    mod.PROBE_MODE = False
    mod.SCAN_MODE = False
    mod.HEADLESS = True
    mod.SHOW_DESKTOP_ALERT = False
    mod.FORCE_RUN = False
    mod.SINGLE_ENTRY_ONLY = True
    mod.FALLBACK_ON_BLOCKED = False
    mod.FALLBACK_ON_SUBMIT_REJECT = False
    mod.START_PAGE = 1
    mod.MAX_PAGES = 0
    mod.TARGET_STARS = [1, 2, 3]
    # 刷新星级数量时不要被「今日已达上限」拦住；举报任务自己会检查
    return mod


class ShopSession(object):
    """一个店铺 = 一个独立浏览器上下文 + 一份核心脚本模块实例。"""

    def __init__(self, engine, shop):
        self.engine = engine
        self.store = engine.store
        self.id = shop["id"]
        self.name = shop["name"]
        # True = 扫码前的临时名，登录成功后要用平台真实店铺名覆盖
        self.auto_name = bool(shop.get("auto_name"))
        self.core = load_core_module(self.id, self.store)
        self.reporter = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        self.current_task = None
        self.task_kind = ""
        self.cancel_login = False
        self.stop_report = False
        self.online = False
        self.stars = {1: 0, 2: 0, 3: 0}
        self.stars_ok = False   # False = 数量待确认，界面数字后加 ?
        self.reported = 0
        self.status = "未登录"
        self.message = ""
        self.last_check = ""
        self.daily_limit = False
        self._limit_notified = False
        self._last_heartbeat = 0.0
        self._last_star_check = 0.0
        self._login_browser = None

    # ---------- 状态 ----------
    def set(self, status=None, message="", online=None, **kw):
        if status is not None:
            self.status = status
        if message:
            self.message = message
        if online is not None:
            self.online = online
        for k, v in kw.items():
            setattr(self, k, v)
        self.engine.publish(self)

    def snapshot(self):
        return {
            "name": self.name,
            "online": bool(self.online),
            "status": self.status,
            "star1": self.stars.get(1, 0),
            "star2": self.stars.get(2, 0),
            "star3": self.stars.get(3, 0),
            "stars_ok": bool(self.stars_ok),
            "reported": self.reported,
            "last_check": self.last_check,
            "daily_limit": bool(self.daily_limit),
            "message": self.message,
        }

    # ---------- 任务串行化 ----------
    def busy(self):
        return self.current_task is not None and not self.current_task.done()

    async def run_exclusive(self, coro, kind=""):
        """同一店铺同一时间只允许一个长任务，避免心跳和举报抢同一个页面。"""
        if self.busy():
            try:
                coro.close()
            except Exception:
                pass
            return False
        task = asyncio.ensure_future(coro)
        self.current_task = task
        self.task_kind = kind
        try:
            await task
            return True
        except asyncio.CancelledError:
            raise
        finally:
            if self.current_task is task:
                self.current_task = None
                self.task_kind = ""

    # ---------- 无头上下文（登录后隐藏浏览器） ----------
    async def ensure_context(self, force=False):
        """用该店铺 storage_state 建立不可见的后台上下文。"""
        if self.context and not force:
            return True
        browser = await self.engine.get_browser()
        if browser is None:
            self.set("浏览器未就绪", "没找到可用的 Chrome / Edge / Chromium")
            return False
        # 重建前先把续期后的 cookie 落盘，别把在线状态弄丢
        if self.context:
            await self.save_login_state()
        await self.close_context()
        storage = self.store.storage_file(self.id)
        kwargs = {"no_viewport": True}
        if os.path.exists(storage):
            kwargs["storage_state"] = storage
        self.context = await browser.new_context(**kwargs)
        self.page = await self.context.new_page()
        self.reporter = self.core.PinduoduoReviewReporter()
        self.reporter.browser = browser
        self.reporter.context = self.context
        self.reporter.page = self.page
        return True

    async def close_context(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        self.context = None
        self.page = None
        self.reporter = None

    async def save_login_state(self):
        """心跳时把续期后的 cookie 写回，保证「明天打开还是在线」。"""
        if not self.context:
            return
        try:
            await self.context.storage_state(
                path=self.store.storage_file(self.id))
        except Exception:
            pass

    # ---------- 店铺名自动获取 ----------
    def _apply_auto_name(self, name):
        """只在「临时名」状态下回填；用户手动重命名过就不再覆盖。"""
        if not name or not self.auto_name:
            return False
        try:
            self.store.rename(self.id, name)
        except Exception:
            return False
        self.name = name
        self.auto_name = False
        self.set(message="已自动获取店铺名：%s" % name)
        return True

    async def refresh_shop_name(self, overwrite=False):
        """重新从商家后台读取店铺名；抓不到绝不覆盖现有名字。"""
        if not self.page:
            return ""
        name = ""
        for _ in range(5):
            name = await extract_shop_name(self.page)
            if name:
                break
            await asyncio.sleep(0.6)
        if not name:
            self.set(message="页面未找到店铺名，可右键「重命名」手动填写")
            return ""
        if overwrite or self.auto_name:
            try:
                self.store.rename(self.id, name)
            except Exception:
                return ""
            self.name = name
            self.auto_name = False
            self.set(message="已更新店铺名：%s" % name)
        return name

    async def _auto_name_if_needed(self):
        """后台页面（评价管理页）再兜底抓一次，防止登录瞬间店铺名还没渲染。"""
        if not self.auto_name or not self.page:
            return
        await self.refresh_shop_name()

    # ---------- 扫码登录 ----------
    async def _activate_qr_tab(self, page):
        """登录页默认可能停在手机号/密码，主动切到二维码栏并确认已渲染。"""
        for _ in range(8):
            try:
                clicked = False
                for kw in ("扫码登录", "二维码登录", "APP扫码", "扫码"):
                    loc = page.get_by_text(kw, exact=False)
                    for j in range(min(await loc.count(), 8)):
                        el = loc.nth(j)
                        try:
                            if not await el.is_visible():
                                continue
                            txt = (await el.inner_text() or "").strip()
                            if not txt or len(txt) > 20:
                                continue
                            await el.click(timeout=1500)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if clicked:
                        break
                if not clicked:
                    await page.evaluate(QR_CLICK_JS)
                await asyncio.sleep(0.5)
                if await page.evaluate(QR_VISIBLE_JS):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False

    async def login(self, timeout=300):
        """打开可见浏览器给商家版 APP 扫码；成功后立刻关掉窗口，改跑无头后台。

        注意：串行化由 run_exclusive 负责，这里不能再检查 current_task，
        否则自己就会被自己判定成「已有任务在运行」而立刻退出。
        """
        self.cancel_login = False
        self.set("扫码中", "已打开登录窗口，请用拼多多商家版 APP 扫码", online=False)
        browser = None
        ctx = None
        try:
            browser = await self.engine.launch_browser(headless=False)
            if browser is None:
                self.set("登录失败", "没找到可用的 Chrome / Edge / Chromium")
                return False
            self._login_browser = browser
            ctx = await browser.new_context(no_viewport=True)
            page = await ctx.new_page()
            await page.goto(self.core.PDD_MMS_URL, wait_until="domcontentloaded")
            await asyncio.sleep(1.2)
            # 关键：登录页默认可能停在手机号/密码，主动切到「扫码登录」
            qr_ok = await self._activate_qr_tab(page)
            self.set("扫码中",
                     "二维码已就绪，请用拼多多商家版 APP 扫码"
                     if qr_ok else
                     "已打开登录窗口，若显示手机号登录请点「扫码登录」")

            ok = False
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.cancel_login:
                    self.set("已取消", "扫码登录已取消")
                    return False
                ok = await self._is_logged_in(page, ctx)
                if ok:
                    break
                left = int(deadline - time.time())
                self.set("扫码中", "等待扫码… 剩余 %d 秒（可点「取消登录」）" % max(0, left))
                await asyncio.sleep(2)

            if not ok:
                self.set("扫码超时", "5 分钟内未完成扫码，请重新点「扫码登录」")
                return False

            # 扫码成功瞬间可能还停在欢迎/宣传页，等后台真正渲染完再取店名
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            for _ in range(8):
                if self._apply_auto_name(await extract_shop_name(page)):
                    break
                await asyncio.sleep(0.8)

            # 保存登录态，然后关闭可见窗口 —— 这就是「登录后隐藏」
            await ctx.storage_state(path=self.store.storage_file(self.id))
            self.set("登录成功", "登录窗口已关闭，正在切换为后台在线")
        except Exception as e:
            self.set("登录失败", str(e)[:160])
            traceback.print_exc()
            return False
        finally:
            # 无论成功、失败、超时还是被取消，可见登录窗口都必须关掉
            self._login_browser = None
            for cleanup in (lambda: ctx.close() if ctx else None,
                            lambda: browser.close() if browser else None):
                try:
                    result = cleanup()
                    if result is not None:
                        await result
                except BaseException:
                    pass

        try:
            await self.ensure_context(force=True)
            await self._enter_review_page()
            self.online = True
            await self.refresh_stars()
            self.online = True
            self._last_heartbeat = time.time()
            self._last_star_check = time.time()
            self.set("在线", "登录态已保持，浏览器已隐藏")
            return True
        except Exception as e:
            self.set("在线", "已进入后台，但评价页初始化失败：%s" % str(e)[:120])
            return True

    def request_cancel_login(self):
        self.cancel_login = True

    async def _is_logged_in(self, page, ctx):
        try:
            url = (page.url or "").lower()
            if ("login" in url or "passport" in url or "signin" in url):
                return False
            cookies = await ctx.cookies()
            if len(cookies) < 3:
                return False
            body = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 4000)")
            if any(k in body for k in LOGIN_KEYWORDS):
                return False
            return "mms.pinduoduo.com" in url
        except Exception:
            return False

    # ---------- 在线保持 / 星级数量 ----------
    async def boot(self):
        """程序启动：已有登录态就直接恢复后台在线，不弹任何浏览器窗口。"""
        if not self.store.has_login(self.id):
            self.set("未登录", "点「扫码登录」添加店铺登录态")
            return
        self.set("连接中", "正在用已保存的登录态恢复在线")
        try:
            await self.ensure_context(force=True)
            await self._enter_review_page()
            self.online = True
            await self.save_login_state()
            await self.refresh_stars()
            self.online = True
            self._last_heartbeat = time.time()
            self._last_star_check = time.time()
            self.set("在线", "登录态已恢复，浏览器保持隐藏")
        except Exception as e:
            self.set("掉线，请重新扫码", str(e)[:160], online=False)

    async def _enter_review_page(self):
        """轻量进入评价管理页：不做核心脚本那一整套诊断截图，避免每次心跳都刷屏。"""
        if not self.page:
            raise RuntimeError("页面未就绪")
        await self.page.goto(self.core.REVIEW_MANAGE_URL,
                             wait_until="domcontentloaded", timeout=30000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await asyncio.sleep(2)
        url = (self.page.url or "").lower()
        if "login" in url or "passport" in url:
            self.online = False
            raise RuntimeError("登录态已失效，需要重新扫码")
        # 登录瞬间若没抓到店铺名，进入后台页面后再兜底抓一次
        try:
            await self._auto_name_if_needed()
        except Exception:
            pass
        return True

    async def heartbeat(self):
        """轻量在线心跳：续期 cookie + 掉线检测，不刷新星级（星级单独按更长的周期查）。"""
        if not self.store.has_login(self.id):
            return
        try:
            if not self.context:
                await self.ensure_context(force=True)
            await self._enter_review_page()
            self.online = True
            self._last_heartbeat = time.time()
            await self.save_login_state()
            self.set("在线" if not self.daily_limit else "今日已达上限",
                     "登录态已续期")
        except Exception as e:
            self.set("掉线，请重新扫码", str(e)[:160], online=False)

    async def refresh_stars_due(self, interval):
        """到点才查 1/2/3 星数量，避免频繁筛选触发平台风控。"""
        if not self.online or self.busy():
            return
        if time.time() - self._last_star_check < interval:
            return
        await self.run_exclusive(self.refresh_stars())
        self._last_star_check = time.time()

    async def _star_snapshot(self):
        """一次读取列表指纹 + 分页总数 + 星级标签数量。"""
        if not self.page:
            return {}
        try:
            return await self.page.evaluate(STAR_SNAPSHOT_JS)
        except Exception:
            return {}

    async def _wait_star_data(self, star, before, timeout=14):
        """筛选后必须等到列表真的换了数据、且总数连续稳定，才采纳读数。

        旧实现切完星级立刻读分页，接口还没返回，三个星级读到的都是
        同一个旧总数（截图里的 7 / 7 / 7 就是这么来的）。
        """
        deadline = time.time() + timeout
        last_total, stable = None, 0
        before_total = before.get("total")
        before_orders = before.get("orderSig") or ""
        changed = False
        while time.time() < deadline:
            snap = await self._star_snapshot()
            orders = snap.get("orderSig") or ""
            total = snap.get("total")
            if (orders and orders != before_orders) or \
                    (total is not None and total != before_total):
                changed = True
            if changed and total is not None:
                if total == last_total:
                    stable += 1
                    if stable >= 2:
                        return total, True, snap
                else:
                    stable = 0
                    last_total = total
            await asyncio.sleep(0.7)
        last = await self._star_snapshot()
        # 超时也给出当前读数，但 ok=False 让界面标「?」，绝不静默当成准确值
        return (last_total if last_total is not None else last.get("total")), \
            False, last

    async def refresh_stars(self):
        """读取 1/2/3 星数量：优先标签自带数量，否则逐个筛选并等真实刷新。

        核心脚本一行未改；所有等待与校验都在 GUI 侧完成，举报流程不受影响。
        """
        if not self.reporter or not self.page:
            return
        r = self.reporter
        first = await self._star_snapshot()
        tabs = first.get("tabCounts") or {}
        # 不少列表页的星级 tab 直接带数量，一次读取最准、也最省平台请求
        if all(str(k) in tabs for k in (1, 2, 3)):
            for star in (1, 2, 3):
                self.stars[star] = int(tabs[str(star)])
            self.stars_ok = True
            self.last_check = time.strftime("%H:%M:%S")
            self.engine.publish(self)
            return

        reliable = True
        for star in (1, 2, 3):
            before = await self._star_snapshot()
            try:
                await r.filter_by_star(star)
            except Exception:
                pass
            n, ok, after = await self._wait_star_data(star, before)
            if n is None:
                try:
                    pages = await r._detect_total_pages(wait_stable=False)
                except Exception:
                    pages = 0
                size = after.get("size") or 10
                n = pages * size if pages else 0
                ok = False
            self.stars[star] = max(0, int(n or 0))
            reliable = reliable and ok
            self.engine.publish(self)
            await asyncio.sleep(0.4)

        vals = [self.stars[i] for i in (1, 2, 3)]
        if vals[0] == vals[1] == vals[2] and vals[0] > 0 and not reliable:
            reliable = False
        self.stars_ok = reliable
        self.last_check = time.strftime("%H:%M:%S")
        self.engine.publish(self)

    # ---------- 举报任务 ----------
    async def run_report(self):
        if not self.store.has_login(self.id):
            self.set("未登录", "请先点「扫码登录」")
            return
        if self.core.daily_limit_state():
            self.daily_limit = True
            self.set("今日已达上限", "平台风控，明日额度恢复后自动可继续")
            self.engine.emit("daily_limit", self.id, self.name, self.reported)
            return

        self.stop_report = False
        self._limit_notified = False
        self.reported = 0
        self.daily_limit = False
        self.set("举报中", "只走「同行恶意差评」单入口；遇风控立即暂停")
        try:
            await self.ensure_context()
            await self._enter_review_page()
            r = self.reporter
            r.daily_limit_reached = False
            r.stats["total_reported"] = 0
            for star in (1, 2, 3):
                if self.stop_report or r.daily_limit_reached:
                    break
                await r.run_filter_and_report(star)
                self.reported = int(r.stats.get("total_reported", 0))
                self.engine.publish(self)
                if r.daily_limit_reached:
                    break
                await asyncio.sleep(1)
            self.reported = int(r.stats.get("total_reported", 0))
            if r.daily_limit_reached or self.core.daily_limit_state():
                self.daily_limit = True
                self.set("今日已达上限", "本次已举报 %d 条，明日自动恢复" % self.reported)
                if not self._limit_notified:
                    self._limit_notified = True
                    self.engine.emit("daily_limit", self.id, self.name, self.reported)
            elif self.stop_report:
                self.set("已停止", "本次已举报 %d 条" % self.reported)
            else:
                self.set("在线", "本轮处理完毕，本次已举报 %d 条" % self.reported)
                await self.refresh_stars()
        except asyncio.CancelledError:
            self.set("已停止", "任务已停止")
            raise
        except Exception as e:
            traceback.print_exc()
            self.set("异常", str(e)[:160])
        finally:
            self.engine.publish(self)

    def request_stop(self):
        """停止当前任务。登录任务用标志位温和退出，避免浏览器窗口来不及关闭。"""
        self.stop_report = True
        if self.task_kind == "login":
            self.cancel_login = True
            return
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()

    async def dispose(self):
        await self.save_login_state()
        self.request_stop()
        await self.close_context()
