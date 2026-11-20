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
import json
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

# 列表快照：总条数 / 页码 / 每页条数 / 列表指纹 / 星级标签计数候选。
# 注意：只取第一个分页组件、或用「订单编号」判断列表是否刷新，都会导致
# 三个星级读到同一个旧数字（历史上表现为 7? / 7? / 7?）。
# 现在把所有候选一并交给 Python 侧按可信度裁决，并额外提供诊断素材。
STAR_SNAPSHOT_JS = r"""
() => {
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const visible = el => { try { return !!el && el.offsetParent !== null; } catch (e) { return false; } };
  const cls = el => String((el && el.className) || '').slice(0, 160);
  const hash = s => {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(16);
  };

  // 1) 枚举所有分页组件：页面上可能不止一个列表
  const pagers = [];
  for (const ul of document.querySelectorAll(
      'ul[data-testid="beast-core-pagination"], ul[data-testid*="pagination"]')) {
    let totalText = '';
    const tEl = ul.querySelector('li[class*="PGT_totalText_"]');
    if (tEl) totalText = clean(tEl.innerText || tEl.textContent);
    if (!totalText) {
      for (const li of ul.querySelectorAll('li')) {
        const t = clean(li.innerText || li.textContent);
        if (/共有?\s*[0-9]+\s*条/.test(t)) { totalText = t; break; }
      }
    }
    const tm = totalText.match(/([0-9]+)\s*条/);
    const st = ul.getAttribute('data-status') || '';
    const sm = st.match(/pagination-([0-9]+)-([0-9]+)/);
    pagers.push({
      total: tm ? parseInt(tm[1], 10) : null,
      totalText: totalText, status: st,
      size: sm ? parseInt(sm[1], 10) : null,
      page: sm ? parseInt(sm[2], 10) : null,
      visible: visible(ul)
    });
  }
  const mainPager = pagers.find(p => p.visible && p.total !== null) ||
                    pagers.find(p => p.total !== null) || pagers[0] || null;
  let total = mainPager ? mainPager.total : null;
  if (total === null) {
    const m = (document.body.innerText || '').match(/共(?:有)?\s*([0-9]+)\s*条/);
    if (m) total = parseInt(m[1], 10);
  }

  // 2) 星级数字候选：全部收集，Python 侧按「是否在筛选区 / 是否冲突」裁决
  const tabRaw = [], starTexts = [], countTexts = [];
  for (const el of document.querySelectorAll(
      'span,div,a,button,li,label,p,td,th,option,em,b,strong')) {
    if (!visible(el) || el.children.length > 3) continue;
    const t = clean(el.innerText || el.textContent || '');
    if (!t || t.length > 30) continue;
    if (/给出了|好评|差评|评价内容|追评|回复|买家/.test(t)) continue;
    const c = cls(el) + ' ' + (el.parentElement ? cls(el.parentElement) : '');
    if (/星/.test(t) && starTexts.length < 60) starTexts.push(t);
    if (/[0-9]+\s*条|共|合计/.test(t) && countTexts.length < 40) countTexts.push(t);
    const m = t.match(/^([1-5]|[一二三四五])\s*[星★分][^0-9]{0,12}([0-9]+)/);
    if (!m) continue;
    const cn = {'一':1,'二':2,'三':3,'四':4,'五':5};
    const sv = cn[m[1]] ? cn[m[1]] : parseInt(m[1], 10);
    if (!sv || sv < 1 || sv > 5) continue;
    const n = parseInt(m[2], 10);
    if (!Number.isFinite(n) || n < 0 || n > 999999) continue;
    tabRaw.push({
      star: sv, n: n, text: t.slice(0, 40), tag: el.tagName,
      cls: c.slice(0, 160),
      filterish: /tab|filter|star|level|radio|segment|nav|score|count|num|option/i.test(c) ? 1 : 0
    });
  }

  // 3) 列表指纹：不依赖「订单编号」字样（页面常写「订单号」或干脆不显示）
  const body = document.body.innerText || '';
  const host = document.querySelector(
      '[class*="table"], [class*="Table"], [class*="list"], [class*="List"], tbody') || document.body;
  const listText = clean((host.innerText || body).slice(0, 1500));
  const ids = body.match(/(?:订单编号|订单号|订单SN)[:：]?\s*([0-9A-Za-z-]{8,})/g) || [];
  const activeStars = [];
  for (const el of document.querySelectorAll('[data-pdd-star]')) {
    const c = (cls(el) + ' ' + (el.parentElement ? cls(el.parentElement) : '')).toLowerCase();
    if (/active|selected|checked|current/.test(c) ||
        el.getAttribute('aria-checked') === 'true' ||
        el.getAttribute('aria-selected') === 'true') {
      activeStars.push(clean(el.textContent));
    }
  }
  return {
    url: location.href, title: document.title,
    total: total, totalText: mainPager ? mainPager.totalText : '',
    page: mainPager ? mainPager.page : null, size: mainPager ? mainPager.size : null,
    pagers: pagers.slice(0, 6), tabRaw: tabRaw.slice(0, 40),
    starTexts: starTexts, countTexts: countTexts,
    cards: (body.match(/举报/g) || []).length,
    listSig: hash(listText), listHead: listText.slice(0, 300),
    orderSig: ids.slice(0, 3).join('|'),
    starSig: activeStars.sort().join(','), bodyLen: body.length
  };
}
"""

