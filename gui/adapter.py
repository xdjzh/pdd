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

# 关闭评价页上的遮挡浮层（「新消息」「评价有礼」等），它们会挡住星级筛选
# 和分页，导致逐页统计只扫到 1 条、截图也看不清。只点明确的关闭按钮。
DISMISS_OVERLAY_JS = r"""
() => {
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  let closed = 0;
  // 1) 消息面板右上角的圆形关闭按钮：常见是 title/aria-label 含「关闭」或
  //    位于「新消息」浮层内、文本为空的 svg/button。只点可见且无文字的。
  const panels = [];
  for (const el of document.querySelectorAll('div,section,aside')) {
    if (el.offsetParent === null) continue;
    const t = clean(el.innerText || '');
    if (/^新消息|消息\s*\(|通知/.test(t) && t.length < 300) panels.push(el);
  }
  for (const p of panels) {
    const btn = p.querySelector('[class*="close"], [class*="Close"], [aria-label*="关闭"], button');
    if (btn && btn.offsetParent !== null) { try { btn.click(); closed++; } catch (e) {} }
  }
  // 2) 通用浮层关闭：带 close 类、无文字、可见的小图标
  for (const el of document.querySelectorAll('[class*="close"],[class*="Close"],[data-testid*="close"]')) {
    if (el.offsetParent === null) continue;
    const t = clean(el.textContent || '');
    if (t.length > 2) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 40 || r.height > 40 || r.width < 8 || r.height < 8) continue;
    try { el.click(); closed++; } catch (e) {}
  }
  return {closed: closed};
}
"""