# 逐页统计评价卡片的星级：完全不依赖星级筛选控件。
# 举报流程本身就用「给出了 N 星」/「用户评价分：★★★★★」判定星级，
# 所以这里复用同一套语义，只读不点，不会触发筛选风控。
STAR_CARDS_JS = r"""
() => {
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const mask = s => clean(s).replace(/[0-9]{8,}/g, '<NUM>');
  const cn = {'一':1,'二':2,'三':3,'四':4,'五':5};
  const counts = {1:0,2:0,3:0,4:0,5:0};
  const seen = new Set(), samples = [];
  const collect = (onlyLeaf) => {
    for (const el of document.querySelectorAll('body *')) {
      if (el.offsetParent === null) continue;
      if (onlyLeaf && el.children.length > 0) continue;
      const t = clean(el.innerText || el.textContent || '');
      if (!t || t.length < 4 || t.length > 600) continue;
      let star = null;
      const m = t.match(/给出了\s*([1-5一二三四五])\s*星/);
      if (m) star = cn[m[1]] ? cn[m[1]] : parseInt(m[1], 10);
      if (star === null) {
        const m2 = t.match(/用户评价分[:：]?\s*([★☆]{1,5})/);
        if (m2) star = (m2[1].match(/★/g) || []).length;
      }
      if (star === null || star < 1 || star > 5) continue;
      const key = star + '|' + mask(t).slice(0, 90);
      if (seen.has(key)) continue;
      seen.add(key);
      counts[star] += 1;
      if (samples.length < 6) samples.push({star: star, text: mask(t).slice(0, 140)});
    }
  };
  collect(true);
  if (Object.values(counts).reduce((a, b) => a + b, 0) === 0) collect(false);
  return {counts: counts, samples: samples,
          total: Object.values(counts).reduce((a, b) => a + b, 0)};
}
"""

# 页面类型判定：必须区分「评价管理页」和「商品列表页」。
# 后者也有分页「共有 N 条」，但那是商品数量，被误当成评价数就会出现 7/7/7。
PAGE_KIND_JS = r"""
() => {
  const u = location.href || '';
  const t = document.title || '';
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const body = (document.body && document.body.innerText) || '';
  const links = [], seen = new Set();
  for (const a of document.querySelectorAll(
      'a[href*="evaluat"], a[href*="comment"], a[href*="review"]')) {
    const href = a.getAttribute('href') || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    links.push({t: clean(a.innerText || a.textContent || '').slice(0, 30),
                href: href});
    if (links.length >= 12) break;
  }
  const goodsList = /goods_list/.test(u) || /商品列表/.test(t);
  const reviewUrl = /evaluat|comment|review/i.test(u) && !goodsList;
  const starTextCount = (body.match(/给出了\s*[1-5]\s*星|用户评价分/g) || []).length;
  const reportBtnCount = (body.match(/举报/g) || []).length;
  // 商品列表页绝不会有「给出了 N 星」的评价卡片；若两类评价特征同时出现，
  // 即使 SPA 没改 URL，也要认定已经进入评价页。
  const reviewContent = starTextCount > 0 && reportBtnCount >= 3;
  return {
    url: u, title: t, goodsList: goodsList, reviewUrl: reviewUrl,
    isReview: ((!goodsList && (reviewUrl || starTextCount > 0 ||
              (/评价管理|评价举报/.test(body) && reportBtnCount >= 3))) || reviewContent),
    links: links, starTextCount: starTextCount,
    reportBtnCount: reportBtnCount, head: clean(body).slice(0, 300)
  };
}
"""

# 诊断导出：把「页面上到底长什么样」原样留档，方便按真实 DOM 精确适配。
# 长数字串一律打码，避免诊断文件里带出完整订单号 / 手机号。
STAR_DIAG_JS = r"""
() => {
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const mask = s => clean(s).replace(/[0-9]{8,}/g, '<NUM>')
                            .replace(/([?&](?:token|access_token|sign|anti[^=]*)=)[^&\s]{8,}/gi,
                                     '$1<TOKEN>');
  const visible = el => { try { return el && el.offsetParent !== null; } catch (e) { return false; } };
  const texts = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 0 || !visible(el)) continue;
    const t = clean(el.innerText || el.textContent || '');
    if (!t || t.length > 60 || !/[星条共级评价筛选]/.test(t)) continue;
    texts.push({t: mask(t), tag: el.tagName,
                cls: String(el.className || '').slice(0, 120)});
    if (texts.length >= 150) break;
  }
  // 星级控件真实 HTML：这是校准筛选选择器最关键的素材
  const starEls = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el) || el.children.length > 4) continue;
    const t = clean(el.innerText || el.textContent || '');
    if (!t || t.length > 40) continue;
    if (!/[星★☆评分等级]/.test(t) && !/star|rate|score|level/i.test(String(el.className || ''))) continue;
    starEls.push({
      t: mask(t), tag: el.tagName,
      cls: String(el.className || '').slice(0, 140),
      html: mask(el.outerHTML || '').slice(0, 420),
      parentHtml: mask((el.parentElement && el.parentElement.outerHTML) || '').slice(0, 520)
    });
    if (starEls.length >= 70) break;
  }
  const pagers = [];
  for (const ul of document.querySelectorAll(
      'ul[data-testid="beast-core-pagination"], [data-testid*="pagination"], [class*="pagination"]')) {
    pagers.push(mask(ul.outerHTML || '').slice(0, 1500));
    if (pagers.length >= 4) break;
  }
  const buttons = [];
  for (const el of document.querySelectorAll(
      'button,[role="button"],[role="tab"],[role="radio"],[class*="tab"],[class*="select"],option')) {
    if (!visible(el)) continue;
    buttons.push({
      t: mask(clean(el.innerText || el.textContent || '').slice(0, 50)),
      cls: String(el.className || '').slice(0, 120),
      sel: el.getAttribute('aria-selected'), chk: el.getAttribute('aria-checked')
    });
    if (buttons.length >= 100) break;
  }
  // 页面实际发过哪些接口：用于找出评价列表 API 与它的星级参数
  const resources = [];
  try {
    for (const e of performance.getEntriesByType('resource')) {
      if (!/xmlhttprequest|fetch/i.test(e.initiatorType || '')) continue;
      resources.push(mask(e.name || '').slice(0, 220));
      if (resources.length >= 60) break;
    }
  } catch (e) {}
  const frames = [];
  for (const f of document.querySelectorAll('iframe,frame')) {
    frames.push(mask(f.getAttribute('src') || f.name || '').slice(0, 200));
    if (frames.length >= 10) break;
  }
  const globals = {};
  for (const k of ['__INITIAL_STATE__','__NEXT_DATA__','__NUXT__','__PDD_STATE__']) {
    try { if (window[k]) globals[k] = mask(JSON.stringify(window[k]).slice(0, 4000)); }
    catch (e) {}
  }
  return {
    url: location.href, title: document.title,
    bodyHead: mask((document.body.innerText || '').slice(0, 2500)),
    htmlHead: mask((document.documentElement.outerHTML || '').slice(0, 3000)),
    texts: texts, starEls: starEls, pagers: pagers, buttons: buttons,
    resources: resources, frames: frames, globals: globals
  };
}
"""

# ------------------------------------------------------------------
# 星级数量：优先读评价接口返回的 JSON，其次读页面星级标签，
# 最后才用「逐个筛选 + 分页总数」。任何来源拿不准都不写假数字。
# ------------------------------------------------------------------
_API_KEYWORDS = ("evaluate", "comment", "review", "appraise", "remark",
                 "feedback", "goods/list", "getlist")
_STAR_FIELD_RE = re.compile(r"^(star|starlevel|star_level|score|level|rating|grade|starnum)$", re.I)
_COUNT_FIELD_RE = re.compile(r"^(count|num|cnt|total|totalcount|total_num|totalnum|"
                             r"totalsize|totalrows|reviewnum|commentnum|evaluatenum)$", re.I)
_TOTAL_FIELD_RE = re.compile(r"^(total|totalcount|total_count|totalnum|total_num|"
                             r"totalsize|total_size|totalrows|cnt)$", re.I)