# 星级筛选标签：诊断证实评价管理页有「用户评价得分 1星 2星 3星 4星 5星」，
# 叶节点文本精确为「N星」，父级 class 含 evaluation_search_*。这里标记它们，
# 并返回当前分页总数（PGT_totalText「共有 N 条」），供「点星级→读分页」使用。
STAR_FILTER_READ_JS = r"""
() => {
  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const vis = el => { try { return el && el.offsetParent !== null; } catch (e) { return false; } };
  document.querySelectorAll('[data-pdd-sf]').forEach(e => e.removeAttribute('data-pdd-sf'));
  const tags = [];
  const cn = {'一':1,'二':2,'三':3,'四':4,'五':5};
  for (const el of document.querySelectorAll('span,div,a,button,li,label,em,b,strong')) {
    if (!vis(el) || el.children.length > 1) continue;
    const t = clean(el.textContent || '');
    if (!t || t.length > 6) continue;
    let s = null;
    const m = t.match(/^([1-5])星$/);
    if (m) s = parseInt(m[1], 10);
    else { const m2 = t.match(/^([一二三四五])星$/); if (m2) s = cn[m2[1]]; }
    if (!s || s < 1 || s > 5) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 6) continue;
    // 只要叶子文本恰好是「N星」，基本就是星级筛选（评价卡片里是「给出了N星」）
    const mark = 's' + s + '_' + tags.length;
    el.setAttribute('data-pdd-sf', mark);
    const cls = ((el.className || '') + ' ' +
                 (el.parentElement ? el.parentElement.className : '')).toString().toLowerCase();
    const active = /active|selected|checked|current|on\b/.test(cls) ||
                   el.getAttribute('aria-checked') === 'true' ||
                   el.getAttribute('aria-selected') === 'true';
    tags.push({mark: mark, star: s, text: t, active: active,
               x: Math.round(r.x), y: Math.round(r.y), cls: cls.slice(0, 90)});
  }
  // 去重：同一星级只留第一个（最靠上的通常是筛选区）
  const byStar = {};
  for (const tag of tags) if (!byStar[tag.star]) byStar[tag.star] = tag;
  // 分页总数
  let total = null, totalText = '';
  for (const ul of document.querySelectorAll('ul[data-testid="beast-core-pagination"],ul[class*="pagination"]')) {
    const tEl = ul.querySelector('li[class*="PGT_totalText_"]');
    if (tEl) totalText = clean(tEl.innerText || tEl.textContent);
    if (!totalText) {
      for (const li of ul.querySelectorAll('li')) {
        const t = clean(li.innerText || li.textContent);
        if (/共有?\s*[0-9]+\s*条/.test(t)) { totalText = t; break; }
      }
    }
    const tm = totalText.match(/([0-9]+)\s*条/);
    if (tm) { total = parseInt(tm[1], 10); break; }
  }
  if (total === null) {
    const m = (document.body.innerText || '').match(/共(?:查询到|有)?\s*([0-9,]+)\s*条/);
    if (m) total = parseInt(m[1].replace(/,/g, ''), 10);
  }
  // 列表指纹：确认筛选后列表真的变了
  const host = document.querySelector('[class*="table"],[class*="Table"],tbody') || document.body;
  const listText = clean((host.innerText || '').slice(0, 1200));
  let h = 5381;
  for (let i = 0; i < listText.length; i++) h = ((h << 5) + h + listText.charCodeAt(i)) | 0;
  return {tags: Object.values(byStar), total: total, totalText: totalText,
          listSig: (h >>> 0).toString(16)};
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
  // 拼多多新版把「评价管理」拆出「互动动态」子页（/goods/evalution/dynamic），
  // 那页只有「近90天+查询+回复+隐藏评论」，既没有星级筛选也没有「举报」按钮，
  // 分页里的「共有 N 条」是互动留言数，绝不是评价数。必须排除，否则程序
  // 误当评价页，读出 18 条互动留言当评价，再把它当成「全部评价」基准去校验。
  const interactDynamic = /evalution\/dynamic|interact.*dynamic|互动动态/.test(u) ||
                          /互动动态/.test(body.slice(0, 800));
  const reviewUrl = /evaluat|comment|review/i.test(u) && !goodsList && !interactDynamic;
  const starTextCount = (body.match(/给出了\s*[1-5]\s*星|用户评价分/g) || []).length;
  const reportBtnCount = (body.match(/举报/g) || []).length;
  // 商品列表页绝不会有「给出了 N 星」的评价卡片；若两类评价特征同时出现，
  // 即使 SPA 没改 URL，也要认定已经进入评价页。
  const reviewContent = starTextCount > 0 && reportBtnCount >= 3;
  return {
    url: u, title: t, goodsList: goodsList, interactDynamic: interactDynamic,
    reviewUrl: reviewUrl,
    isReview: ((!goodsList && !interactDynamic &&
                (reviewUrl || starTextCount > 0 ||
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
_STAR_FIELD_RE = re.compile(
    r"^(star|starlevel|star_level|starlevel|score|level|rating|grade|starnum|"
    r"starlevelnum|evaluationlevel|evaluationscore|goodsstar|goodsstarlevel)$", re.I)
_COUNT_FIELD_RE = re.compile(r"^(count|num|cnt|total|totalcount|total_num|totalnum|"
                             r"totalsize|totalrows|reviewnum|commentnum|evaluatenum|"
                             r"amount|quantity|numcount)$", re.I)
_TOTAL_FIELD_RE = re.compile(r"^(total|totalcount|total_count|totalnum|total_num|"
                             r"totalsize|total_size|totalrows|cnt)$", re.I)
# 中文/带数字的星级键名：1星 / 一星 / star1Count / oneStarNum 等
_STAR_KEY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_STAR_KEY_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _star_key_to_int(k):
    """把键名里的星级解析成 1-5；解析不出返回 None。"""
    ks = str(k).strip()
    if ks in ("1", "2", "3", "4", "5"):
        return int(ks)
    m = re.match(r"^([一二三四五one|two|three|four|five]+)\s*星?", ks, re.I)
    if m:
        for ch in m.group(1).lower():
            if ch in _STAR_KEY_CN:
                return _STAR_KEY_CN[ch]
        for w, n in _STAR_KEY_WORD.items():
            if w in m.group(1).lower():
                return n
    m = re.search(r"star\s*([1-5])", ks, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"([1-5])\s*star", ks, re.I)
    if m:
        return int(m.group(1))
    return None


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
        self.stars = {1: 0, 2: 0, 3: 0}       # 未举报数（已扣除台账里的已举报）
        self.stars_total = {1: 0, 2: 0, 3: 0}  # 平台原始全量数（诊断用）
        self.stars_reported = {1: 0, 2: 0, 3: 0}  # 本次扣除的已举报数
        self.stars_ok = False   # False = 数量未确认，界面显示「—」而不是假数字
        self.stars_source = ""  # api / tab / pager / none，便于排查
        self.star_note = ""
        self.api_hints = []     # 最近若干条评价接口响应摘要
        self.api_urls = []      # 页面实际发过的 XHR 地址（诊断用）
        self.api_call_log = []  # count_stars_by_api 执行日志（诊断用，避免盲试）
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
            "stars_total": dict(self.stars_total),
            "stars_reported": dict(self.stars_reported),
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

    @staticmethod
    def _mask_deep(o, limit=None):
        """递归脱敏：长数字串（订单号/手机号）打码，字符串截断，控制体积。"""
        import re as _re
        def mask_str(s):
            s = _re.sub(r"\d{8,}", "<NUM>", s)
            s = _re.sub(r"([?&](?:token|access_token|sign|anti[^=]*)=)[^&\s]{8,}",
                        r"\1<TOKEN>", s, flags=_re.I)
            return s[:2000]
        if isinstance(o, str):
            return mask_str(o)
        if isinstance(o, (int, float, bool)) or o is None:
            return o
        if isinstance(o, list):
            return [ShopSession._mask_deep(x) for x in o[:60]]
        if isinstance(o, dict):
            out = {}
            for i, (k, v) in enumerate(o.items()):
                if limit and i >= limit:
                    out["..."] = "(截断)"
                    break
                out[str(k)] = ShopSession._mask_deep(v)
            return out
        return str(o)[:500]

    async def _absorb_response(self, response, url):
        try:
            raw = await response.text()
            if not raw or len(raw) > 400000:
                return
            data = json.loads(raw)
        except Exception:
            return
        star_map, totals = parse_star_payload(data)
        # 请求方法 / post 参数：拼多多星级筛选接口靠它区分星级，诊断必须带上
        method, post_data = "", ""
        try:
            req = response.request
            method = getattr(req, "method", "") or ""
            post_data = getattr(req, "post_data", None) or ""
        except Exception:
            pass
        hint = {
            "ts": time.time(), "url": url[:200],
            "starMap": star_map or {},
            "totals": totals,
            "rawHead": re.sub(r"\d{8,}", "<NUM>", raw[:300]),
        }
        # 关键评价接口：留完整脱敏响应体，用于精确校准星级字段。
        # 只留 /saturn/reviews/ 系列（星级统计/列表），避免诊断包过大。
        low = url.lower()
        if "/saturn/reviews/" in low or "/review/analyze" in low:
            try:
                result = data.get("result") if isinstance(data, dict) else data
                # 只留顶层标量 + 每个 list/dict 的首项，控制诊断包体积
                slim = {}
                if isinstance(result, dict):
                    for k, v in result.items():
                        if isinstance(v, (int, float, bool, str, type(None))):
                            slim[k] = v
                        elif isinstance(v, list) and v:
                            slim[k] = ["(共%d项，首项样本)" % len(v),
                                       self._mask_deep(v[0])]
                        elif isinstance(v, dict):
                            slim[k] = self._mask_deep(v, limit=12)
                else:
                    slim = self._mask_deep(result)
                hint["fullResult"] = slim
                hint["method"] = method
                if post_data and post_data[:1] in "{[":
                    try:
                        hint["postData"] = self._mask_deep(json.loads(post_data))
                    except Exception:
                        hint["postData"] = re.sub(r"\d{8,}", "<NUM>", post_data)[:800]
                elif post_data:
                    hint["postData"] = re.sub(r"\d{8,}", "<NUM>", post_data)[:800]
            except Exception:
                pass
        self.api_hints.append(hint)
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

    def _reported_count_by_star(self):
        """按本店铺台账统计各星级「已举报」数量（举报中 + 举报成功 + 待核对）。

        权威集合是 reported_ids.json：核心脚本只要点了提交就写入 key，
        只有被平台明确拒绝（error_toast / 风控）才不写——正好对应界面要排除的部分。
        report_log.jsonl 每条带 star + key，用于把无星级的台账 key 映射到星级
        （同一条评价星级固定，取最后一条有效记录即可）。

        映射不到的 key 不计入（宁可少扣、不可多扣），避免显示负数或误删未举报。
        """
        result = {1: 0, 2: 0, 3: 0}
        try:
            shop_dir = self.store.shop_dir(self.id)
            ledger = os.path.join(shop_dir, "reported_ids.json")
            logf = os.path.join(shop_dir, "report_log.jsonl")
            if not os.path.isfile(ledger):
                return result
            with open(ledger, "r", encoding="utf-8") as f:
                keys = set(str(x) for x in (json.load(f) or []))
            if not keys:
                return result
            key_star = {}
            if os.path.isfile(logf):
                with open(logf, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        k, s = rec.get("key"), rec.get("star")
                        if k in keys and s in (1, 2, 3):
                            key_star[k] = int(s)
            for k in keys:
                s = key_star.get(k)
                if s in (1, 2, 3):
                    result[s] += 1
        except Exception:
            pass
        return result

    def _apply_stars(self, values, source, ok, note="", skip_ledger=False):
        # ★ 反复刷新保护：读不到可信数时【保留上一次可信读数】，不清零、不覆盖。
        #   旧实现把内部值清零 → 顶部合计闪成 0；再加上多次刷新状态互相踩，
        #   表现为「没动再查数字越来越多/忽大忽小」。只有明确读到可信数才更新。
        if not ok:
            self.stars_ok = False
            self.stars_source = "none"
            self.star_note = str(note)[:220]
            self.last_check = time.strftime("%H:%M:%S")
            keep = sum(self.stars.get(k, 0) for k in (1, 2, 3))
            msg = ("未读到可信的评价数量，已保留上次读数（%s）；诊断已本地留档，"
                   "可点「刷新数量」重试" % (str(keep) if keep else "—"))
            self.set(message=msg)
            self.engine.publish(self)
            return
        raw = {k: max(0, int(values.get(k, 0) or 0)) for k in (1, 2, 3)}
        # 平台返回的是全量评价数（含举报中/举报成功），界面只显示未举报的：
        # 统一在这里按台账扣除，顶部「待处理差评」合计与列表 1/2/3 星都会跟着变。
        # ★ v1.5.1：接口直读(api_reported)已用平台 reportResult 精确排除已举报，
        #   raw 就是未举报数，绝不能再扣本地台账（台账是全店历史累计，口径不一致会把
        #   当前这批差评误扣成 0，且每次刷新扣数变化 → 数字忽大忽小）。
        if skip_ledger:
            rep = {1: 0, 2: 0, 3: 0}
            for k in (1, 2, 3):
                self.stars[k] = raw[k]
        else:
            rep = self._reported_count_by_star() if ok else {1: 0, 2: 0, 3: 0}
            for k in (1, 2, 3):
                self.stars[k] = max(0, raw[k] - rep.get(k, 0))
        self.stars_total = dict(raw)     # 展示口径的原始数（api_reported 时为未举报数）
        self.stars_reported = dict(rep)  # 本次已扣除的已举报数（api_reported 时为0）
        self.stars_ok = bool(ok)
        self.stars_source = source if ok else "none"
        rep_total = sum(rep.values())
        if ok and rep_total:
            note = "%s；平台共%d条，已扣除已举报%d条（举报中/举报成功）" % (
                note, sum(raw.values()), rep_total)
        self.star_note = str(note)[:220]
        self.last_check = time.strftime("%H:%M:%S")
        label = {"api": "评价接口", "api_reported": "平台举报状态", "tab": "星级标签",
                 "cards": "逐页统计评价卡片", "pager": "筛选分页"}.get(source, source)
        if ok:
            msg = "未举报 1/2/3 星已更新（来源：%s）" % label
            if skip_ledger:
                msg += "，已按平台举报状态排除已举报评价"
            elif rep_total:
                msg += "，已扣除已举报 %d 条" % rep_total
        else:
            msg = "未读到可信的评价数量，诊断已本地留档并自动回传；可点「刷新数量」重试"
        self.set(message=msg)
        self.engine.publish(self)

    async def _dismiss_overlays(self):
        """关掉评价页的「新消息」等浮层，避免挡住星级筛选和分页。"""
        if not self.page:
            return 0
        try:
            r = await self.page.evaluate(DISMISS_OVERLAY_JS)
            return int((r or {}).get("closed", 0) or 0)
        except Exception:
            return 0

    async def count_stars_by_filter(self):
        """星级筛选→分页总数：诊断证实评价页有真实星级筛选与分页，这是最可靠的来源。

        ★ v1.4.0 关键修复（累加虚高）：
          拼多多「用户评价得分」的 1/2/3 星是【多选标签】——点 2 星不会自动取消 1 星。
          旧实现逐个 loc.click() 点亮，导致读数变成逐级累加：
              点1星→73(真值)，点2星→139(1星+2星)，点3星→507(1+2+3星合计)
          于是 3 星被虚报成 507（正好等于后台总数），合计 719 > 实际 507。
          修复：改用核心脚本成熟的 _set_only_star(star)（点亮目标 + 取消所有残留
          + 复核"仅目标选中"），并在每颗星读数前后用 _scan_star_filters() 独立复核，
          只有确认"当前恰好只有目标星级选中"才采信分页数。
        """
        if not self.page or not self.reporter:
            return None
        r = self.reporter
        await self._dismiss_overlays()
        try:
            await self.page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        # ★ v1.4.1：读数前先清除上一次残留的星级筛选。
        #   _enter_review_page 若已在评价页会直接返回，不会重载页面，
        #   上一点亮的星级一直残留 → 反复刷新越叠越多（截图的 434/368/2000）。
        #   用核心脚本的 _cancel_current_star_filter（点"全部"+取消激活项）归零。
        if callable(getattr(r, "_cancel_current_star_filter", None)):
            try:
                await r._cancel_current_star_filter()
            except Exception as e:
                notes_reset = "重置星级失败:%s" % str(e)[:40]
            else:
                notes_reset = "已重置星级筛选"
        else:
            notes_reset = "无重置接口"

        try:
            await asyncio.sleep(1.2)
            all_state = await r._scan_star_filters()
            still_active = [x["star"] for x in (all_state or []) if x.get("active")]
        except Exception:
            still_active = []

        # 星级已归零，此刻分页显示的是"全部评价"基准数，先记下来用于合理性校验。
        all_total = None
        try:
            ps0 = await r._pager_state()
            if isinstance(ps0, dict):
                all_total = ps0.get("totalRows")
        except Exception:
            pass

        # 确认核心脚本具备单选能力（老补丁环境若没有则退回旧路径）
        has_set_only = callable(getattr(r, "_set_only_star", None)) and \
                       callable(getattr(r, "_scan_star_filters", None))

        vals, notes = {}, []
        if notes_reset:
            notes.append(notes_reset)
        for star in (1, 2, 3):
            if has_set_only:
                # 用核心脚本的成熟逻辑：点亮目标 + 取消所有残留星级 + 内部3轮复核。
                # 举报主流程一直用它且能正常翻页举报，说明它在本页面有效。
                verified = False
                try:
                    await r._set_only_star(star)
                    verified = bool(getattr(r, "_star_filter_verified", False))
                except Exception as e:
                    notes.append("%s星单选异常:%s" % (star, str(e)[:40]))
                # 外部二次扫描：明确发现"仅目标亮着"也可作为采信依据（双路径，
                # 避免内部 verified 因时序偶发 False 就把能读的数据否掉）。
                active_stars = None
                try:
                    state = await r._scan_star_filters()
                    active_stars = [x["star"] for x in (state or []) if x.get("active")]
                except Exception:
                    pass
                only_target = verified or (active_stars == [star])
            else:
                notes.append("%s星:核心脚本无单选接口，跳过避免累加" % star)
                active_stars = None
                only_target = False

            if not only_target:
                # 无法确认"仅目标选中"，绝不能读——否则又把累加值当单星数
                notes.append("%s星未确认单选(亮:%s,已验证:%s)" % (
                    star, active_stars, verified if has_set_only else "?"))
                vals[star] = None
                continue

            # 确认单选后，等接口刷新列表，轮询分页到稳定（最多 ~4.5s）。
            # 优先分页组件 PGT_totalText 的筛选后条数；读不到则用页面正文
            # 「共查询到 N 条数据」兜底（STAR_FILTER_READ_JS 有该正则）。
            n = None
            last = None
            for _ in range(9):
                await asyncio.sleep(0.5)
                cn = None
                try:
                    ps = await r._pager_state()
                    if isinstance(ps, dict):
                        cn = ps.get("totalRows")
                except Exception:
                    pass
                if cn is None:
                    try:
                        cur = await self.page.evaluate(STAR_FILTER_READ_JS)
                        cn = cur.get("total") if isinstance(cur, dict) else None
                    except Exception:
                        pass
                if cn is not None:
                    n = cn
                if last is not None and cn == last and cn is not None:
                    break
                last = cn
            vals[star] = n
            notes.append("%s星=%s✓单选" % (star, n))

        if not all(isinstance(vals.get(s), int) for s in (1, 2, 3)):
            return None

        # ★ v1.4.1 合理性校验：单星数量必须明显小于"全部评价"基准。
        #   截图里 3星=2000 恰等于全部评价数（诊断"共有 2000 条"），
        #   这是星级没筛上、读到全量的典型特征，绝不能采信。
        if isinstance(all_total, int) and all_total > 0:
            if any(vals.get(s, 0) >= all_total for s in (1, 2, 3)):
                notes.append("合理性校验失败：单星读数>=全部评价%d，判定筛选未生效" % all_total)
                return None
        # 累加特征：3个数严格递增且最大≈另两个之和（误差10%），判为残留累加。
        v = [vals[1], vals[2], vals[3]]
        if v[0] < v[1] < v[2] and v[2] > 0:
            s12 = v[0] + v[1]
            if abs(v[2] - s12) <= max(3, s12 * 0.1):
                notes.append("合理性校验失败：读数 %d/%d/%d 呈累加特征" % tuple(v))
                return None

        return {"counts": {s: vals[s] for s in (1, 2, 3)},
                "distinct": len(set(vals.values())) > 1, "notes": notes}

    @staticmethod
    def _item_star(item):
        """从一条评价里解析星级（1-5）。诊断确认字段是 descScore。"""
        if not isinstance(item, dict):
            return None
        for k in ("descScore", "score", "star", "starLevel", "evaluateScore",
                  "goodsScore", "level"):
            v = item.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and 1 <= int(v) <= 5:
                return int(v)
            if isinstance(v, str) and v.strip().isdigit() and 1 <= int(v) <= 5:
                return int(v)
        return None

    @staticmethod
    def _item_reported(item):
        """用接口自带的 reportResult 判断一条评价是否已举报（平台官方状态）。

        诊断证实 /saturn/reviews/list 每条评价带：
          reportResult = {'desc': '未被举报', 'status': 99, 'clientDesc': '未被举报', ...}
        拼多多启用 hitMallReportContradictoryReviewGray 新版举报逻辑后，该字段准确反映
        「举报中/举报成功/未被举报」。用它比本地台账扣减可靠得多——台账是全店历史累计，
        与"最近2000条"口径不一致，会把当前这批差评误扣成 0。

        返回 True=已举报(举报中/成功)，False=未举报。字段缺失时保守返回 False（当作未
        举报，宁可让用户看到后可举报，也不要漏掉真正待处理的差评）。

        ★ v1.5.5：按用户口径，「已处理」不只是举报中/成功，还包括
        待审核、举报失败、经平台审核不符合举报要求、不计入店铺考核——这些都已
        提交过或平台已处理，不该再算进待处理数。所以除了"未被举报"以外，凡是
        desc 命中这些状态一律视为已处理。
        """
        if not isinstance(item, dict):
            return False
        rr = item.get("reportResult")
        if isinstance(rr, dict):
            desc = str(rr.get("desc") or rr.get("clientDesc") or "")
            st = rr.get("status")
            # 明确的"未被举报"——唯一要计入待处理的
            if "未被举报" in desc or "未举报" in desc:
                return False
            for kw in ("举报中", "待审核", "审核中", "审核通过", "举报成功",
                       "已举报", "举报失败", "不符合", "不计入", "已处理",
                       "处理中", "已生效", "已失效"):
                if kw in desc:
                    return True
            # status: 99=未被举报（诊断样本）；其它码都当作已处理
            if st == 99:
                return False
            if isinstance(st, int) and 1 <= st <= 98:
                return True
        # 兼容顶层字段
        for key in ("reported", "isReported", "hasReported"):
            v = item.get(key)
            if isinstance(v, bool):
                return v
        return False

    async def count_stars_by_api(self, all_total=None):
        """直接调用评价列表接口，分页拉取评价、按 descScore 统计 1/2/3 星。

        ★ 线路演进（每一步都是被真实诊断逼出来的，不要回退）：
          - 接口 `/saturn/reviews/list` 的请求参数只有 pageNo/pageSize/startTime/
            endTime/orderSn，**没有星级筛选字段**（v1.4.2 猜参数全被忽略）。
          - 必须发原生 JSON：context.request 的 form 表单编码接口不认（v1.4.4）。
          - pageSize 私自调大（100）会报 errorCode 2000010「仅支持查看近2000条」（v1.4.5）。
          - 已举报状态用每条评价自带的 `reportResult` 判断（v1.5.1），比本地台账可靠。
          - ★ v1.5.2：诊断证实**页面内 fetch 会永久挂起**（page.evaluate 没有超时，
            反爬 SDK 拦截后 fetch 不 resolve），整轮刷新卡死、诊断里只剩"模板参数"
            一行。所以改为：优先 context.request 发原生 JSON（自带超时、不依赖页面
            JS 执行），失败再退回页面 fetch 并强制 wait_for 超时；分页改成并发，
            2000 条约二三十秒出结果。
          - ★ v1.5.4：诊断证实**单个时间窗只能看 showNum（2000）条**，而近 90 天
            totalNum 常有三五千条，直接翻 2000 条等于按比例缩水（实测 2 星只数出 5，
            实际 13 ≈ 5135/2000×5）。改为**时间切片**：先看最近 2000 条，取这批最老的
            createTime，把 endTime 设成它再往前续拉，直到 total≤showNum 或时间回到
            startTime，按 reviewId 去重合并，从而覆盖近 90 天全量。
        """
        try:
            return await self._count_stars_by_api_impl(all_total)
        except asyncio.CancelledError:
            self._log_api("被取消（CancelledError）")
            raise
        except Exception as e:
            self._log_api("异常中止：%s: %s" % (type(e).__name__, str(e)[:120]))
            return None

    async def _count_stars_by_api_impl(self, all_total=None):
        if not self.page:
            self._log_api("跳过：无 page")
            return None
        tmpl = None
        for h in reversed(self.api_hints):
            u = (h.get("url") or "").lower()
            if "/reviews/list" in u and h.get("postData"):
                tmpl = h.get("postData")
                break
        if not isinstance(tmpl, dict):
            self._log_api("跳过：api_hints 里没有 reviews/list 的 postData 模板")
            return None
        self._log_api("模板参数=%s" % json.dumps(tmpl, ensure_ascii=False)[:200])

        url = "https://mms.pinduoduo.com/saturn/reviews/list"
        notes = []
        timeout_cap = 180.0
        deadline = time.time() + timeout_cap
        # 通道：ctx = context.request（首选，有超时）；page = 页面内 fetch（兜底）
        chan = {"v": "ctx", "page_timeout": 0}

        fetch_js = """
        async (arg) => {
          const r = await fetch(arg.url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
            body: JSON.stringify(arg.payload)
          });
          const txt = await r.text();
          let js = null;
          try { js = JSON.parse(txt); } catch (e) {}
          return {status: r.status, head: txt.slice(0, 160), js: js};
        }
        """

        class _BizError(Exception):
            """接口明确返回的业务错误（参数/上限/权限）。这类错误换通道也没用，直接向上抛。"""

        def _digest(js, head, status):
            """把接口响应统一成 (res, total, show)；碰到上限返回 _limit_reached。"""
            if js is None or not isinstance(js, dict):
                raise RuntimeError("非JSON(%s) %s" % (status, (head or "")[:70]))
            if js.get("success") is False:
                code = js.get("errorCode")
                msg = str(js.get("errorMsg") or "")
                if code == 2000010 or "2000" in msg:
                    return {"_limit_reached": True}, None, None
                raise _BizError("业务错误 %s %s" % (code, msg[:50]))
            res = js.get("result")
            if not isinstance(res, dict):
                raise RuntimeError("无result %s" % (head or "")[:70])
            n = None
            for nk in ("totalNum", "totalRows", "total", "totalCount"):
                if isinstance(res.get(nk), int) and res[nk] >= 0:
                    n = res[nk]
                    break
            return res, n, res.get("showNum")

        async def _post_via_ctx(page_no, size, end_ts=None):
            payload = dict(tmpl)
            payload["pageNo"] = page_no
            payload["pageSize"] = size
            if end_ts:
                payload["endTime"] = int(end_ts)
            resp = await self.context.request.post(
                url, data=json.dumps(payload, ensure_ascii=False),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/plain, */*",
                         "Origin": "https://mms.pinduoduo.com",
                         "Referer": "https://mms.pinduoduo.com/goods/evaluation/index"},
                timeout=15000)
            txt = await resp.text()
            try:
                js = json.loads(txt)
            except Exception:
                js = None
            return _digest(js, txt, resp.status)

        async def _post_via_page(page_no, size, end_ts=None):
            payload = dict(tmpl)
            payload["pageNo"] = page_no
            payload["pageSize"] = size
            if end_ts:
                payload["endTime"] = int(end_ts)
            try:
                out = await asyncio.wait_for(
                    self.page.evaluate(fetch_js, {"url": url, "payload": payload}),
                    timeout=18.0)
            except asyncio.TimeoutError:
                chan["page_timeout"] += 1
                raise RuntimeError("页面fetch超时(18s)")
            if not isinstance(out, dict):
                raise RuntimeError("fetch返回非对象")
            if out.get("status") != 200:
                raise RuntimeError("HTTP %s %s" % (out.get("status"), (out.get("head") or "")[:50]))
            return _digest(out.get("js"), out.get("head"), out.get("status"))

        async def _post(page_no, size, end_ts=None):
            if time.time() > deadline:
                raise RuntimeError("统计超时（%ds）" % int(timeout_cap))
            # ctx 通道单页偶发失败（限流）只让该页进重试；连续 3 次才永久切页面 fetch
            if chan["v"] == "ctx":
                try:
                    out = await _post_via_ctx(page_no, size, end_ts)
                    chan["ctx_fail"] = 0
                    return out
                except asyncio.CancelledError:
                    raise
                except _BizError:
                    raise          # 业务错误与通道无关，不浪费一次页面请求
                except Exception as e:
                    chan["ctx_fail"] = int(chan.get("ctx_fail") or 0) + 1
                    if chan["ctx_fail"] >= 3:
                        self._log_api("ctx通道连续失败%d次(%s)，改用页面fetch" % (
                            chan["ctx_fail"], str(e)[:50]))
                        chan["v"] = "page"
                    raise
            if chan["page_timeout"] >= 2:
                raise RuntimeError("页面fetch已连续超时，放弃")
            return await _post_via_page(page_no, size, end_ts)

        # ---- 第 1 页：同时探测可用的 pageSize（50 → 20 → 10）----
        res1, n1, show1 = None, None, None
        page_size, last_err = 10, None
        end_ts0 = int(tmpl.get("endTime") or 0) or None
        start_ts0 = int(tmpl.get("startTime") or 0) or None
        for size_try in (50, 20, 10):
            try:
                r_try, n_try, s_try = await _post(1, size_try, end_ts0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                self._log_api("第1页 pageSize=%d 失败:%s" % (size_try, str(e)[:70]))
                continue
            # 2000010 在探测阶段几乎都是「每页条数太大」，不是真的到查看上限：
            # 换更小的 pageSize 再试，只有 10 条也被拒才放弃。
            if isinstance(r_try, dict) and r_try.get("_limit_reached"):
                self._log_api("第1页 pageSize=%d 被接口拒绝(2000010)，降级重试" % size_try)
                if size_try == 10:
                    notes.append("接口限制：每页条数只能为 10，仍被拒绝")
                    return {"counts": None, "notes": notes}
                continue
            res1, n1, show1, page_size = r_try, n_try, s_try, size_try
            self._log_api("第1页成功 pageSize=%d 通道=%s" % (size_try, chan["v"]))
            break
        if res1 is None:
            notes.append("第1页始终失败：%s" % str(last_err)[:60])
            return {"counts": None, "notes": notes}

        total_rows = n1 if isinstance(n1, int) and n1 > 0 else None
        show_cap = show1 if isinstance(show1, int) and show1 > 0 else None
        # 接口单个时间窗最多只看 showNum（通常 2000）条，而近 90 天往往有几千条：
        # 所以覆盖率要以「全量 total」为分母，靠时间切片把整段时间分批拉完。
        cap_total = total_rows or show_cap or all_total
        self._log_api("总量 total=%s show=%s 每页%d 起始时间=%s 结束时间=%s" % (
            total_rows, show_cap, page_size, start_ts0, end_ts0))

        counts = {1: 0, 2: 0, 3: 0}          # 未举报（界面展示用）
        counts_all = {1: 0, 2: 0, 3: 0}      # 全部（含已举报，诊断对照）
        seen = set()
        scanned = 0
        samples = {}
        errors = []

        def _absorb(res):
            """吸收一页数据，同时返回该页最老的 createTime（给时间切片续拉用）。"""
            nonlocal scanned
            data = res.get("data") or res.get("reviewList") or res.get("list") or []
            if not isinstance(data, list):
                return 0
            oldest = 0
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    ct = int(item.get("createTime")
                             or item.get("commentTime") or 0)
                except Exception:
                    ct = 0
                if ct and (not oldest or ct < oldest):
                    oldest = ct
                rid = item.get("reviewId") or item.get("orderSn") or ""
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                star = self._item_star(item)
                if star in counts:
                    counts_all[star] += 1
                    if not self._item_reported(item):
                        counts[star] += 1
                    if star not in samples:
                        rr = item.get("reportResult")
                        samples[star] = "举报=%s/%s | %s" % (
                            self._item_reported(item),
                            (rr.get("desc") if isinstance(rr, dict) else None),
                            str(item.get("comment") or "")[:30])
                scanned += 1
            return oldest

        # ---- 批内并发拉页（并发 4，温和，避免风控；失败页再串行重试一次）----
        async def _pull_pages(pages_b, end_ts):
            got = {}

            async def _one(pg, sem):
                async with sem:
                    if time.time() > deadline:
                        return
                    try:
                        res, n, show = await _post(pg, page_size, end_ts)
                        if isinstance(res, dict) and not res.get("_limit_reached"):
                            got[pg] = res
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass

            sem = asyncio.Semaphore(4)
            await asyncio.gather(*[_one(pg, sem) for pg in range(2, pages_b + 1)],
                                 return_exceptions=True)
            failed = [pg for pg in range(2, pages_b + 1) if pg not in got]
            if failed:
                self._log_api("本批重试失败页 %d 个" % len(failed))
                sem2 = asyncio.Semaphore(2)
                await asyncio.gather(*[_one(pg, sem2) for pg in failed],
                                     return_exceptions=True)
                failed = [pg for pg in range(2, pages_b + 1) if pg not in got]
            return got, failed

        # ---- 时间切片：每批只看「end 往前最近 showNum 条」，再以最老时间续拉 ----
        batch_log = []
        cur_end = end_ts0
        total_pages = 0
        batch = 0
        MAX_BATCH = 10
        seen_ends = set()
        while True:
            batch += 1
            if batch == 1:
                res_b, total_b, show_b = res1, n1, show1
            else:
                try:
                    res_b, total_b, show_b = await _post(1, page_size, cur_end)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    notes.append("第%d批探测失败：%s" % (batch, str(e)[:40]))
                    self._log_api("第%d批探测失败:%s" % (batch, str(e)[:60]))
                    break
            if not isinstance(res_b, dict) or res_b.get("_limit_reached"):
                break
            show_b = show_b if isinstance(show_b, int) and show_b > 0 else 2000
            if isinstance(total_b, int) and total_b >= 0:
                cap_b = min(total_b, show_b)
            else:
                cap_b = show_b
            pages_b = max(1, min((int(cap_b) + page_size - 1) // page_size, 200))
            total_pages += pages_b

            oldest = _absorb(res_b)
            if pages_b > 1:
                got, failed = await _pull_pages(pages_b, cur_end)
                for pg in sorted(got):
                    ct = _absorb(got[pg])
                    if ct and (not oldest or ct < oldest):
                        oldest = ct
                if failed:
                    errors.append("第%d批%d页失败" % (batch, len(failed)))
            batch_log.append("批%d end=%s total=%s 拉%d页 累计%d条" % (
                batch, cur_end or "-", total_b, pages_b, scanned))

            # 这一批是否已经把区间内的评价全部拿到
            if not isinstance(total_b, int) or total_b <= show_b:
                break
            if not oldest:
                self._log_api("批%d 取不到最老时间，停止续拉" % batch)
                break
            nxt = oldest - 1
            if start_ts0 and nxt <= start_ts0:
                self._log_api("批%d 已回到起始时间，停止" % batch)
                break
            if nxt in seen_ends or (cur_end and nxt >= cur_end):
                self._log_api("批%d 时间未推进，停止" % batch)
                break
            seen_ends.add(nxt)
            cur_end = nxt
            if batch >= MAX_BATCH:
                self._log_api("达到最大批次 %d，停止" % MAX_BATCH)
                break
            if time.time() > deadline:
                self._log_api("统计时间到（%ds），停止续拉" % int(timeout_cap))
                break

        self._log_api("分批完成 %d 批：%s" % (batch, "；".join(batch_log)))
        for st in (1, 2, 3):
            if st in samples:
                self._log_api("  %d星样本: %s" % (st, samples[st]))

        if scanned == 0:
            self._log_api("结果：scanned=0，放弃")
            notes.append("接口未返回任何评价数据")
            return {"counts": None, "notes": notes}

        cov = (float(len(seen)) / float(cap_total)) if cap_total else None
        enough = (isinstance(cov, float) and cov >= 0.6) or scanned >= 60
        self._log_api("统计完成 scanned=%d cap=%s cov=%s enough=%s 未举报=%s 全部=%s" % (
            scanned, cap_total,
            ("%.0f%%" % (cov * 100)) if isinstance(cov, float) else "?",
            enough, [counts[s] for s in (1, 2, 3)], [counts_all[s] for s in (1, 2, 3)]))
        if not enough:
            notes.append("仅扫 %d 条（总量 %s，覆盖 %s），样本不足不采信" % (
                scanned, cap_total,
                ("%.0f%%" % (cov * 100)) if isinstance(cov, float) else "?"))
            return {"counts": None, "notes": notes}
        if errors:
            notes.append("有部分页请求失败：%s" % "、".join(errors[:3]))
        notes.append("扫 %d 条评价（共 %s 条，覆盖 %s）：未举报1/2/3星=%s，全部=%s" % (
            scanned, cap_total,
            ("%.0f%%" % (cov * 100)) if isinstance(cov, float) else "?",
            [counts[s] for s in (1, 2, 3)], [counts_all[s] for s in (1, 2, 3)]))
        # counts 已是"未举报"数；source=api_reported 让 _apply_stars 跳过台账扣减
        return {"counts": counts, "counts_all": counts_all,
                "source": "api_reported", "notes": notes,
                "pageSize": page_size, "pages": total_pages,
                "batches": batch, "scanned": scanned,
                "channel": chan["v"]}

    def _log_api(self, text):
        self.api_call_log.append("%s %s" % (time.strftime("%H:%M:%S"), text))
        del self.api_call_log[:-40]

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
            "apiCallLog": getattr(self, "api_call_log", []),
        }

    async def _desktop_paths(self, tag):
        """诊断文件固定放桌面，用户一眼就能找到并发出来。"""
        folder = ""
        try:
            folder = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        except Exception:
            folder = os.path.expanduser("~")
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]", "_", self.name)[:20] or "shop"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = "PDD诊断_%s_%s_%s" % (safe, tag, stamp)
        return (os.path.join(folder, base + ".txt"),
                os.path.join(folder, base + "_页面截图.png"))

    async def write_desktop_diag(self, tag="刷新数量", reason=""):
        """把浏览器现场写成桌面文本 + 截图。全程本地，不需要网络、不需要云回传。"""
        txt_path, png_path = await self._desktop_paths(tag)
        try:
            import updater
            ver = updater.version_info()
        except Exception:
            ver = {}
        payload = await self._build_diag_payload(reason)
        page = payload.get("page") or {}
        snap = payload.get("snapshot") or {}
        card = payload.get("cardProbe") or {}
        lines = []
        A = lines.append
        A("=" * 72)
        A("PDD 工作台 · 星级诊断报告")
        A("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        A("版本：%s  内核：%s" % (ver.get("label", "?"), ver.get("version", "?")))
        A("店铺：%s" % self.name)
        A("场景：%s" % tag)
        A("说明：请直接把本文件 + 同名截图发给开发者，不要改动内容。")
        A("=" * 72)
        A("")
        A("【1. 当前读数】")
        A("星级 1/2/3：%s" % payload.get("currentStars"))
        A("是否可信 stars_ok：%s   来源：%s" %
          (payload.get("starsOk"), payload.get("starsSource")))
        A("失败原因 starNote：%s" % payload.get("starNote"))
        A("")
        A("【2. 页面判定与导航】")
        A("page_kind：%s" % payload.get("pageKind"))
        A("page_url ：%s" % payload.get("pageUrl"))
        A("导航日志 navLog：")
        for x in (payload.get("navLog") or []):
            A("   - %s" % x)
        A("")
        A("【3. 浏览器实际落地】")
        A("URL   ：%s" % page.get("url"))
        A("标题  ：%s" % page.get("title"))
        A("正文前 2500 字：")
        A(page.get("bodyHead") or "(空)")
        A("")
        A("【4. 星级相关控件 HTML（最关键）】")
        star_els = page.get("starEls") or []
        if not star_els:
            A("   (没有识别到任何含『星/评分/等级』的控件)")
        for i, el in enumerate(star_els[:30]):
            A("   [%d] 文本=%s  tag=%s  class=%s" %
              (i, el.get("t"), el.get("tag"), el.get("cls")))
            A("        HTML：%s" % el.get("html"))
        A("")
        A("【5. 分页结构与按钮】")
        for i, p in enumerate(page.get("pagers") or []):
            A("   分页[%d]：%s" % (i, p))
        for i, b in enumerate((page.get("buttons") or [])[:40]):
            A("   按钮[%d] 文本=%s class=%s selected=%s checked=%s" %
              (i, b.get("t"), b.get("cls"), b.get("sel"), b.get("chk")))
        A("")
        A("【6. 评价卡片探测】")
        A("scan total=%s  counts=%s" % (card.get("total"), card.get("counts")))
        for s in (card.get("samples") or []):
            A("   样例 %s星：%s" % (s.get("star"), s.get("text")))
        A("")
        A("【7. 页面快照候选】")
        A("分页 totalText=%s total=%s" % (snap.get("totalText"), snap.get("total")))
        A("星级候选 tabRaw=%s" % (snap.get("tabRaw") or [])[:8])
        A("含星文本 starTexts=%s" % (snap.get("starTexts") or [])[:20])
        A("列表指纹 listSig=%s" % snap.get("listSig"))
        A("")
        A("【8. 页面发出的接口（找出评价 API）】")
        for u in (page.get("resources") or [])[:40]:
            A("   %s" % u)
        A("   本会话接口摘要：")
        for h in (payload.get("apiHints") or [])[:8]:
            A("   - %s  starMap=%s totals=%s method=%s" %
              (h.get("url"), h.get("starMap"), h.get("totals"), h.get("method", "")))
            if h.get("postData") is not None:
                A("       postData=%s" % str(h.get("postData"))[:600])
            if h.get("fullResult") is not None:
                A("       fullResult=%s" % str(h.get("fullResult"))[:2500])
        A("")
        A("【8b. 接口直读执行日志（count_stars_by_api）】")
        logs = payload.get("apiCallLog") or []
        if logs:
            for line in logs:
                A("   %s" % line)
        else:
            A("   (空 —— 说明接口直读根本没被调用，或被前置分支拦截)")
        A("")
        A("【9. iframe / 全局数据】")
        A("frames=%s" % (page.get("frames") or []))
        for k, v in (page.get("globals") or {}).items():
            A("   %s = %s" % (k, str(v)[:800]))
        A("")
        A("=" * 72)
        A("截图文件：%s" % png_path)
        A("（若截图不存在，说明当时浏览器窗口未就绪，文本信息仍然有效）")
        A("=" * 72)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        # 截图：隐藏浏览器也能截，失败不影响文本
        try:
            if self.page:
                await self.page.screenshot(path=png_path, full_page=False, timeout=15000)
        except Exception as e:
            png_path = "(截图失败：%s)" % str(e)[:80]
        self.set(message="桌面诊断已生成：%s" % os.path.basename(txt_path))
        return txt_path, png_path

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

        # 2.5) 主动调用评价列表接口按星级筛选（v1.4.2 新增）
        #   新版后台落到「互动动态」子页，没有星级筛选控件，页面读数走不通；
        #   评价数据来自 /saturn/reviews/list，复用它的请求参数逐个星级重放。
        all_total = None
        try:
            for h in reversed(self.api_hints):
                if "/reviews/list" in (h.get("url") or "").lower():
                    for t in (h.get("totals") or []):
                        if isinstance(t, int) and t > 0:
                            all_total = t
                            break
                    if all_total:
                        break
        except Exception:
            pass
        self.set(message="正在从评价接口统计 1/2/3 星"
                         "（按时间分段拉全近 90 天，约需 30~60 秒，请稍候）…")
        try:
            api = await self.count_stars_by_api(all_total=all_total)
        except Exception as e:
            api = None
            self.set(message="接口星级统计异常：%s" % str(e)[:60])
        if api and api.get("counts"):
            note = "平台举报状态精确统计：" + "，".join(api.get("notes") or [])
            # api_reported 来源：counts 已按 reportResult 排除已举报，跳过本地台账扣减
            self._apply_stars(api["counts"], api.get("source") or "api",
                              True, note, skip_ledger=(api.get("source") == "api_reported"))
            return

        # 3) 星级筛选→分页总数（v1.3.8 新增，最可靠）
        #    v1.3.7 诊断证实：页面有真实星级筛选按钮 + 分页读数；之前是被
        #    「逐页统计残缺数据」抢先 return 了，才一直显示 0/0/0。
        try:
            await self._dismiss_overlays()
            filt = await self.count_stars_by_filter()
        except Exception as e:
            filt = None
            self.set(message="星级筛选统计异常：%s" % str(e)[:60])
        if filt and filt.get("counts") and (filt.get("distinct") or any(
                filt["counts"].get(s, 0) > 0 for s in (1, 2, 3))):
            note = "星级筛选分页：" + "，".join(filt.get("notes") or [])
            self._apply_stars(filt["counts"], "pager", True, note)
            return

        # 4) 逐页统计评价卡片：星级筛选控件认不出来也能拿到数字。
        #    必须覆盖足够比例才采信，否则「只扫到 1 条」会误报 0/0/0。
        try:
            card = await self.count_stars_by_cards()
        except Exception as e:
            card = None
            self.set(message="逐页统计异常：%s" % str(e)[:60])
        if card and card.get("scanned", 0) > 0:
            cov = card.get("coverage")
            enough = (isinstance(cov, float) and cov >= 0.6) or \
                     (card.get("totalRows") in (None, 0) and card.get("scanned", 0) >= 10)
            if enough:
                note = "逐页统计 %d 条评价（%d 页）" % (card["scanned"], card["pages"])
                if isinstance(cov, float) and cov < 0.8:
                    note += "，约覆盖 %d%%（分页共 %s 条）" % (
                        int(cov * 100), card.get("totalRows"))
                if card.get("samples"):
                    note += "；样例：" + "，".join(
                        "%s星" % s.get("star") for s in card["samples"][:3])
                self._apply_stars(card["counts"], "cards", True, note)
                return
            # 覆盖不足：留档但不采信，继续尝试下一路
            card_note = "逐页统计仅 %d 条/共 %s 条，覆盖不足不采信" % (
                card.get("scanned"), card.get("totalRows"))
        else:
            card_note = ""

        # 5) 逐个筛选 + 分页总数（核心脚本的路径，最后兜底）
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

        # 失败时把接口通道的真实原因带上，避免只显示误导性的"筛选未识别"
        api_why = ""
        if isinstance(api, dict) and api.get("notes"):
            api_why = "；接口统计未采信：" + "，".join(str(x) for x in api["notes"][:2])
        same = len(set(vals.values())) == 1
        if same and not changed_any:
            self._apply_stars(vals, "none", False,
                              "三次读数均为 %s 且列表未变化（星级筛选控件未识别），"
                              "判断为同一份「全部评价」数据%s" % (
                                  list(vals.values())[0], api_why))
        elif missing == 0 and (changed_any or len(set(vals.values())) > 1):
            self._apply_stars(vals, "pager", True, "；".join(notes))
        else:
            self._apply_stars(vals, "none", False, "；".join(notes) + api_why)

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