def _is_count(v):
    """合理条数：非负整数、不是布尔、不是订单号那种巨大数字。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return 0 <= v < 1000000 and float(v).is_integer()


def parse_star_payload(data):
    """从评价接口 JSON 里挖「星级计数」与「总数」。

    兼容三种常见形态：
      1. {"1": 12, "2": 3, "3": 5} / {"star1Count": 12, ...}
      2. [{"star": 1, "count": 12}, {"level": 2, "num": 3}, ...]
      3. 任意层级的 {"total": 381} / {"totalCount": 381}
    返回 (star_map or None, totals list)。
    """
    maps, totals = [], []

    def walk(o, depth=0):
        if depth > 8:
            return
        if isinstance(o, list):
            sm = {}
            for item in o[:200]:
                if not isinstance(item, dict):
                    continue
                star = cnt = None
                for k, v in item.items():
                    ks = str(k)
                    if _STAR_FIELD_RE.match(ks) and _is_count(v) and 1 <= int(v) <= 5:
                        star = int(v)
                    if _COUNT_FIELD_RE.match(ks) and _is_count(v):
                        cnt = int(v)
                if star and cnt is not None:
                    sm[star] = cnt
            if all(k in sm for k in (1, 2, 3)):
                maps.append(sm)
            for item in o[:200]:
                walk(item, depth + 1)
            return
        if not isinstance(o, dict):
            return
        sm = {}
        for k, v in o.items():
            ks = str(k)
            if ks in ("1", "2", "3", "4", "5") and _is_count(v):
                sm[int(ks)] = int(v)
            elif "star" in ks.lower() and _is_count(v) and re.search(r"[1-5]", ks) and \
                    re.search(r"count|num|total|cnt", ks, re.I):
                sm[int(re.search(r"[1-5]", ks).group())] = int(v)
            if _TOTAL_FIELD_RE.match(ks) and _is_count(v):
                totals.append(int(v))
            if isinstance(v, (dict, list)):
                walk(v, depth + 1)
            elif isinstance(v, str) and v[:1] in "{[" and len(v) < 200000:
                try:
                    walk(json.loads(v), depth + 1)
                except Exception:
                    pass
        if all(k in sm for k in (1, 2, 3)):
            maps.append(sm)

    try:
        walk(data)
    except Exception:
        pass
    good = [m for m in maps if any(m.get(k, 0) > 0 for k in (1, 2, 3))]
    best = (good or maps or [None])[-1]
    return best, totals[:12]


def pick_tab_counts(raw):
    """从 DOM 候选里裁决 1/2/3 星数量；有冲突或缺失就返回空，绝不猜。

    同一个星级出现多个候选时，只比较「最像筛选标签」的那一组；
    组内数值不一致就判定冲突，宁可显示「—」也不随便挑一个。
    """
    if not isinstance(raw, list):
        return {}
    by_star = {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            star, n = int(c.get("star")), int(c.get("n"))
        except (TypeError, ValueError):
            continue
        if star not in (1, 2, 3) or n < 0 or n > 999999:
            continue
        rank = int(c.get("filterish") or 0)
        by_star.setdefault(star, []).append((rank, n))
    out = {}
    for star in (1, 2, 3):
        items = by_star.get(star) or []
        if not items:
            return {}
        top = max(r for r, _ in items)
        vals = {n for r, n in items if r == top}
        if len(vals) != 1:
            return {}
        out[star] = vals.pop()
    return out


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
        self.stars_ok = False   # False = 数量未确认，界面显示「—」而不是假数字
        self.stars_source = ""  # api / tab / pager / none，便于排查
        self.star_note = ""
        self.api_hints = []     # 最近若干条评价接口响应摘要
        self.api_urls = []      # 页面实际发过的 XHR 地址（诊断用）
        self._diag_uploaded_at = 0.0   # 失败诊断自动回传去重
        self.page_kind = ""        # review / goods_list / login / unknown
        self.page_url = ""         # 最近一次实际落地地址
        self.nav_log = []          # 进入评价页的尝试记录（诊断用）
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
            "stars_source": self.stars_source,
            "star_note": self.star_note,
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
        # 评价列表的真实数字通常在接口 JSON 里；后台静默收集，不干扰业务请求
        try:
            self.page.on("response", self._on_api_response)
        except Exception:
            pass
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
            if self.page_kind == "login":
                return False
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
            if self.page_kind == "login":
                return
            self.online = True
            await self.save_login_state()
            await self.refresh_stars()
            self.online = True
            self._last_heartbeat = time.time()
            self._last_star_check = time.time()
            self.set("在线", "登录态已恢复，浏览器保持隐藏")
        except Exception as e:
            self.set("掉线，请重新扫码", str(e)[:160], online=False)

    async def _page_kind(self):
        """读当前页面类型：评价页 / 商品列表 / 登录页，以及侧栏真实入口。"""
        if not self.page:
            return {}
        try:
            return await self.page.evaluate(PAGE_KIND_JS)
        except Exception as e:
            return {"error": str(e)[:120]}

    async def _enter_review_page(self):
        """确保浏览器真的停在评价管理页。

        旧地址 /goods/evaluate/list 已被改版重定向到「商品列表」，那边的
        「共有 7 条」是 7 个在售商品。这里按候选地址逐个尝试并校验落地页，
        还不行就从侧栏真实 href 再跳。返回 bool，不抛异常，避免把「页面
        没进去」误判成「店铺掉线」。
        """
        if not self.page:
            return False
        kind = await self._page_kind()
        self.page_url = kind.get("url", "")
        low = self.page_url.lower()
        if "login" in low or "passport" in low:
            self.page_kind = "login"
            self.online = False
            self.set("掉线，请重新扫码", "登录态已失效")
            return False
        self.page_kind = ("review" if kind.get("isReview") else
                          "goods_list" if kind.get("goodsList") else "unknown")
        if kind.get("isReview"):
            self.nav_log = ["已在评价管理页"]
            try:
                await self._auto_name_if_needed()
            except Exception:
                pass
            return True

        urls = list(getattr(self.core, "REVIEW_PAGE_URLS",
                            [getattr(self.core, "REVIEW_MANAGE_URL", "")]))
        for lk in kind.get("links") or []:
            h = str(lk.get("href") or "")
            if h.startswith("//"):
                h = "https:" + h
            elif h.startswith("/"):
                h = "https://mms.pinduoduo.com" + h
            if h.startswith("http") and h not in urls:
                urls.append(h)

        nav_log = []
        for url in urls[:8]:
            if not url:
                continue
            try:
                await self.page.goto(url, wait_until="domcontentloaded",
                                     timeout=30000)
                try:
                    await self.page.wait_for_load_state("networkidle",
                                                        timeout=10000)
                except Exception:
                    pass
                # 拼多多是 SPA，落地后异步渲染，1.2s 常常还没切完页面
                await asyncio.sleep(2.2)
            except Exception as e:
                nav_log.append("%s→异常:%s" % (url.rsplit("/", 1)[-1][:36],
                                               str(e)[:36]))
                continue
            k2 = await self._page_kind()
            cur = k2.get("url", "")
            nav_log.append("%s→%s" % (url.rsplit("/", 1)[-1][:36],
                                      cur.rsplit("/", 1)[-1][:36]))
            if "login" in cur.lower() or "passport" in cur.lower():
                self.page_kind, self.page_url, self.nav_log = "login", cur, nav_log
                self.online = False
                self.set("掉线，请重新扫码", "登录态已失效")
                return False
            if k2.get("isReview"):
                self.page_kind, self.page_url, self.nav_log = "review", cur, nav_log
                try:
                    await self._auto_name_if_needed()
                except Exception:
                    pass
                return True

        # 侧栏真实入口兜底：新版后台的侧栏菜单多是 div + JS 路由，a[href]
        # 抓不到，直接按可见文字点击「评价管理 / 评价举报」最可靠。
        for label in ("评价管理", "评价举报"):
            try:
                loc = self.page.get_by_text(label, exact=True)
                n = await loc.count()
                for i in range(min(3, n)):
                    item = loc.nth(i)
                    if not await item.is_visible():
                        continue
                    await item.click(timeout=5000)
                    await asyncio.sleep(2.2)
                    k3 = await self._page_kind()
                    cur3 = k3.get("url", "")
                    nav_log.append("点击%s→%s" %
                                   (label, cur3.rsplit("/", 1)[-1][:36]))
                    if "login" in cur3.lower() or "passport" in cur3.lower():
                        self.page_kind, self.page_url, self.nav_log = "login", cur3, nav_log
                        self.online = False
                        self.set("掉线，请重新扫码", "登录态已失效")
                        return False
                    if k3.get("isReview"):
                        self.page_kind, self.page_url, self.nav_log = "review", cur3, nav_log
                        try:
                            await self._auto_name_if_needed()
                        except Exception:
                            pass
                        return True
                    break
            except Exception as e:
                nav_log.append("点击%s异常:%s" % (label, str(e)[:30]))

        final = await self._page_kind()
        self.page_url = final.get("url", "")
        self.page_kind = ("goods_list" if final.get("goodsList") else "unknown")
        self.nav_log = nav_log
        self.set("在线", "未进入评价管理页（当前：%s）" %
                 (final.get("title") or self.page_url.rsplit("/", 1)[-1][:36]))
        return False

    async def heartbeat(self):
        """轻量在线心跳：续期 cookie + 掉线检测，不刷新星级（星级单独按更长的周期查）。"""
        if not self.store.has_login(self.id):
            return
        try:
            if not self.context:
                await self.ensure_context(force=True)
            await self._enter_review_page()
            if self.page_kind == "login":
                return
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
        """一次读取页面快照：分页、星级标签候选、列表指纹。"""
        if not self.page:
            return {}
        try:
            return await self.page.evaluate(STAR_SNAPSHOT_JS)
        except Exception:
            return {}

    # ---------- 评价接口响应：最可信的数字来源 ----------
    def _on_api_response(self, response):
        """同步回调里只做筛选，读响应体交给后台任务，绝不阻塞页面。"""
        try:
            url = response.url or ""
            low = url.lower()
            # 所有 XHR 地址都留档：真实接口名常常不含 evaluate/comment 等关键词
            if "/api/" in low or any(k in low for k in _API_KEYWORDS):
                if url not in self.api_urls:
                    self.api_urls.append(url[:300])
                    del self.api_urls[:-60]
            if not any(k in low for k in _API_KEYWORDS):
                return
            ct = ""
            try:
                ct = (response.headers or {}).get("content-type", "")
            except Exception:
                pass
            if "json" not in ct.lower() or getattr(response, "status", 200) >= 400:
                return
            asyncio.ensure_future(self._absorb_response(response, url))
        except Exception:
            pass

    async def _absorb_response(self, response, url):
        try:
            raw = await response.text()
            if not raw or len(raw) > 400000:
                return
            data = json.loads(raw)
        except Exception:
            return
        star_map, totals = parse_star_payload(data)
        self.api_hints.append({
            "ts": time.time(), "url": url[:200],
            "starMap": star_map or {},
            "totals": totals,
            "rawHead": re.sub(r"\d{8,}", "<NUM>", raw[:300]),
        })
        del self.api_hints[:-16]

    def _recent_star_map(self, max_age=90, since=0):
        cut = time.time() - max_age
        for h in reversed(self.api_hints[since:]):
            sm = h.get("starMap") or {}
            if h.get("ts", 0) >= cut and all(k in sm for k in (1, 2, 3)):
                return {k: max(0, int(sm[k])) for k in (1, 2, 3)}
        return None

    def _totals_since(self, mark):
        out = []
        for h in self.api_hints[mark:]:
            out.extend(h.get("totals") or [])
        return out

    def _apply_stars(self, values, source, ok, note=""):
        # 不可靠时连内部值也清零，避免顶部统计卡把假数字加总（7+7+7=21）
        if not ok:
            values = {1: 0, 2: 0, 3: 0}
        for k in (1, 2, 3):
            self.stars[k] = max(0, int(values.get(k, 0) or 0))
        self.stars_ok = bool(ok)
        self.stars_source = source if ok else "none"
        self.star_note = str(note)[:220]
        self.last_check = time.strftime("%H:%M:%S")
        label = {"api": "评价接口", "tab": "星级标签",
                 "cards": "逐页统计评价卡片", "pager": "筛选分页"}.get(source, source)
        self.set(message=("1/2/3 星数量已更新（来源：%s）" % label) if ok else
                 "未读到可信的评价数量，诊断已本地留档并自动回传；可点「刷新数量」重试")
        self.engine.publish(self)

    async def count_stars_by_cards(self, max_pages=12, max_seconds=55):
        """逐页统计评价卡片的星级，完全不依赖星级筛选控件。

        举报流程本身就用「给出了 N 星」/「用户评价分：★★★★★」判定星级，
        这里复用同一套语义，只读不点，不触发筛选风控。返回 None 表示一条都没读到。
        """
        r = self.reporter
        if not r or not self.page:
            return None
        deadline = time.time() + max_seconds
        counts, samples = {1: 0, 2: 0, 3: 0}, []
        scanned, pages, total_rows = 0, 0, None
        seen_pages = set()
        for _ in range(max_pages):
            if time.time() > deadline:
                break
            snap = await self._star_snapshot()
            sig = snap.get("listSig")
            if sig and sig in seen_pages:
                break                      # 没翻成功，别把同一页重复计一遍
            if sig:
                seen_pages.add(sig)
            try:
                js = await self.page.evaluate(STAR_CARDS_JS)
            except Exception:
                js = {}
            c = js.get("counts") or {}
            page_n = 0
            for k in (1, 2, 3, 4, 5):
                try:
                    v = int(c.get(str(k), c.get(k, 0)) or 0)
                except (TypeError, ValueError):
                    v = 0
                page_n += v
                if k in counts:
                    counts[k] += v
            for s in (js.get("samples") or [])[:3]:
                if len(samples) < 6:
                    samples.append(s)
            if page_n == 0:
                # 兜底：核心脚本的精确卡片识别（以「举报」链接为锚点）
                try:
                    cards = await r._collect_review_cards()
                except Exception:
                    cards = []
                for item in (cards or []):
                    page_n += 1
                    try:
                        star = r._detect_card_star(str(item.get("text") or ""))
                    except Exception:
                        star = None
                    if star in counts:
                        counts[star] += 1
            if page_n == 0:
                break
            scanned += page_n
            pages += 1
            try:
                ps = await r._pager_state()
            except Exception:
                ps = {}
            if isinstance(ps.get("totalRows"), int) and ps["totalRows"] > 0:
                total_rows = ps["totalRows"]
            if total_rows and scanned >= total_rows:
                break
            if ps.get("found") and ps.get("nextDisabled"):
                break
            try:
                moved = await r.go_to_next_page()
            except Exception:
                moved = False
            if not moved:
                break
            await asyncio.sleep(0.35)
        if scanned == 0:
            return None
        coverage = (min(1.0, float(scanned) / total_rows)
                    if total_rows else (1.0 if pages == 1 else None))
        return {"counts": counts, "scanned": scanned, "pages": pages,
                "totalRows": total_rows, "coverage": coverage,
                "samples": samples}

    async def _build_diag_payload(self, reason=""):
        """采集真实页面素材。长数字一律打码，不含密码、密钥、完整订单号。"""
        try:
            page_info = await self.page.evaluate(STAR_DIAG_JS) if self.page else {}
        except Exception as e:
            page_info = {"error": str(e)[:200]}
        try:
            card_probe = await self.page.evaluate(STAR_CARDS_JS) if self.page else {}
        except Exception:
            card_probe = {}
        try:
            import updater
            ver = updater.version_info()
        except Exception:
            ver = {}
        return {
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shop": self.name, "reason": reason,
            "appVersion": ver.get("version"), "appLabel": ver.get("label"),
            "currentStars": {str(k): self.stars.get(k) for k in (1, 2, 3)},
            "starsOk": self.stars_ok, "starsSource": self.stars_source,
            "starNote": self.star_note,
            "snapshot": await self._star_snapshot(),
            "pageKind": getattr(self, "page_kind", ""),
            "pageUrl": getattr(self, "page_url", ""),
            "navLog": getattr(self, "nav_log", []),
            "page": page_info, "cardProbe": card_probe,
            "apiUrls": self.api_urls[-30:], "apiHints": self.api_hints[-10:],
        }

    async def _save_diag_local(self, reason=""):
        """诊断同时落到店铺目录：离线或回传失败时也能把文件发出来排查。"""
        payload = await self._build_diag_payload(reason)
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]", "_", self.name)[:24] or "shop"
        fname = "星级诊断_%s_%s.json" % (safe, time.strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(self.store.shop_dir(self.id), fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    async def upload_star_diag(self, reason="", force=False):
        """把诊断自动回传到云端，用户不用再手动找文件、传文件。"""
        if not force and time.time() - self._diag_uploaded_at < 1800:
            return False
        try:
            import base64
            import zlib
            from .license import LicenseClient
            payload = await self._build_diag_payload(reason)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            packed = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
            res = LicenseClient().call(
                "client_diag",
                {"shop": self.name, "reason": reason, "packed": packed,
                 "size": len(raw)}, timeout=20)
        except Exception as e:
            try:
                path = await self._save_diag_local(reason or "诊断回传异常")
                self.set(message="诊断回传异常（%s），已保存到本地：%s" %
                                 (str(e)[:50], os.path.basename(path)))
            except Exception:
                self.set(message="诊断回传异常：%s" % str(e)[:100])
            return False
        self._diag_uploaded_at = time.time()
        if res.get("ok"):
            self.set(message="页面诊断已自动回传（编号 %s），无需手动发文件" %
                             str(res.get("diagId", ""))[:16])
        else:
            try:
                path = await self._save_diag_local(reason or "诊断回传失败")
                self.set(message="诊断回传失败（%s），已保存到本地：%s" %
                                 (str(res.get("msg", ""))[:50], os.path.basename(path)))
            except Exception:
                self.set(message="诊断回传失败：%s" % str(res.get("msg", ""))[:90])
        return bool(res.get("ok"))

    async def refresh_stars(self):
        """读取 1/2/3 星数量，四级来源逐级降级。

          1. 评价接口 JSON 里的星级统计（最准）
          2. 页面星级标签自带数量
          3. 逐页统计评价卡片星级（不依赖筛选控件，最通用）
          4. 逐个筛选后读分页总数
        只有「三次读数相同且列表从未变化」才判定为旧数据；其余能读到就显示，
        并把来源写进状态栏。全部失败时自动回传页面诊断，便于按真实 DOM 适配。
        """
        if not self.reporter or not self.page:
            return
        r = self.reporter
        self.set(message="正在读取 1/2/3 星数量…")

        # 页面守卫：旧地址会被重定向到「商品列表」，那边的「共有 7 条」是
        # 7 个在售商品。不确认在评价管理页，绝不读分页、绝不写任何数字。
        try:
            await self._enter_review_page()
        except Exception as e:
            self.page_kind = "unknown"
            self.set(message="进入评价页异常：%s" % str(e)[:80])
        if getattr(self, "page_kind", "") != "review":
            cn = {"goods_list": "商品列表页", "login": "登录页"}.get(
                self.page_kind, "其它页面")
            tail = (getattr(self, "page_url", "") or "").rsplit("/", 1)[-1][:36]
            nav = " / ".join((getattr(self, "nav_log", []) or [])[:5])
            note = "当前停在%s（%s），未能进入评价管理页，数量未读取%s" % (
                cn, tail, ("；导航：" + nav) if nav else "")
            self._apply_stars({}, "none", False, note)
            try:
                await self._save_diag_local(reason="页面守卫：" + note)
            except Exception:
                pass
            await self.upload_star_diag(reason="页面守卫：" + note, force=True)
            return

        snap = await self._star_snapshot()
        for _ in range(4):
            if self._recent_star_map():
                break
            await asyncio.sleep(0.5)

        # 1) 接口直接给出各星级数量
        sm = self._recent_star_map()
        if sm:
            self._apply_stars(sm, "api", True, "来自评价接口统计")
            return

        # 2) 页面星级标签自带数量
        tabs = pick_tab_counts(snap.get("tabRaw"))
        if tabs:
            self._apply_stars(tabs, "tab", True, "来自页面星级标签")
            return

        # 3) 逐页统计评价卡片：星级筛选控件认不出来也能拿到数字
        try:
            card = await self.count_stars_by_cards()
        except Exception as e:
            card = None
            self.set(message="逐页统计异常：%s" % str(e)[:60])
        if card and card.get("scanned", 0) > 0:
            cov = card.get("coverage")
            note = "逐页统计 %d 条评价（%d 页）" % (card["scanned"], card["pages"])
            if isinstance(cov, float) and cov < 0.8:
                note += "，约覆盖 %d%%（分页共 %s 条）" % (
                    int(cov * 100), card.get("totalRows"))
            if card.get("samples"):
                note += "；样例：" + "，".join(
                    "%s星" % s.get("star") for s in card["samples"][:3])
            self._apply_stars(card["counts"], "cards", True, note)
            return

        # 4) 逐个筛选 + 分页总数。筛选状态未确认不再一票否决，
        #    改看「列表指纹是否真的变了」，避免把能读到的数据也否掉。
        vals, notes, missing = {}, [], 0
        changed_any = False
        for star in (1, 2, 3):
            before = await self._star_snapshot()
            mark = len(self.api_hints)
            verified = False
            try:
                await r.filter_by_star(star)
                verified = bool(getattr(r, "_star_filter_verified", False))
            except Exception as e:
                notes.append("%s星筛选异常:%s" % (star, str(e)[:50]))
            sm2 = self._recent_star_map(max_age=30, since=mark)
            if sm2:
                self._apply_stars(sm2, "api", True, "筛选后来自评价接口统计")
                return
            try:
                ps = await r._pager_state()
            except Exception:
                ps = {}
            after = await self._star_snapshot()
            n = ps.get("totalRows")
            if n is None:
                n = after.get("total")
            if n is None:
                tts = self._totals_since(mark)
                n = max(tts) if tts else None
            changed = bool(before.get("listSig") and after.get("listSig") and
                           before.get("listSig") != after.get("listSig"))
            changed_any = changed_any or changed
            if n is None:
                missing += 1
            vals[star] = max(0, int(n or 0))
            notes.append("%s星=%s%s" % (
                star, n, "" if (verified or changed) else "(筛选未确认)"))
            self.engine.publish(self)
            await asyncio.sleep(0.4)

        same = len(set(vals.values())) == 1
        if same and not changed_any:
            self._apply_stars(vals, "none", False,
                              "三次读数均为 %s 且列表未变化（星级筛选控件未识别），"
                              "判断为同一份「全部评价」数据" % list(vals.values())[0])
        elif missing == 0 and (changed_any or len(set(vals.values())) > 1):
            self._apply_stars(vals, "pager", True, "；".join(notes))
        else:
            self._apply_stars(vals, "none", False, "；".join(notes))

        if not self.stars_ok:
            # 不打断主流程：失败时后台自动回传一次真实页面结构
            try:
                await self.upload_star_diag(
                    reason="星级数量读取失败：" + (self.star_note or "未知"))
            except Exception:
                pass

    async def export_star_diag(self):
        """本地留档 + 自动回传云端：用户不必再手动找文件、发文件。"""
        payload = await self._build_diag_payload("用户手动导出")
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]", "_", self.name)[:24] or "shop"
        fname = "星级诊断_%s_%s.json" % (safe, time.strftime("%Y%m%d_%H%M%S"))
        path = ""
        try:
            path = os.path.join(self.store.shop_dir(self.id), fname)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.set(message="本地诊断保存失败：%s" % str(e)[:120])
        ok = await self.upload_star_diag(reason="用户手动导出", force=True)
        if path:
            self.set(message="诊断已保存并%s回传：%s（右键「打开店铺目录」可找到）" %
                             ("成功" if ok else "未能", fname))
        return path

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
