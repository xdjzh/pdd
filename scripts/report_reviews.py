#!/usr/bin/env python3
"""
拼多多商家后台 - 评价管理自动化举报脚本
============================================
功能：登录拼多多商家后台 → 进入评价管理 → 筛选1/2/3星评价 → 逐一举报

使用方式：
    python report_reviews.py

依赖：
    pip install playwright
    （使用系统自带 Chrome/Edge 浏览器，无需额外下载 Chromium）
"""

import asyncio
import base64
import hashlib
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ============================================================
# 配置区 — 按需修改
# ============================================================

# 拼多多商家后台URL
PDD_MMS_URL = "https://mms.pinduoduo.com/"

# 浏览器用户数据目录（保存登录态，下次免登录）
USER_DATA_DIR = str(Path(__file__).parent.parent / "assets" / "browser_data")

# 评价管理页面路径。
# 2026-11 商家后台改版：旧 /goods/evaluate/list 会被重定向到 /goods/goods_list
# （商品列表），程序会把「共有 7 条」的在售商品误读成评价数量。侧栏真实入口
# 是 /goods/evaluation/index。保留多个候选，运行时按落地 URL 校验逐个尝试。
REVIEW_MANAGE_URL = "https://mms.pinduoduo.com/goods/evaluation/index?msfrom=mms_sidenav"
REVIEW_PAGE_URLS = [
    # 侧栏「评价管理」真实链接带 msfrom=mms_sidenav，裸路径在部分店铺会被
    # 重定向回商品列表，所以带参数的放第一位。
    "https://mms.pinduoduo.com/goods/evaluation/index?msfrom=mms_sidenav",
    "https://mms.pinduoduo.com/goods/evaluation/index",
    "https://mms.pinduoduo.com/goods/evaluation/index?sellerRightProtect=1",
    "https://mms.pinduoduo.com/goods/evaluate/list",
]

# 筛选的星级：1星、2星、3星
TARGET_STARS = [1, 2, 3]

# 举报理由（自定义修改）
REPORT_REASON = "无任何跟客服沟通记录，也没有任何售后诉求，订单评价中也没有任何内容评价，恶意差评。"

# ============================================================
# 举报入口（2026-11-16 15:41 起：改回「只走同行恶意差评」单入口）
#   用户实测四级自动降级虽然能提交，但入口类型不符导致申诉成功率低。
#   现在的策略：只用第 1 级「同行恶意差评」；一旦被平台风控置灰 / 拒绝，
#   立即暂停全部任务、桌面弹窗提醒「已到达当日举报上限」，等次日额度恢复再跑。
#   下面 ALL_REPORT_TYPES 保留四级真实 viewid 仅供 --probe 诊断 / 日后需要时启用，
#   默认不会参与提交，也不会降级。
#     reason     : fill = 描述必填；auto = 有描述框才填；none = 不填内容
#     sub_reason : 选中类型后还要再选的二级「举报原因」，空串表示不需要
# ============================================================
ALL_REPORT_TYPES = [
    {"rank": 1, "name": "同行恶意差评",
     "viewid": "bad_peer_reviews",
     "reason": "fill", "text": REPORT_REASON, "sub_reason": ""},
    {"rank": 2, "name": "评价内容异常",
     "viewid": "abnormal_review_content",
     "reason": "auto", "text": REPORT_REASON, "sub_reason": "购买A商品评价B商品"},
    {"rank": 3, "name": "星级与内容矛盾",
     "viewid": "conflict_between_star_ratings_and_content",
     "reason": "none", "text": "", "sub_reason": ""},
    {"rank": 4, "name": "利用评价要挟",
     "viewid": "use_evaluation_to_blackmai",
     "reason": "fill", "text": REPORT_REASON, "sub_reason": ""},
]

# 单入口模式（默认）：只提交「同行恶意差评」，禁止任何降级
SINGLE_ENTRY_ONLY = True
SINGLE_ENTRY_RANK = 1
REPORT_TYPE_PLAN = [p for p in ALL_REPORT_TYPES if p["rank"] == SINGLE_ENTRY_RANK]
# 传 --multi-entry 才重新启用四级自动降级（默认关闭，避免类型不符拉低成功率）
FALLBACK_ON_BLOCKED = False
FALLBACK_ON_SUBMIT_REJECT = False

# 二级「举报原因」的文字变体会在匹配时统一去空格 / 转全角，下面列出常见写法兜底
SUB_REASON_ALIASES = {
    "购买A商品评价B商品": [
        "购买A商品评价B商品", "购买 A 商品评价 B 商品",
        "购买A商品，评价B商品", "购买A商品评价B", "买A发B",
    ]
}

TYPE_VIEWIDS = [p["viewid"] for p in REPORT_TYPE_PLAN]
# 弹窗状态读取始终扫描全部四个 radio（诊断用），但提交只走 TYPE_VIEWIDS
ALL_VIEWIDS = [p["viewid"] for p in ALL_REPORT_TYPES]
TYPE_RADIO_SELECTOR_TPL = 'input[data-tracking-viewid="%s"]'
TYPE_RADIO_SELECTOR = TYPE_RADIO_SELECTOR_TPL % REPORT_TYPE_PLAN[0]["viewid"]
REPORT_TYPE = REPORT_TYPE_PLAN[0]["name"]  # 兼容旧日志字段，表示默认首选入口

# 选不中举报类型时是否仍允许提交。强烈建议 False —— 宁可漏报，不要错报
# 单入口模式下，选不中一律不提交，也不换其它入口
ALLOW_SUBMIT_WITHOUT_TYPE = False

# ============================================================
# 风控 / 当日举报上限（2026-11-16 15:41 用户实测）
#   「同行恶意差评」入口被置灰 / 平台明确拒绝提交 = 当日额度已用完，
#   平台通常次日自动恢复。此时不再尝试其它入口、不再翻页，直接暂停并提醒。
# ============================================================
RISK_STOP_ENABLED = True          # 命中风控信号是否立即暂停
RISK_STOP_CONSECUTIVE = 1         # 连续命中几次才暂停（1 = 一次即停）
SHOW_DESKTOP_ALERT = True         # 是否弹桌面提醒窗口（--no-alert 可关）
ALERT_GIVE_UP_SECONDS = 120       # 弹窗多少秒后自动消失，避免长期挂着

ALERT_TITLE = "已到达当日举报上限"
ALERT_MESSAGE_TPL = (
    "平台风控已限制「同行恶意差评」入口，脚本已暂停。\n\n"
    "本次成功举报 {success} 条，时间 {time}。\n"
    "举报额度一般次日自动恢复，明天重新打开启动器继续即可。\n"
    "已成功的评价已写入去重台账，不会重复举报。"
)

# 判定为「风控 / 当日额度用完」的原因码（来自 _apply_plan / _submit_with_verify）
RISK_WHY_CODES = {
    "disabled_by_risk", "hidden", "absent", "not_checked",
    "error_toast", "submit_rejected", "no_radio",
}
# 页面/弹窗文案里出现这些词，也直接判定为当日上限（比原因码更明确）
RISK_TEXT_PATTERN = (
    r"当日.{0,6}上限|今日.{0,6}上限|当天.{0,6}上限|举报.{0,8}(次数|额度).{0,6}"
    r"(已达|达到|用完|超额|超出)|操作.{0,4}频繁|举报.{0,4}频繁|"
    r"(明日|明天|次日).{0,6}(再|重试|恢复|继续)|风控|已达上限|次数上限|超出限制"
)

# ============================================================
# 真实弹窗 DOM（2026-11-16 由 probe_dialog.html 实测校准）
#   弹窗根：[data-testid="beast-core-modal"]，标题「举报该评价」
#   4 个举报类型 radio：abnormal_review_content / conflict_between_star_ratings_and_content /
#     use_evaluation_to_blackmai / bad_peer_reviews
#   提交按钮 = new_confirm_report_btn，取消 = new_cancel_report_btn
#   分页 li 的 class 含 PGT_pagerItem_
# ============================================================
DIALOG_ROOT_SELECTOR = '[data-testid="beast-core-modal"]'
SUBMIT_BTN_SELECTOR = 'button[data-tracking-viewid="new_confirm_report_btn"]'
CANCEL_BTN_SELECTOR = 'button[data-tracking-viewid="new_cancel_report_btn"]'
PAGER_ITEM_SELECTOR = 'li[class*="PGT_pagerItem_"]'

# 真实分页组件（2026-11-16 由 no_cards_page.html 实测）：
#   <ul data-testid="beast-core-pagination" data-status="beast-core-pagination-10-1">
#     <li class="PGT_totalText_...">共有 381 条</li>
#     <li class="PGT_sizeChanger_...">每页 <input value="10"> 条</li>
#     <li data-testid="beast-core-pagination-prev" class="PGT_disabled_...">
#     <li class=" PGT_pagerItem_... PGT_pagerItemActive_...">1</li> ... 5 ... 39
#     <li data-testid="beast-core-pagination-next" class="PGT_next_...">
#   data-status 末段就是「当前页码」，比数 active class 更可靠
PAGER_UL_SELECTOR = 'ul[data-testid="beast-core-pagination"]'
PAGER_NEXT_SELECTOR = 'li[data-testid="beast-core-pagination-next"]'
PAGER_PREV_SELECTOR = 'li[data-testid="beast-core-pagination-prev"]'
PAGER_ACTIVE_SELECTOR = 'li[class*="PGT_pagerItemActive_"]'
PAGER_TOTAL_SELECTOR = 'li[class*="PGT_totalText_"]'

# 用户 2026-11-16 实测：不填写内容平台会拒绝提交（弹窗不关 / 校验不通过）。
# True  = 描述没填进去就绝不点提交（默认，避免又一次空点 + 污染台账）
# False = 允许不带描述提交（仅用于确认平台改版成「只选类型即可提交」时）
REQUIRE_REASON_FIELD = True

# 选中「同行恶意差评」后，描述输入框可能是 React 条件渲染出来的：
# 轮询等待它出现的最长秒数 / 每轮间隔
REASON_FIELD_WAIT_SECONDS = 5.0
REASON_FIELD_POLL_SECONDS = 0.5

# 提交后只有出现「举报成功」类提示才算确认成功；
# 仅弹窗关闭时记为 submitted_unverified，不冒充成功
STRICT_SUBMIT_VERIFY = True

# 每条操作之间的随机延时区间（秒），模拟人工节奏，避免触发风控
MIN_ACTION_DELAY = 1.5
MAX_ACTION_DELAY = 3.5

# 星级筛选控件找不到 / 状态校验失败时：
#   False = 自动降级为「按评价卡片文字判定星级」，不中止（推荐，绝不会误报 4/5 星）
#   True  = 直接跳过该星级
STAR_FILTER_REQUIRED = False

# 处理每条评价前，用卡片文字（「给出了N星好评/差评」或 ★ 数量）复核星级
VERIFY_STAR_IN_CARD = True

# 每页处理后的等待时间（秒）- 避免操作过快
PAGE_WAIT_SECONDS = 3

# 最大处理页数（0 = 不限制，处理全部）
MAX_PAGES = 0

# 是否启用无头模式（调试时设为False）
HEADLESS = False

# ============================================================
# 运行模式 — 用命令行参数控制，不用反复改代码
#   python report_reviews.py              试运行（默认）：全流程走一遍，不点提交
#   python report_reviews.py --probe     探测模式：只点开第 1 条举报弹窗，导出结构后退出
#   python report_reviews.py --run       正式运行：只走「同行恶意差评」，风控即暂停
#   python report_reviews.py --run --pages 1   正式运行，但每个星级只跑 1 页
#   python report_reviews.py --run --stars 1,2     只处理 1/2 星
#   python report_reviews.py --force      忽略当日上限标记，强制再跑一次
#   python report_reviews.py --clear-limit   清除当日上限标记（次日 / 误判时用）
#   python report_reviews.py --no-alert   不弹桌面提醒（无头 / 远程时用）
#   python report_reviews.py --multi-entry  （不推荐）重新启用四级自动降级
# 默认策略：单入口「同行恶意差评」→ 风控暂停 → 桌面弹窗 → 次日恢复
# ============================================================
_argv = sys.argv[1:]


def _arg_value(flag, default=None):
    if flag in _argv:
        idx = _argv.index(flag)
        if idx + 1 < len(_argv) and not _argv[idx + 1].startswith("--"):
            return _argv[idx + 1]
    return default


PROBE_MODE = "--probe" in _argv
SCAN_MODE = "--scan" in _argv          # 只导出页面真实结构，供校准选择器，不点任何举报
DRY_RUN = ("--run" not in _argv) or PROBE_MODE or SCAN_MODE
HEADLESS = "--headless" in _argv or HEADLESS
FORCE_RUN = "--force" in _argv         # 忽略当日上限标记
CLEAR_LIMIT = "--clear-limit" in _argv
if "--no-alert" in _argv:
    SHOW_DESKTOP_ALERT = False
_parse_pages = _arg_value("--pages")
if _parse_pages is not None:
    MAX_PAGES = int(_parse_pages)
_parse_stars = _arg_value("--stars")
if _parse_stars:
    TARGET_STARS = [int(s) for s in _parse_stars.split(",") if s.strip().isdigit()]
# 从第 N 页开始（前几页已举报完时，直接跳过，不必逐页空跑）
_parse_start = _arg_value("--start-page")
START_PAGE = int(_parse_start) if _parse_start and _parse_start.isdigit() else 1

# 兼容旧参数：只有显式传 --multi-entry / --plan / --rank-from 才会离开单入口模式
MULTI_ENTRY = "--multi-entry" in _argv
if MULTI_ENTRY:
    SINGLE_ENTRY_ONLY = False
    REPORT_TYPE_PLAN = list(ALL_REPORT_TYPES)
    FALLBACK_ON_BLOCKED = True
    FALLBACK_ON_SUBMIT_REJECT = True

# 只用指定等级的举报入口（显式指定，不自动降级）
_parse_plan = _arg_value("--plan")
FORCED_PLAN = int(_parse_plan) if _parse_plan and _parse_plan.isdigit() else 0
if FORCED_PLAN:
    _only = [p for p in ALL_REPORT_TYPES if p["rank"] == FORCED_PLAN]
    if _only:
        REPORT_TYPE_PLAN = _only
        SINGLE_ENTRY_ONLY = True

# 从指定等级开始（显式指定，恢复降级链）
_parse_rank_from = _arg_value("--rank-from")
RANK_FROM = int(_parse_rank_from) if _parse_rank_from and _parse_rank_from.isdigit() else 1
if RANK_FROM > 1:
    _rest = [p for p in ALL_REPORT_TYPES if p["rank"] >= RANK_FROM]
    if _rest:
        REPORT_TYPE_PLAN = _rest
        SINGLE_ENTRY_ONLY = False
        FALLBACK_ON_BLOCKED = True
        FALLBACK_ON_SUBMIT_REJECT = True

if (FORCED_PLAN or RANK_FROM > 1 or MULTI_ENTRY) and REPORT_TYPE_PLAN:
    TYPE_VIEWIDS = [p["viewid"] for p in REPORT_TYPE_PLAN]
    REPORT_TYPE = REPORT_TYPE_PLAN[0]["name"]
    TYPE_RADIO_SELECTOR = TYPE_RADIO_SELECTOR_TPL % REPORT_TYPE_PLAN[0]["viewid"]

# 去重台账与执行日志
LOG_DIR = str(Path(__file__).parent.parent / "assets")
REPORTED_FILE = os.path.join(LOG_DIR, "reported_ids.json")
LOG_FILE = os.path.join(LOG_DIR, "report_log.jsonl")
PROBE_FILE = os.path.join(LOG_DIR, "probe_dialog.html")
DAILY_LIMIT_FILE = os.path.join(LOG_DIR, "daily_limit.json")

# ============================================================
# 页面选择器配置 — 根据实际页面结构调整
# ============================================================

SELECTORS = {
    # 登录相关
    "login_indicator": ".login-form, .login-container, [class*='login']",
    "logged_in_indicator": ".main-container, .home-page, .sidebar, [class*='sidebar']",

    # 评价管理导航
    "nav_evaluate": "text=评价管理",
    "evaluate_link": "a[href*='evaluation'], a[href*='evaluate'], a[href*='comment'], a[href*='review'], :text('评价管理')",

    # 星级筛选
    "star_filter_all": "text=全部",  # 先展开全部筛选
    "star_filter_1": ":text('1星'), :text('一星'), [class*='star']:text('1')",
    "star_filter_2": ":text('2星'), :text('二星'), [class*='star']:text('2')",
    "star_filter_3": ":text('3星'), :text('三星'), [class*='star']:text('3')",
    "star_label_pattern": "{star}星",  # 可调整的标签模式

    # 评价列表
    "review_list": "[class*='evaluate-list'], [class*='comment-list'], [class*='review-list'], table tbody",
    "review_item": "tr, [class*='evaluate-item'], [class*='comment-item'], [class*='review-item']",
    "star_display": "[class*='star'], [class*='rating']",

    # 举报按钮
    "report_btn": ":text('举报'), :text('投诉'), [class*='report'], [class*='complaint']",
    "report_btn_alt": "button:has-text('举报'), a:has-text('举报'), span:has-text('举报')",

    # 举报弹窗
    "report_dialog": "[class*='dialog'], [class*='modal'], [class*='popup']",
    "report_reason_input": "textarea, input[type='text'], [class*='reason']",
    "report_submit_btn": ":text('提交'), :text('确认'), :text('确定'), button[type='submit']",
    "report_success": ":text('成功'), :text('举报成功'), [class*='success']",
    "report_close": ":text('关闭'), :text('取消'), [class*='close']",

    # 分页
    "next_page": ":text('下一页'), .pagination .next, [class*='next']:not([class*='disabled'])",
    "no_more_data": ":text('暂无数据'), :text('没有更多'), :text('无评价')",
}

# ============================================================
# 风控 / 当日举报上限工具（模块级，便于单独测试）
#   判定「当日额度用完」→ 写状态文件 → 弹桌面提醒 → 所有循环立即停
#   状态文件只记录日期，跨天自动失效，第二天直接双击启动器即可继续
# ============================================================


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def is_risk_signal(why="", text=""):
    """是否为「风控 / 当日举报上限」信号（区别于普通页面/填写失败）"""
    w = str(why or "")
    for code in RISK_WHY_CODES:
        if code and (w == code or code in w):
            return True
    if text and re.search(RISK_TEXT_PATTERN, str(text)):
        return True
    return False


def daily_limit_state():
    """读取今日上限标记；不是今天则视为已恢复，返回 None"""
    try:
        with open(DAILY_LIMIT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("date") != _today_str():
        return None
    return d


def mark_daily_limit(reason, success_count, detail=""):
    """记录今日已达上限（原子写，避免半截文件）"""
    rec = {
        "date": _today_str(),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry": REPORT_TYPE,
        "reason": str(reason),
        "detail": str(detail)[:300],
        "success": int(success_count or 0),
        "recover_hint": "次日自动恢复，重新运行启动器即可；--force 可强制再试",
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = DAILY_LIMIT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DAILY_LIMIT_FILE)
        return rec
    except Exception as e:
        print("   ⚠️ 当日上限状态写入失败: %s" % e)
        return rec


def clear_daily_limit():
    """清除今日上限标记（次日 / 误判 / 手动恢复时用）"""
    existed = os.path.exists(DAILY_LIMIT_FILE)
    try:
        if existed:
            os.remove(DAILY_LIMIT_FILE)
    except Exception as e:
        print("   ⚠️ 清除上限标记失败: %s" % e)
        return False
    return existed


def daily_limit_message(success_count=0, when=""):
    return ALERT_MESSAGE_TPL.format(
        success=success_count,
        time=when or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def print_daily_limit_banner(message):
    """终端醒目横幅（弹窗失败时的兜底，也方便回看日志）"""
    lines = ["⏸️  " + ALERT_TITLE, "=" * 58]
    for ln in str(message).split("\n"):
        lines.append("   " + ln)
    lines.append("=" * 58)
    print("\n" + "\n".join(lines) + "\n", flush=True)


def show_desktop_alert(title="", message=""):
    """
    跨平台、非阻塞桌面弹窗。子进程显示，主脚本不会被「确定」按钮卡住。
    macOS: osascript display dialog（2 分钟后自动消失）
    Windows: PowerShell MessageBox（-EncodedCommand 传中文，避免控制台乱码）
    其它: notify-send / zenity 尽力而为
    返回是否成功拉起弹窗；任何失败都降级为终端横幅。
    """
    if not SHOW_DESKTOP_ALERT:
        return False
    title = title or ALERT_TITLE
    message = message or daily_limit_message()
    try:
        system = platform.system()
        if system == "Darwin":
            def _q(s):
                return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
            as_text = " & linefeed & ".join(_q(p) for p in str(message).split("\n"))
            script = (
                'display dialog (%s) with title %s '
                'buttons {"知道了"} default button 1 with icon caution '
                'giving up after %d' % (as_text, _q(title), ALERT_GIVE_UP_SECONDS)
            )
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if system == "Windows":
            def _ps(s):
                return "'" + str(s).replace("'", "''").replace("\n", "`n") + "'"
            ps = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "[System.Windows.Forms.MessageBox]::Show(%s, %s, 0, 48) | Out-Null"
                  % (_ps(message), _ps(title)))
            encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                 "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        # Linux / 其它：尽力而为
        subprocess.Popen(
            ["notify-send", title, message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print("   ⚠️ 桌面弹窗拉起失败（已改用终端提醒）: %s" % e)
        return False


# ============================================================
# 脚本主体
# ============================================================


class PinduoduoReviewReporter:
    """拼多多评价举报自动化"""

    def __init__(self):
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.stats = {
            "total_checked": 0, "total_reported": 0, "errors": 0,
            "skipped_duplicate": 0, "blocked_no_type": 0,
            "fallback_used": 0, "blocked_all_types": 0,
            "platform_rejected": 0, "daily_limit_hits": 0,
        }
        # 四级入口统计：{1: 成功数, 2: ..., 3: ..., 4: ...}
        self.plan_stats = {p["rank"]: 0 for p in REPORT_TYPE_PLAN}
        self.daily_limit_reached = False   # 命中当日上限后，所有循环立即停
        self._risk_streak = 0              # 连续风控信号计数（成功一次清零）
        self._last_submit_risk = False
        self._active_viewid = TYPE_VIEWIDS[0] if TYPE_VIEWIDS else ""
        self._active_plan = REPORT_TYPE_PLAN[0] if REPORT_TYPE_PLAN else {}
        self._current_star = None  # 跟踪当前选中的星级
        self._type_confidence = "none"  # 举报类型选中态判定结果
        self._probe_done = False
        self._page_num = 1              # 当前页码（翻页用）
        self._total_pages = None        # 从「共有 N 条 / 每页 M 条」推算的总页数
        self._star_filter_verified = False  # 星级筛选状态是否已确认达标
        os.makedirs(LOG_DIR, exist_ok=True)
        self.reported_ids = self._load_reported()

    # ---------- 通用工具方法 ----------

    async def scroll_page_down(self, distance: int = 600):
        """向下滚动页面"""
        await self.page.evaluate(f"window.scrollBy(0, {distance})")
        await asyncio.sleep(0.3)

    async def scroll_to_bottom(self):
        """滚动到页面底部（多次滚动以确保懒加载内容出现）"""
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

    async def scroll_element_into_view(self, element):
        """将元素滚动到可视区域"""
        try:
            await element.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
        except Exception:
            pass

    async def safe_click(self, element):
        """安全点击：先滚动到可见区域，再点击"""
        try:
            await self.scroll_element_into_view(element)
            await element.click()
            await asyncio.sleep(0.5)
            return True
        except Exception:
            return False

    async def setup_browser(self):
        """启动浏览器（优先使用系统 Chrome，无需额外下载 Chromium）"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("❌ 未安装 playwright，请运行: pip install playwright")
            raise SystemExit(1)

        self.playwright = await async_playwright().start()

        # 创建用户数据目录以保持登录状态
        os.makedirs(USER_DATA_DIR, exist_ok=True)

        # 优先使用系统 Chrome / Edge，如果都没有则回退到 Playwright 内置 Chromium
        browser = None
        for channel, name in [("chrome", "Chrome"), ("msedge", "Edge"), (None, "Chromium")]:
            try:
                browser = await self.playwright.chromium.launch(
                    channel=channel,
                    headless=HEADLESS,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--force-device-scale-factor=1",
                    ],
                )
                print(f"✅ 使用系统 {name} 浏览器")
                break
            except Exception:
                continue

        if browser is None:
            print("❌ 未找到可用的 Chrome / Edge 浏览器")
            raise SystemExit(1)

        self.browser = browser

        # 尝试加载已保存的登录状态
        storage_file = os.path.join(USER_DATA_DIR, "storage_state.json")
        if os.path.exists(storage_file):
            self.context = await browser.new_context(
                storage_state=storage_file,
                no_viewport=True,
            )
            print("✅ 已加载登录状态（免登录）")
        else:
            self.context = await browser.new_context(
                no_viewport=True,
            )

        # 登录态由 run() 的 finally 显式保存，避免 close 事件里残留未完成任务
        self.page = await self.context.new_page()

        print("✅ 浏览器已启动")

    async def wait_for_manual_login(self):
        """等待用户手动登录"""
        print("\n" + "=" * 60)
        print("🔐 请在浏览器中手动完成登录")
        print("=" * 60)
        print("1. 浏览器窗口已打开")
        print("2. 请使用拼多多商家版APP扫码或手机号登录")
        print("3. 登录成功后，脚本将自动继续...")
        print()

        await self.page.goto(PDD_MMS_URL, wait_until="domcontentloaded")

        # 等待登录成功 — 检测页面中出现商家后台特征元素
        logged_in = False
        max_wait = 300  # 最多等待5分钟
        check_interval = 3

        for i in range(max_wait // check_interval):
            try:
                # 检查是否已登录（检测侧边栏/主导航等特征）
                url = self.page.url
                if "/login" not in url and "login" not in url:
                    # 尝试检测后台页面元素
                    sidebar = await self.page.query_selector(
                        "[class*='sidebar'], [class*='menu'], [class*='nav'], .main-layout"
                    )
                    if sidebar:
                        logged_in = True
                        print("✅ 检测到已登录状态！")
                        break
            except Exception:
                pass

            elapsed = (i + 1) * check_interval
            print(f"   ⏳ 等待登录中... ({elapsed}s / {max_wait}s)")
            await asyncio.sleep(check_interval)

        if not logged_in:
            print("⚠️ 检测超时，但将继续尝试操作")
            # 给用户一个手动按回车的机会
            try:
                input("\n如果已登录，请按 Enter 继续...")
            except (EOFError, OSError):
                print("   （非交互终端，自动继续）")

    async def _diagnose_page(self, label: str = "debug"):
        """完整页面诊断：截图 + 导出全部可见文本 + 导出 HTML"""
        import datetime
        ts = datetime.datetime.now().strftime("%H%M%S")
        base = os.path.join(USER_DATA_DIR, f"{label}_{ts}")

        try:
            await self.page.screenshot(path=f"{base}.png", full_page=False)
            print(f"   📸 截图已保存: {label}_{ts}.png")
        except Exception as e:
            print(f"   ⚠️ 截图失败: {e}")

        try:
            body_text = await self.page.evaluate("document.body.innerText")
            with open(f"{base}_text.txt", "w", encoding="utf-8") as f:
                f.write(body_text)
            print(f"   📄 页面文本已导出 ({len(body_text)} 字): {label}_{ts}_text.txt")
        except Exception as e:
            print(f"   ⚠️ 文本导出失败: {e}")

        try:
            html = await self.page.content()
            with open(f"{base}_html.txt", "w", encoding="utf-8") as f:
                f.write(html[:50000])  # 前 50KB 足够分析筛选栏
            print(f"   📄 HTML 已导出: {label}_{ts}_html.txt")
        except Exception as e:
            print(f"   ⚠️ HTML 导出失败: {e}")

        # 额外：用 JS 扫描页面顶部和中部区域的文本元素
        try:
            scan = await self.page.evaluate("""
                () => {
                    const results = { top: [], mid: [] };
                    const all = document.querySelectorAll('span, div, a, button, li, label, p, h1, h2, h3, h4');
                    for (const el of all) {
                        const text = (el.textContent || '').trim();
                        if (text.length < 1 || text.length > 80) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 5) continue;
                        const info = text.substring(0, 40) + ' @(' + Math.round(rect.x) + ',' + Math.round(rect.y) + ') ' + rect.width + 'x' + rect.height;
                        if (rect.y >= 0 && rect.y < 500) {
                            if (results.top.length < 50) results.top.push(info);
                        } else if (rect.y >= 500 && rect.y < 1500) {
                            if (results.mid.length < 30) results.mid.push(info);
                        }
                    }
                    return results;
                }
            """)
            print(f"   🔍 顶部(0-500px)文本元素({len(scan['top'])}个): {scan['top'][:20]}")
            print(f"   🔍 中部(500-1500px)文本元素({len(scan['mid'])}个): {scan['mid'][:10]}")
        except Exception as e:
            print(f"   ⚠️ 文本扫描失败: {e}")

    @staticmethod
    def _is_review_page_url(url):
        u = (url or "").lower()
        return (("evaluat" in u or "comment" in u or "review" in u)
                and "goods_list" not in u)

    async def navigate_to_review_management(self):
        """导航到评价管理页面，并确认真的落在评价页。

        2026-11 改版后旧地址会被重定向到商品列表页（共有 7 条 = 7 个商品），
        所以按候选地址逐个尝试 + 校验落地 URL；还不行就从侧栏读真实 href 再跳。
        """
        print("\n📍 正在导航到评价管理页面...")

        candidates = list(REVIEW_PAGE_URLS)
        try:
            hrefs = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'a[href*="evaluat"], a[href*="comment"], a[href*="review"]'))
                  .map(a => a.getAttribute('href')).filter(Boolean).slice(0, 10)
            """)
            for h in hrefs or []:
                if h.startswith("//"):
                    h = "https:" + h
                elif h.startswith("/"):
                    h = "https://mms.pinduoduo.com" + h
                if h.startswith("http") and h not in candidates:
                    candidates.append(h)
        except Exception:
            pass

        landed = False
        for url in candidates[:8]:
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                print(f"   ⚠️ 访问 {url} 失败: {str(e)[:80]}")
                continue
            await asyncio.sleep(PAGE_WAIT_SECONDS + 1)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            print(f"   📍 尝试 {url} → 实际 {self.page.url}")
            if self._is_review_page_url(self.page.url):
                landed = True
                break

        if not landed:
            print("   直接访问未进入评价页，尝试通过导航菜单...")
            for selector in SELECTORS["evaluate_link"].split(", "):
                selector = selector.strip()
                try:
                    target = self.page.locator(selector).first
                    if await target.count() > 0:
                        await self.safe_click(target)
                        await asyncio.sleep(PAGE_WAIT_SECONDS + 2)
                        try:
                            await self.page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        if self._is_review_page_url(self.page.url):
                            landed = True
                            print(f"   ✅ 通过 '{selector}' 进入评价管理, URL: {self.page.url}")
                            break
                except Exception:
                    continue
            if not landed:
                print(f"   ⚠️ 未能进入评价管理页，当前：{self.page.url}")

        # ===== 完整诊断：截图+文本+HTML =====
        print("\n   🔬 正在分析评价管理页面结构...")
        await self._diagnose_page("eval_page")

        # ===== 等待并滚动确保所有内容加载 =====
        print("   ⏳ 等待评价内容完全加载...")
        await asyncio.sleep(2)
        for _ in range(4):
            await self.scroll_page_down(600)
            await asyncio.sleep(0.8)
        await self.scroll_to_bottom()
        await asyncio.sleep(1)
        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        print("   ✅ 评价管理页面就绪")
        return True

    # ---------- 精确 DOM 定位：星级标签 / 评价卡片 / 分页 ----------

    async def _scan_star_filters(self):
        """扫描星级筛选标签，打 data-pdd-star 标记并返回选中态"""
        try:
            return await self.page.evaluate("""
                () => {
                    document.querySelectorAll('[data-pdd-star]').forEach(e => e.removeAttribute('data-pdd-star'));
                    const cn = {'一':1,'二':2,'三':3,'四':4,'五':5};
                    const out = [];
                    for (const el of document.querySelectorAll('span, div, a, button, li, label, p, td, th, input')) {
                        if (el.offsetParent === null) continue;
                        const raw = (el.textContent || '').trim();
                        if (!raw || raw.length > 12) continue;
                        const t = raw.split(' ').join('');
                        let s = null;
                        const m = t.match(/^([1-5])星$/);
                        if (m) s = parseInt(m[1]);
                        else {
                            const m2 = t.match(/^([一二三四五])星$/);
                            if (m2) s = cn[m2[1]];
                        }
                        if (!s) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 5) continue;
                        const cls = (el.className || '').toString().toLowerCase();
                        const pcls = el.parentElement ? (el.parentElement.className || '').toString().toLowerCase() : '';
                        const active = /active|selected|checked|current|primary|is-on/.test(cls + ' ' + pcls)
                            || el.getAttribute('aria-checked') === 'true'
                            || el.getAttribute('aria-selected') === 'true'
                            || (el.tagName === 'INPUT' && el.checked === true);
                        const mark = String(out.length);
                        el.setAttribute('data-pdd-star', mark);
                        out.push({mark: mark, star: s, text: raw, tag: el.tagName,
                                  cls: (el.className || '').toString().substring(0, 70),
                                  active: active, x: Math.round(r.x), y: Math.round(r.y)});
                    }
                    return out;
                }
            """)
        except Exception as e:
            print(f"   ⚠️ 星级标签扫描失败: {e}")
            return []

    async def _click_star_mark(self, mark):
        try:
            loc = self.page.locator('[data-pdd-star="' + mark + '"]').first
            await loc.scroll_into_view_if_needed()
            await loc.click(timeout=3000)
        except Exception:
            try:
                await self.page.evaluate(
                    "(m) => { const e = document.querySelector('[data-pdd-star=\"' + m + '\"]'); if (e) e.click(); }", mark)
            except Exception:
                return False
        await asyncio.sleep(0.7)
        return True

    async def _set_only_star(self, star: int):
        """
        保证页面上「只有目标星级」处于选中态。
        兼容两种交互：多选标签（点 2 星时 1 星仍选中）与单选 tab。
        每步都重新读取真实选中态，不靠点击次数猜。
        """
        state = await self._scan_star_filters()

        # 星级标签可能在下拉里，先尝试展开
        if not state:
            try:
                opened = await self.page.evaluate("""
                    () => {
                        document.querySelectorAll('[data-pdd-star-trigger]').forEach(e => e.removeAttribute('data-pdd-star-trigger'));
                        const kws = ['星级', '评价星级', '评分', '筛选'];
                        for (const el of document.querySelectorAll('span, div, button, a, [class*="select"]')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || '').trim();
                            if (!t || t.length > 20) continue;
                            if (kws.some(k => t.includes(k))) {
                                el.setAttribute('data-pdd-star-trigger', '1');
                                return t;
                            }
                        }
                        return null;
                    }
                """)
                if opened:
                    try:
                        await self.page.locator('[data-pdd-star-trigger]').first.click(timeout=3000)
                    except Exception:
                        await self.page.evaluate("""
                            () => { const e = document.querySelector('[data-pdd-star-trigger]'); if (e) e.click(); }
                        """)
                    await asyncio.sleep(0.9)
                    print(f"   🔽 已展开筛选「{opened}」")
                    state = await self._scan_star_filters()
            except Exception:
                pass

        if not state:
            self._star_filter_verified = False
            print("   ℹ️ 页面上未找到 1/2/3 星筛选标签 → 降级为「按卡片文字判定星级」")
            return None

        print("   🔍 星级标签: " + ", ".join(
            f"{x['text']}{'(选中)' if x['active'] else ''}" for x in state[:12]))

        # 最多 3 轮：先点亮目标 → 再取消所有残留的其他星级 → 复核
        for _ in range(3):
            state = await self._scan_star_filters()
            targets = [x for x in state if x['star'] == star]
            others_active = [x for x in state if x['star'] != star and x['active']]
            target_active = any(x['active'] for x in targets)
            if target_active and not others_active:
                break
            if not target_active and targets:
                await self._click_star_mark(targets[0]['mark'])
                print(f"   ⭐ 点亮 {star} 星标签")
                continue
            if others_active:
                for x in others_active[:5]:
                    await self._click_star_mark(x['mark'])
                    print(f"   🚫 取消残留的「{x['text']}」选中态")
                continue
            break

        state = await self._scan_star_filters()
        others_active = [x for x in state if x['star'] != star and x['active']]
        target_active = any(x['active'] for x in state if x['star'] == star)
        self._star_filter_verified = bool(target_active and not others_active)
        if self._star_filter_verified:
            print(f"   ✅ 星级状态已确认：仅 {star} 星为选中态")
        else:
            print(f"   ⚠️ 星级状态未达标（目标选中={target_active}，残留={[x['text'] for x in others_active]}）"
                  f" → 降级为卡片文字复核，并导出页面结构")
            await self._export_page_structure(f"star_filter_{star}")
        return self._star_filter_verified

    async def _export_page_structure(self, tag="scan"):
        """导出真实 DOM 结构，供校准选择器（--scan / 状态异常时自动调用）"""
        try:
            html_path = os.path.join(LOG_DIR, f"{tag}_page.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(await self.page.content())
            info = await self.page.evaluate("""
                () => {
                    const pick = (el) => ({
                        tag: el.tagName, text: (el.textContent || '').trim().substring(0, 60),
                        cls: (el.className || '').toString().substring(0, 100),
                        html: el.outerHTML.substring(0, 400),
                        y: Math.round(el.getBoundingClientRect().y)
                    });
                    const stars = [], reports = [], pagers = [], cards = [];
                    for (const el of document.querySelectorAll('*')) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (/^[1-5]星$/.test(t) && t.length < 8) stars.push(pick(el));
                        if (t === '举报' && el.children.length === 0) reports.push(pick(el));
                        if (t.includes('给出了') && t.includes('星') && t.length < 60) cards.push(pick(el));
                    }
                    for (const el of document.querySelectorAll('[class*="pagination"], [class*="pager"], [class*="page-"]')) {
                        if (el.offsetParent === null) continue;
                        if ((el.textContent || '').trim().length < 200) pagers.push(pick(el));
                    }
                    return {url: location.href, title: document.title,
                            stars: stars.slice(0, 30), reports: reports.slice(0, 10),
                            cards: cards.slice(0, 10), pagers: pagers.slice(0, 10),
                            bodyText: (document.body.innerText || '').substring(0, 1200)};
                }
            """)
            json_path = os.path.join(LOG_DIR, f"{tag}_structure.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            await self.page.screenshot(path=os.path.join(LOG_DIR, f"{tag}_page.png"))
            print(f"   📦 结构清单: {json_path}")
            print(f"   📦 完整 HTML: {html_path}")
            return json_path
        except Exception as e:
            print(f"   ⚠️ 结构导出失败: {e}")
            return None

    async def _pager_state(self):
        """
        读取真实分页组件状态。data-status="beast-core-pagination-10-1" 末段就是当前页码，
        比数 active class 可靠；nextDisabled 直接告诉我们还有没有下一页。
        """
        try:
            js = r"""
                () => {
                    const SEL_UL = %r, SEL_NEXT = %r, SEL_PREV = %r;
                    const SEL_ACTIVE = %r, SEL_ITEM = %r, SEL_TOTAL = %r;
                    const ul = document.querySelector(SEL_UL);
                    if (!ul) return {found: false};
                    const status = ul.getAttribute('data-status') || '';
                    const m = status.match(/pagination-([0-9]+)-([0-9]+)/);
                    const active = ul.querySelector(SEL_ACTIVE);
                    const items = [];
                    for (const li of ul.querySelectorAll(SEL_ITEM)) {
                        const t = (li.textContent || '').trim();
                        if (/^[0-9]+$/.test(t)) items.push(parseInt(t, 10));
                    }
                    const next = ul.querySelector(SEL_NEXT);
                    const prev = ul.querySelector(SEL_PREV);
                    const totalEl = ul.querySelector(SEL_TOTAL);
                    const sizeEl = ul.querySelector('input[data-testid="beast-core-select-htmlInput"]');
                    const totalTxt = totalEl ? (totalEl.innerText || '') : '';
                    const tm = totalTxt.match(/([0-9]+)\s*条/);
                    return {
                        found: true,
                        status: status,
                        page: m ? parseInt(m[2], 10)
                            : (active ? parseInt((active.textContent || '').trim(), 10) : null),
                        size: m ? parseInt(m[1], 10)
                            : (sizeEl ? parseInt(sizeEl.value, 10) : null),
                        activeText: active ? (active.textContent || '').trim() : '',
                        maxVisible: items.length ? Math.max.apply(null, items) : null,
                        totalRows: tm ? parseInt(tm[1], 10) : null,
                        nextExists: !!next,
                        nextDisabled: next ? /disabled/i.test(next.className || '') : true,
                        prevDisabled: prev ? /disabled/i.test(prev.className || '') : false,
                        totalText: totalTxt.trim()
                    };
                }
            """ % (PAGER_UL_SELECTOR, PAGER_NEXT_SELECTOR, PAGER_PREV_SELECTOR,
                   PAGER_ACTIVE_SELECTOR, PAGER_ITEM_SELECTOR, PAGER_TOTAL_SELECTOR)
            return await self.page.evaluate(js)
        except Exception as e:
            print(f"   ⚠️ 读取分页状态失败: {e}")
            return {"found": False}

    async def _legacy_detect_total_pages(self):
        """兜底：从整页文本读「共有 N 条 每页 M 条」"""
        try:
            return await self.page.evaluate("""
                () => {
                    const t = (document.body.innerText || '').split(' ').join('');
                    const m = t.match(/共有([0-9]+)条/);
                    const p = t.match(/每页([0-9]+)条/);
                    if (m && p && parseInt(p[1]) > 0) {
                        return Math.ceil(parseInt(m[1]) / parseInt(p[1]));
                    }
                    return null;
                }
            """)
        except Exception:
            return None

    async def _detect_total_pages(self, wait_stable: bool = True):
        """
        总页数：优先用分页组件的「共有 N 条 / 每页 M 条」，末页数字交叉验证。
        筛选后页面会短暂显示旧数据，轮询到读数稳定再返回，避免把 limit 误算成 1 页。
        """
        last = None
        stable = 0
        rounds = 8 if wait_stable else 1
        for _ in range(rounds):
            st = await self._pager_state()
            val = None
            if st.get("found"):
                if st.get("totalRows") and st.get("size"):
                    val = (st["totalRows"] + st["size"] - 1) // st["size"]
                elif st.get("maxVisible"):
                    val = st["maxVisible"]
            if val is None:
                val = await self._legacy_detect_total_pages()
            if val == last:
                stable += 1
            else:
                stable = 0
                last = val
            if stable >= 2 or rounds == 1:
                return val
            await asyncio.sleep(0.6)
        return last

    async def _page_signature(self):
        """
        当前页内容指纹：取列表前 3 个订单号，不依赖可能残留/被清掉的 data-pdd-review 标记。
        """
        try:
            return await self.page.evaluate("""
                () => {
                    const t = document.body.innerText || '';
                    const ids = t.match(/订单编号[:：]?[ ]*([0-9A-Za-z-]{8,})/g) || [];
                    if (ids.length) return ids.slice(0, 3).join('|');
                    const ul = document.querySelector(%r);
                    if (ul) return (ul.getAttribute('data-status') || '') + '|' + t.trim().substring(0, 120);
                    return t.trim().substring(0, 250);
                }
            """ % PAGER_UL_SELECTOR)
        except Exception:
            return ""

    async def _collect_review_cards(self):
        """
        以「举报」文字链接为锚点向上找评价卡片，给卡片和举报链接打标记。
        返回 [{idx, text, hasBtn}]，Python 侧用 [data-pdd-review="i"] 精确定位。
        """
        try:
            return await self.page.evaluate("""
                () => {
                    document.querySelectorAll('[data-pdd-review]').forEach(e => e.removeAttribute('data-pdd-review'));
                    document.querySelectorAll('[data-pdd-report-btn]').forEach(e => e.removeAttribute('data-pdd-report-btn'));
                    const cards = [], seen = new Set();
                    for (const el of document.querySelectorAll('a, button, span, div, li')) {
                        if (el.offsetParent === null) continue;
                        if ((el.textContent || '').trim() !== '举报') continue;
                        if (el.children.length > 0) continue;
                        let node = el, card = null;
                        for (let i = 0; i < 10 && node; i++) {
                            const ct = node.textContent || '';
                            if (ct.includes('用户评价分') && (ct.includes('订单编号') || ct.includes('给出了'))) {
                                card = node; break;
                            }
                            node = node.parentElement;
                        }
                        if (!card || seen.has(card)) continue;
                        const r = card.getBoundingClientRect();
                        if (r.height > 900 || (card.textContent || '').length > 2000) continue;
                        seen.add(card); cards.push(card);
                    }
                    return cards.map((card, i) => {
                        card.setAttribute('data-pdd-review', String(i));
                        let btn = null;
                        for (const el of card.querySelectorAll('*')) {
                            if ((el.textContent || '').trim() === '举报' &&
                                el.children.length === 0 && el.offsetParent !== null) btn = el;
                        }
                        if (btn) btn.setAttribute('data-pdd-report-btn', String(i));
                        return {idx: i, text: (card.innerText || '').trim().substring(0, 500), hasBtn: !!btn};
                    });
                }
            """)
        except Exception as e:
            print(f"   ⚠️ 评价卡片扫描失败: {e}")
            return []

    @staticmethod
    def _detect_card_star(text):
        m = re.search(r"给出了\s*([1-5])\s*星", text or "")
        if m:
            return int(m.group(1))
        m2 = re.search(r"用户评价分[:：]\s*([★☆]{1,5})", text or "")
        if m2:
            return len(m2.group(1).replace("☆", ""))
        return None

    def _card_star_matches(self, text, star):
        detected = self._detect_card_star(text)
        if detected is not None:
            return (detected == star), f"卡片文字判定为 {detected} 星"
        if self._star_filter_verified:
            return True, "卡片无星级文字，但筛选状态已确认"
        return False, "无法判定星级且筛选未确认（可跑 --scan 校准）"

    async def _cancel_current_star_filter(self):
        """取消当前已选中的星级筛选（点击"全部"重置 + 取消激活项）"""
        print("   🔄 正在清除之前的星级筛选...")

        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # ===== 策略 1：Playwright 找"全部"按钮点击 =====
        for pattern in ["全部", "全部评价", "全部评论", "全部星级", "全部分类"]:
            try:
                loc = self.page.locator(f"text={pattern}")
                count = await loc.count()
                for j in range(min(count, 10)):
                    el = loc.nth(j)
                    if not await el.is_visible():
                        continue
                    rect = await el.bounding_box()
                    if rect and rect["y"] < 600 and rect["width"] < 200:
                        await el.click(timeout=3000)
                        await asyncio.sleep(1.5)
                        print(f"   ✅ 已点击「{pattern}」重置筛选 @ y={rect['y']:.0f}")
                        return
            except Exception:
                continue

        # ===== 策略 2：JS 取消激活的星级项 =====
        cancelled = await self.page.evaluate("""
            () => {
                const starPattern = /^[1-5]星$/;
                const candidates = document.querySelectorAll('span, div, a, button, li');
                for (const el of candidates) {
                    const text = (el.textContent || '').trim();
                    const cls = (el.className || '').toString();
                    const clsLower = cls.toLowerCase();
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 15 || rect.height < 8 || rect.top < 0) continue;
                    if (!starPattern.test(text)) continue;

                    const isActive = (
                        /\\bactive\\b/.test(clsLower) ||
                        /\\bselected\\b/.test(clsLower) ||
                        /\\bchecked\\b/.test(clsLower) ||
                        /-active/.test(clsLower) ||
                        /--active/.test(clsLower) ||
                        /-selected/.test(clsLower) ||
                        /--selected/.test(clsLower) ||
                        clsLower.startsWith('active') || clsLower.startsWith('selected')
                    );
                    if (isActive) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return {text: text, cls: cls.substring(0, 40), y: Math.round(rect.y)};
                    }
                }
                return null;
            }
        """)

        if cancelled:
            await asyncio.sleep(0.8)
            print(f"   ✅ 已取消「{cancelled['text']}」筛选 (y={cancelled['y']})")
        else:
            print("   ℹ️ 未检测到已激活的星级筛选")

    async def filter_by_star(self, star: int):
        """筛选指定星级的评价（多种策略复合，不限制Y轴范围）"""
        print(f"\n⭐ 正在筛选 {star} 星评价...")

        # 先滚动到顶部
        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # ===== 状态化切换：确保只有目标星级被选中 =====
        # 不再盲点「N星」文本：那会在多选标签上叠加选中，或误点评价内容里的「给出了1星」
        ok = await self._set_only_star(star)
        await asyncio.sleep(0.8)
        self._page_num = 1
        # 筛选后列表会短暂显示旧数据，轮询到分页读数稳定再取总页数
        self._total_pages = await self._detect_total_pages(wait_stable=True)
        _ps = await self._pager_state()
        print(f"   📄 当前筛选下共约 {self._total_pages or '未知'} 页"
              + (f"（{_ps.get('totalText')}，当前第 {_ps.get('page')} 页）"
                 if _ps.get("found") else ""))
        if ok is True:
            await self._verify_star_filter_applied(star)
            return True
        if (ok is None or ok is False) and not STAR_FILTER_REQUIRED:
            print(f"   ⚠️ 不依赖星级筛选控件，改用「卡片文字复核星级」：只举报 {star} 星")
            return True
        print("   ⚠️ 星级状态未达标，尝试下方兜底策略...")
        await self._cancel_current_star_filter()
        await asyncio.sleep(0.5)

        # ===== 策略 1：Playwright text locator（旧兜底，默认不执行） =====
        # 尝试多种文本模式
        text_patterns = [f"{star}星", f"{star} 星", f"{star}分"]
        chinese = ['', '一', '二', '三', '四', '五']
        if chinese[star]:
            text_patterns.extend([f"{chinese[star]}星", f"{chinese[star]} 星"])

        for pattern in text_patterns:
            try:
                # 找所有包含该文本的可点击元素
                loc = self.page.locator(f"text={pattern}")
                count = await loc.count()
                if count > 0:
                    for j in range(min(count, 20)):
                        el = loc.nth(j)
                        if not await el.is_visible():
                            continue
                        # 尝试点击元素本身
                        try:
                            await el.scroll_into_view_if_needed()
                            await el.click(timeout=3000)
                            await asyncio.sleep(1.5)
                            print(f"   ✅ Playwright locator 点击成功: text='{pattern}' idx={j}")
                            await self._verify_star_filter_applied(star)
                            return True
                        except Exception:
                            pass
                        # 尝试点击父元素
                        try:
                            parent = el.locator("..")
                            if await parent.count() > 0:
                                await parent.scroll_into_view_if_needed()
                                await parent.click(timeout=3000)
                                await asyncio.sleep(1.5)
                                print(f"   ✅ Playwright locator 点击父元素成功: text='{pattern}' idx={j}")
                                await self._verify_star_filter_applied(star)
                                return True
                        except Exception:
                            pass
                        # 用 JS click 兜底
                        try:
                            await el.evaluate("el => el.click()")
                            await asyncio.sleep(1.5)
                            print(f"   ✅ JS click 成功: text='{pattern}' idx={j}")
                            await self._verify_star_filter_applied(star)
                            return True
                        except Exception:
                            pass
            except Exception:
                continue

        # ===== 策略 2：JS 搜索全页面所有匹配元素（无Y轴限制） =====
        print("   🔍 Playwright locator 未找到，使用 JS 全页面搜索...")
        clicked = await self.page.evaluate(f"""
            (starNum) => {{
                const patterns = [
                    '{{starNum}}星', '{{starNum}} 星', '{{starNum}}分', '{{starNum}} 分'
                ];
                const chineseDigits = ['', '一', '二', '三', '四', '五'];
                if (chineseDigits[starNum]) {{
                    patterns.push(chineseDigits[starNum] + '星');
                    patterns.push(chineseDigits[starNum] + ' 星');
                }}

                const matches = (text) => {{
                    for (const p of patterns) {{
                        if (text === p) return true;
                        if (text.startsWith(p + '(')) return true;
                        if (text.startsWith(p + '（')) return true;
                        if (text.startsWith(p + ' ')) return true;
                        if (text.startsWith(p) && text.length <= p.length + 15) return true;
                        if (text.includes(p) && text.length <= p.length + 10) return true;
                    }}
                    return false;
                }};

                // 搜索全页面所有元素，不限制Y轴
                const tags = ['span', 'div', 'a', 'button', 'li', 'label', 'p', 'td', 'th'];
                for (const tag of tags) {{
                    const elements = document.querySelectorAll(tag);
                    let bestEl = null;
                    let bestScore = Infinity;

                    for (const el of elements) {{
                        const text = (el.textContent || '').trim();
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 15 || rect.height < 8 || rect.top < 0) continue;
                        if (!matches(text)) continue;

                        // 优先选中：靠近顶部、宽度较小（更像tab）、在可见区域
                        const score = rect.top * 0.5 + rect.width * 0.01 + (rect.top > window.innerHeight ? 10000 : 0);
                        if (score < bestScore) {{
                            bestScore = score;
                            bestEl = el;
                        }}
                    }}

                    if (bestEl) {{
                        bestEl.scrollIntoView({{block: 'center'}});
                        bestEl.click();
                        return {{
                            text: (bestEl.textContent || '').trim().substring(0, 30),
                            tag: bestEl.tagName,
                            x: Math.round(bestEl.getBoundingClientRect().x),
                            y: Math.round(bestEl.getBoundingClientRect().y)
                        }};
                    }}
                }}
                return null;
            }}
        """, star)

        if clicked and "x" in clicked:
            await asyncio.sleep(2)
            print(f"   ✅ JS搜索点击: <{clicked['tag']}>「{clicked['text']}」@ ({clicked['x']},{clicked['y']})")
            await self._verify_star_filter_applied(star)
            return True

        # ===== 策略 3：选择器兜底 =====
        star_selectors = {
            1: SELECTORS["star_filter_1"],
            2: SELECTORS["star_filter_2"],
            3: SELECTORS["star_filter_3"],
        }
        selectors_str = star_selectors.get(star, "")
        if selectors_str:
            for selector in selectors_str.split(", "):
                selector = selector.strip()
                try:
                    element = self.page.locator(selector).first
                    if await element.count() > 0 and await element.is_visible():
                        await self.scroll_element_into_view(element)
                        await element.click()
                        await asyncio.sleep(2)
                        print(f"   ✅ 选择器兜底成功: {selector}")
                        return True
                except Exception:
                    continue

        # ===== 全部失败，诊断 =====
        print(f"   ❌ 所有策略均未找到 {star} 星筛选控件")
        await self._diagnose_page(f"fail_filter_{star}star")
        return False

    async def _verify_star_filter_applied(self, star: int):
        """验证星级筛选是否生效（检查URL变化或页面元素状态）"""
        await asyncio.sleep(0.5)
        url = self.page.url
        print(f"   📍 筛选后URL: {url}")

        # 等待页面内容加载
        await asyncio.sleep(1)
        await self.scroll_to_bottom()
        await asyncio.sleep(0.5)
        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # 快速检查页面是否有评价内容
        try:
            body_len = await self.page.evaluate("document.body.innerText.length")
            print(f"   📏 页面文本长度: {body_len} 字")
        except Exception:
            pass

    async def find_report_button(self, review_element):
        """
        在评价元素附近查找举报按钮。
        策略：用 JS 在页面上扫描所有含"举报"文本的可点击元素，
        按位置（与当前评价的垂直/水平距离）匹配最近的那个。
        """
        # ===== 首选：使用 _collect_review_cards 打好的精确标记 =====
        # 避免全页面坐标扫描误点左侧菜单里的「异常举报」等链接
        try:
            marked = review_element.locator('[data-pdd-report-btn]').first
            if await marked.count() > 0 and await marked.is_visible():
                print("   🔘 已精确定位本条评价的「举报」链接")
                return marked
        except Exception:
            pass

        # 获取评价元素的 DOM 边界
        review_rect = None
        try:
            review_rect = await review_element.bounding_box()
        except Exception:
            pass

        # ===== 用 JS 全页面扫描"举报"相关元素 =====
        report_targets = await self.page.evaluate("""
            () => {
                const results = [];
                // 搜索所有元素，不限于叶子节点（按钮可能内嵌图标）
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.offsetParent === null) continue;  // 不可见
                    const text = (el.textContent || '').trim();
                    // 含"举报"且不长（避免匹配整个页面文本块）
                    if (text.includes('举报') && text.length < 30 && text.length > 0) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top < window.innerHeight + 200) {
                            results.push({
                                x: rect.x + rect.width / 2,
                                y: rect.y + rect.height / 2,
                                w: rect.width,
                                h: rect.height,
                                text: text.substring(0, 30),
                                tag: el.tagName.toLowerCase(),
                                cls: (el.className || '').toString().substring(0, 50),
                            });
                        }
                    }
                }
                return results;
            }
        """)

        if review_rect and report_targets:
            review_center_y = review_rect["y"] + review_rect["height"] / 2
            review_center_x = review_rect["x"] + review_rect["width"] / 2

            # 在评价元素可见区域下方或附近找"举报"按钮
            candidates = []
            for t in report_targets:
                ty = t["y"]
                tx = t["x"]
                # 举报按钮通常在评价行的右侧或下方，X轴靠近评价区
                dx = abs(tx - review_center_x)
                dy = ty - review_center_y  # 正=在下方，负=在上方
                # 容忍范围：上下 800px，左右 1000px
                if abs(dy) < 800 and dx < 1000:
                    dist = abs(dy) + dx * 0.3
                    candidates.append((dist, t))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                closest = candidates[0][1]
                print(f"   🎯 JS定位举报按钮：文字='{closest['text']}' 位置=({closest['x']:.0f},{closest['y']:.0f}) [{closest['tag']}]")
                try:
                    await self.page.mouse.click(closest["x"], closest["y"])
                    await asyncio.sleep(0.5)
                    return True  # 已通过坐标点击
                except Exception as e:
                    print(f"   ⚠️ 坐标点击失败: {e}，回退选择器...")

        # ===== 回退：用 Playwright 选择器在评价元素内查找 =====
        for selector in [
            "text=举报", "button:has-text('举报')", "a:has-text('举报')",
            "span:has-text('举报')", "[class*='report']", "[class*='jubao']",
        ]:
            try:
                btn = review_element.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    print(f"   🔘 选择器找到举报按钮: {selector}")
                    return btn
            except Exception:
                continue

        # ===== 再回退：全局查找最近的"举报"按钮 =====
        try:
            all_btns = await self.page.locator("text=举报").all()
            best = None
            best_dist = float("inf")
            for btn in all_btns:
                if not await btn.is_visible():
                    continue
                try:
                    btn_box = await btn.bounding_box()
                    if btn_box and review_rect:
                        dy = btn_box["y"] - review_rect["y"]
                        dx = abs(btn_box["x"] - review_rect["x"])
                        if abs(dy) < 600 and dx < 800:
                            dist = abs(dy) + dx * 0.3
                            if dist < best_dist:
                                best_dist = dist
                                best = btn
                except Exception:
                    continue
            if best:
                print(f"   🔘 全局找到举报按钮")
                return best
        except Exception:
            pass

        return None

    # ---------- 去重台账 / 执行日志 ----------

    def _load_reported(self):
        try:
            with open(REPORTED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _mark_reported(self, key):
        self.reported_ids.add(key)
        try:
            with open(REPORTED_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(self.reported_ids), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"   ⚠️ 去重台账写入失败: {e}")

    @staticmethod
    def _review_key(text):
        """提取稳定唯一键：优先订单号，否则用规范化文本 hash"""
        m = re.search(r"\d{12,}", text or "")
        if m:
            return "order_" + m.group(0)
        normalized = re.sub(r"\s+", "", text or "")[:120]
        return "hash_" + hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def _write_log(self, star, index, key, status, note=""):
        plan = self._active_plan or {}
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "probe" if PROBE_MODE else ("dry" if DRY_RUN else "live"),
            "star": star, "index": index, "key": key,
            "type": plan.get("name") or REPORT_TYPE,
            "rank": plan.get("rank", 0),
            "viewid": plan.get("viewid", ""),
            "sub_reason": plan.get("sub_reason", ""),
            "reason": plan.get("text") or REPORT_REASON,
            "type_confidence": self._type_confidence,
            "status": status, "note": note,
        }
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- 风控 / 当日举报上限：暂停并提醒 ----------

    async def _scan_risk_text(self):
        """读取弹窗 / toast 中可见文案，用于识别「今日次数已达上限」这类明确提示"""
        try:
            return await self.page.evaluate("""
                () => {
                    const sel = '[class*="toast"], [class*="Toast"], '
                        + '[class*="message"], [class*="notification"], '
                        + '[role="alert"], [data-testid="beast-core-modal"] '
                        + '[class*="error"], [data-testid="beast-core-modal"] '
                        + '[class*="tip"]';
                    const out = [];
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t && t.length < 600 && !out.includes(t)) out.push(t);
                        if (out.length >= 8) break;
                    }
                    return out.join(' | ');
                }
            """)
        except Exception:
            return ""

    async def _pause_for_daily_limit(self, why, detail="",
                                     review_index=0, review_key=""):
        """
        判定当日额度已用完：停止全部任务、不写去重台账、弹桌面提醒。
        试运行 / 探测模式只提示不落标记，避免误挡第二天。
        """
        if self.daily_limit_reached:
            return
        self.daily_limit_reached = True
        self.stats["daily_limit_hits"] = self.stats.get("daily_limit_hits", 0) + 1
        live = not (DRY_RUN or PROBE_MODE or SCAN_MODE)

        # 收掉当前举报弹窗，绝不残留一个半填状态
        try:
            await self._close_dialog()
        except Exception:
            pass

        msg = daily_limit_message(self.stats["total_reported"])
        if live:
            mark_daily_limit(why, self.stats["total_reported"], detail)
        else:
            msg += "\n\n（当前为试运行/探测，未写入当日上限标记）"
        print_daily_limit_banner(msg)
        if live:
            show_desktop_alert(ALERT_TITLE, msg)
        try:
            print("\a", end="", flush=True)   # 终端提示音
        except Exception:
            pass

        self._write_log(self._current_star or 0, review_index, review_key,
                        "daily_limit_paused",
                        "entry=%s why=%s detail=%s live=%s 未提交、未写台账"
                        % (REPORT_TYPE, why, str(detail)[:120], live))
        print("   ⏸️ 已停止：不再换入口、不再处理后续评价、不再翻页")
        print("   💡 明天直接重新运行启动器即可（当日标记跨天自动失效）")
        print("   💡 若确认是误判，可用「--force」或启动器菜单 11 清除标记\n",
              flush=True)

    async def _maybe_risk_pause(self, why, detail="",
                                review_index=0, review_key="", force=False):
        """风控信号计数；达到阈值就暂停。成功路径调用 _clear_risk_streak() 清零"""
        if self.daily_limit_reached or not RISK_STOP_ENABLED:
            return False
        self._risk_streak += 1
        if force or self._risk_streak >= RISK_STOP_CONSECUTIVE:
            await self._pause_for_daily_limit(why, detail, review_index, review_key)
            return True
        print("   ⚠️ 风控信号 %d/%d，暂继续观察"
              % (self._risk_streak, RISK_STOP_CONSECUTIVE))
        return False

    def _clear_risk_streak(self):
        self._risk_streak = 0

    # ---------- 弹窗探测（首次上线必跑） ----------

    async def probe_dialog(self):
        """导出举报弹窗 DOM / 截图 / 可点击项清单，用于校准「同行恶意差评」选择器"""
        print("\n🔬 探测模式：正在导出举报弹窗结构（不做任何提交）...")
        try:
            with open(PROBE_FILE, "w", encoding="utf-8") as f:
                f.write(await self.page.content())
            await self.page.screenshot(path=os.path.join(LOG_DIR, "probe_dialog.png"))
            print(f"   📄 已保存: {PROBE_FILE}")
            print(f"   📸 已保存: {os.path.join(LOG_DIR, 'probe_dialog.png')}")
        except Exception as e:
            print(f"   ⚠️ 导出失败: {e}")

        try:
            items = await self.page.evaluate("""
                () => {
                    const out = [];
                    const roots = [];
                    for (const m of document.querySelectorAll('[data-testid="beast-core-modal"]')) {
                        if (m.offsetParent !== null) roots.push(m);
                    }
                    if (!roots.length) roots.push(document);
                    const seen = new Set();
                    for (const sc of roots) {
                        for (const el of sc.querySelectorAll(
                            'input, textarea, label, li, span, div, button, option, [role="radio"], [contenteditable]')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || el.value || '').trim();
                            if (!t || t.length > 40) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 5) continue;
                            const k = el.tagName + '|' + t + '|' + Math.round(r.y);
                            if (seen.has(k)) continue;
                            seen.add(k);
                            out.push({
                                tag: el.tagName, text: t.substring(0, 40),
                                cls: (el.className || '').toString().substring(0, 60),
                                checked: !!el.checked, role: el.getAttribute('role') || '',
                                y: Math.round(r.y)
                            });
                        }
                    }
                    return out.slice(0, 600);
                }
            """)
            path = os.path.join(LOG_DIR, "probe_dialog_items.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            print(f"   🔍 可点击项清单（{len(items)} 条）: {path}")
            for h in [i for i in items if "恶意" in i["text"] or "同行" in i["text"] or "举报" in i["text"]][:20]:
                print(f"      • <{h['tag']}> '{h['text']}' class={h['cls']}")
        except Exception as e:
            print(f"   ⚠️ 元素扫描失败: {e}")

    async def _wait_reason_field(self, verbose: bool = False):
        """选中类型后描述框可能是 React 条件渲染的，轮询等它出现"""
        st = await self._dialog_state()
        rounds = max(1, int(REASON_FIELD_WAIT_SECONDS / REASON_FIELD_POLL_SECONDS))
        for i in range(rounds):
            if st.get("reasonFields"):
                if verbose:
                    print(f"   ⏱️ 第 {i} 轮检测到描述输入框出现（共 {len(st['reasonFields'])} 个）")
                return st
            await asyncio.sleep(REASON_FIELD_POLL_SECONDS)
            st = await self._dialog_state()
            if not st.get("found"):
                return st
        return st

    async def _export_probe_after_type(self, review_index: int = 1,
                                       tag: str = "after_type", do_fill: bool = True):
        """
        选中举报类型并等待动态渲染后，导出弹窗真实结构。
        这是定位「详细描述」输入框最关键的诊断产物，绝不提交。
        """
        try:
            st = await self._dialog_state()
            p_html = os.path.join(LOG_DIR, "probe_dialog_" + tag + ".html")
            p_state = os.path.join(LOG_DIR, "probe_" + tag + "_state.json")
            p_fields = os.path.join(LOG_DIR, "probe_" + tag + "_fields.json")
            p_png = os.path.join(LOG_DIR, "probe_dialog_" + tag + ".png")
            with open(p_html, "w", encoding="utf-8") as f:
                f.write(await self.page.content())
            with open(p_state, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            with open(p_fields, "w", encoding="utf-8") as f:
                json.dump(st.get("reasonFields") or [], f, ensure_ascii=False, indent=2)
            try:
                await self.page.screenshot(path=p_png)
            except Exception:
                pass
            fields = st.get("reasonFields") or []
            print(f"   📄 选中类型后的弹窗: {os.path.basename(p_html)} / {os.path.basename(p_png)}")
            print(f"   📄 字段清单: {os.path.basename(p_fields)}")
            print(f"   📝 弹窗内可编辑输入框: {len(fields)} 个 | 类型选中态: {st.get('typeChecked')}")
            print(f"   🔘 提交按钮参数: {st.get('submitParams')} | 禁用: {st.get('submitDisabled')}")
            for fd in fields[:8]:
                print("      • <%s> placeholder=%r inModal=%s outer=%s"
                      % (fd.get("tag"), fd.get("placeholder"), fd.get("inModal"),
                         (fd.get("outer") or "")[:160]))
            if st.get("errors"):
                print(f"   ⚠️ 弹窗内错误提示: {st['errors']}")
            # 探测模式下顺手验证一次填写（只填不提交）
            if do_fill:
                rs = await self._fill_reason_in_dialog(
                    (self._active_plan or {}).get("text") or REPORT_REASON)
                print(f"   🧪 探测填写（绝不提交）: status={rs['status']} 回读={rs['value']!r}")
                with open(os.path.join(LOG_DIR, "probe_" + tag + "_fill.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(rs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"   ⚠️ 二次导出失败: {e}")

    # ---------- 真实弹窗 DOM 操作（2026-11-16 按 probe_dialog.html 校准） ----------

    def _dialog_js(self):
        """
        把选择器常量注入 JS。字段发现覆盖：
          - 标准 textarea / input[type=text] / contenteditable / role=textbox
          - 自定义组件（data-testid 或 class 含 textarea）
          - shadow DOM 内的元素
          - 选中类型后 Portal 挂到 body 的浮层（按 placeholder / 上下文文字判定）
        明确排除页面顶部 3 个筛选输入框与全站搜索框，绝不误填。
        另外返回 TYPE_VIEWIDS 四个入口各自的可见 / 禁用 / 选中态，供风控降级判断。
        """
        return """
            (opt) => {
                const SEL_SUBMIT = %r;
                const SEL_ROOT   = %r;
                const TYPE_VIEWIDS = %s;
                const WANT = (opt && opt.viewid) || TYPE_VIEWIDS[0];
                const WANT_NAME = (opt && opt.wantName) || '';
                const empty = {found: false, reasonFields: [], typeChecked: false,
                               checkedViewid: '', radios: [], errors: []};
                const btn = document.querySelector(SEL_SUBMIT);
                if (!btn || btn.offsetParent === null) return empty;
                let root = null;
                for (const r of document.querySelectorAll(SEL_ROOT)) {
                    if (r.contains(btn) && r.getBoundingClientRect().width > 100) { root = r; break; }
                }
                if (!root) {
                    let p = btn;
                    for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
                        if ((p.textContent || '').includes('举报类型')) { root = p; break; }
                    }
                }
                if (!root) root = btn.parentElement || btn;

                // 深度查询：包含 shadow DOM
                const deepAll = (node, sel) => {
                    const out = [];
                    if (!node) return out;
                    try { node.querySelectorAll(sel).forEach(e => out.push(e)); } catch (e) {}
                    try {
                        node.querySelectorAll('*').forEach(e => {
                            if (e.shadowRoot) deepAll(e.shadowRoot, sel).forEach(x => out.push(x));
                        });
                    } catch (e) {}
                    return out;
                };
                const FIELD_SEL = 'textarea, input, [contenteditable], [role="textbox"], '
                    + '[data-testid*="textarea"], [data-testid*="TextArea"], '
                    + '[class*="TextArea"], [class*="textArea"], [class*="textarea"]';
                const KW = /描述|说明|理由|举报|凭证|补充|原因|详情/;
                const isReasonField = (el, inModal) => {
                    if (!el || el.disabled || el.readOnly) return false;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const editable = el.isContentEditable === true
                        || el.getAttribute('role') === 'textbox';
                    if (tag === 'input' && !['text', '', 'search'].includes(type)) return false;
                    if (!editable && tag !== 'textarea' && tag !== 'input') return false;
                    if (el.offsetParent === null) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 12) return false;
                    // 顶部筛选框 / 全站搜索框一律排除
                    if (el.closest('.mms-header_search_new_box')) return false;
                    const tracker = el.closest('[data-tracking-click-viewid]');
                    if (tracker && /_input$/.test(tracker.getAttribute('data-tracking-click-viewid') || '')) {
                        return false;
                    }
                    if (!inModal) {
                        const ph = el.getAttribute('placeholder') || '';
                        let ctx = '';
                        let p = el.parentElement;
                        for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
                            ctx += (p.innerText || '') + ' ';
                        }
                        const inOverlay = !!el.closest('[class*="modal"], [class*="Modal"], '
                            + '[class*="dialog"], [class*="Drawer"], [class*="popup"]');
                        if (!inOverlay && !KW.test(ph) && !KW.test(ctx.substring(0, 200))) return false;
                    }
                    return true;
                };
                const mkField = (el, inModal) => ({
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    cls: (el.className || '').toString().substring(0, 100),
                    testid: el.getAttribute('data-testid') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    editable: el.isContentEditable === true || el.getAttribute('role') === 'textbox',
                    value: (el.value !== undefined ? el.value : (el.innerText || '')).substring(0, 80),
                    inModal: inModal,
                    outer: (el.outerHTML || '').substring(0, 320)
                });
                const fields = [];
                const fieldEls = [];
                const seen = new Set();
                for (const el of deepAll(root, FIELD_SEL)) {
                    if (seen.has(el) || !isReasonField(el, true)) continue;
                    seen.add(el); fields.push(mkField(el, true)); fieldEls.push(el);
                }
                if (!fields.length) {
                    for (const el of deepAll(document, FIELD_SEL)) {
                        if (root.contains(el) || seen.has(el) || !isReasonField(el, false)) continue;
                        seen.add(el); fields.push(mkField(el, false)); fieldEls.push(el);
                    }
                }
                let marked = false;
                if (opt && opt.mark && fieldEls.length) {
                    document.querySelectorAll('[data-pdd-reason-field]').forEach(
                        e => e.removeAttribute('data-pdd-reason-field'));
                    fieldEls[0].setAttribute('data-pdd-reason-field', '1');
                    marked = true;
                }

                // ---- 四个举报入口的真实状态（风控会把其中几个置灰 / 隐藏）----
                const radios = [];
                let checkedViewid = '';
                for (const vid of TYPE_VIEWIDS) {
                    const sel = 'input[data-tracking-viewid="' + vid + '"]';
                    const inp = root.querySelector(sel) || document.querySelector(sel);
                    if (!inp) {
                        radios.push({viewid: vid, present: false, visible: false,
                                     disabled: false, checked: false, label: ''});
                        continue;
                    }
                    const lab = inp.closest('label') || inp.parentElement;
                    const cls = ((lab && lab.className) || '').toString();
                    let pe = '';
                    try { pe = lab ? getComputedStyle(lab).pointerEvents : ''; } catch (e) {}
                    let ancestorBlocked = false;
                    let up = lab;
                    for (let k = 0; k < 3 && up; k++, up = up.parentElement) {
                        const c = ((up.className || '') + ' ' +
                            (up.getAttribute('data-disabled') || '') + ' ' +
                            (up.getAttribute('aria-disabled') || '')).toString();
                        if (/RD_disabled|RDG_disabled|is-disabled|disabled|tooltip-disabled/.test(c)) {
                            ancestorBlocked = true; break;
                        }
                    }
                    const disabled = !!inp.disabled || inp.getAttribute('disabled') !== null
                        || (lab && (lab.getAttribute('data-disabled') === 'true'
                                  || lab.getAttribute('aria-disabled') === 'true'))
                        || /RD_disabled|RDG_disabled|is-disabled|disabled/.test(cls)
                        || pe === 'none' || ancestorBlocked;
                    const checked = !!inp.checked || inp.getAttribute('checked') !== null
                        || (lab && lab.getAttribute('data-checked') === 'true')
                        || /RD_checked|is-checked/.test(cls);
                    const label = ((lab && (lab.innerText || lab.textContent)) || '')
                        .trim().substring(0, 40);
                    const rect = (lab || inp).getBoundingClientRect();
                    const visible = !!(lab && lab.offsetParent !== null
                                       && rect.width > 0 && rect.height > 0);
                    if (checked) checkedViewid = vid;
                    radios.push({viewid: vid, present: true, visible: visible,
                                 disabled: disabled, checked: checked, label: label,
                                 cls: cls.substring(0, 120)});
                }
                // viewid 被平台改掉时，仍能凭「已选中的 label 名称」认出当前入口
                if (!checkedViewid && root) {
                    for (const lab of root.querySelectorAll('label[data-testid="beast-core-radio"]')) {
                        if (lab.getAttribute('data-checked') !== 'true') continue;
                        const inp2 = lab.querySelector('input');
                        const vid2 = inp2 ? (inp2.getAttribute('data-tracking-viewid') || '') : '';
                        const n2 = ((lab.innerText || lab.textContent) || '').trim();
                        checkedViewid = vid2 || ('label:' + n2);
                        radios.push({viewid: checkedViewid, present: true, visible: true,
                                     disabled: false, checked: true,
                                     label: n2.substring(0, 40)});
                        break;
                    }
                }
                const typeChecked = !!WANT && (checkedViewid === WANT
                    || (!!WANT_NAME && checkedViewid === ('label:' + WANT_NAME)));

                const errors = [];
                for (const el of deepAll(root, '[class*="error"], [class*="Error"], '
                    + '[class*="tip"], [class*="notice"], [role="alert"]')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 100 && /请填写|必填|不能为空|至少|请输入|不可为空|未通过|请选择|不可用|不支持|失败|频繁|限制|上限|异常|错误/.test(t)) {
                        if (!errors.includes(t)) errors.push(t);
                    }
                }
                return {
                    found: true,
                    rootTag: root.tagName,
                    rootCls: (root.className || '').toString().substring(0, 80),
                    submitText: (btn.textContent || '').trim(),
                    typeChecked: typeChecked,
                    checkedViewid: checkedViewid,
                    radios: radios,
                    reasonFields: fields,
                    marked: marked,
                    errors: errors.slice(0, 8),
                    submitParams: btn.getAttribute('data-tracking-click-params') || '',
                    submitDisabled: !!btn.disabled || /disabled/.test((btn.className || '').toString())
                };
            }
        """ % (SUBMIT_BTN_SELECTOR, DIALOG_ROOT_SELECTOR,
               json.dumps(ALL_VIEWIDS, ensure_ascii=False))

    async def _read_dialog(self, viewid: str = ""):
        """读取弹窗状态，并按指定入口（默认当前激活入口）判定 typeChecked"""
        try:
            return await self.page.evaluate(
                self._dialog_js(),
                {"viewid": viewid or self._active_viewid,
                 "wantName": (self._active_plan or {}).get("name", "")})
        except Exception as e:
            print(f"   ⚠️ 读取弹窗状态失败: {e}")
            return {"found": False, "reasonFields": [], "typeChecked": False,
                    "checkedViewid": "", "radios": []}

    async def _dialog_state(self):
        """读取弹窗状态：是否存在 / 类型是否选中 / 弹窗内可编辑输入框清单 / 四个入口状态"""
        return await self._read_dialog(self._active_viewid)

    async def _wait_dialog(self, seconds=6):
        """等待真实举报弹窗出现（以「提交举报」按钮为准）"""
        for _ in range(int(seconds * 2)):
            st = await self._dialog_state()
            if st.get("found"):
                return st
            await asyncio.sleep(0.5)
        return {"found": False, "reasonFields": [], "typeChecked": False,
                "checkedViewid": "", "radios": []}

    # ---------- 四级举报入口：按优先级选中，风控不可选时自动降级 ----------

    def _sub_reason_js(self):
        """
        找 / 校验二级「举报原因」（如「购买 A 商品评价 B 商品」）。
        归一化：去所有空白与标点、全角字母转半角、统一大写，兼容平台各种排版。
        mode=find    → 标记最合适的可点击候选 data-pdd-sub-option="1"
        mode=verify  → 判断是否已有候选处于选中态
        """
        return r"""
            (opt) => {
                const norm = (s) => {
                    let t = (s || '').replace(/[\s\u3000]+/g, '')
                        .replace(/[（）()，,、。.：:；;【】\[\]"'`]/g, '');
                    t = t.replace(/[\uFF01-\uFF5E]/g,
                        c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
                    return t.toUpperCase();
                };
                const targets = (opt.aliases || []).map(norm).filter(Boolean);
                const hit = (txt) => {
                    const n = norm(txt);
                    if (!n || n.length > 80) return false;
                    for (const t of targets) {
                        if (n === t || n.includes(t)) return true;
                        if (n.length >= 6 && t.includes(n)) return true;
                    }
                    return false;
                };
                const btn = document.querySelector(
                    'button[data-tracking-viewid="new_confirm_report_btn"]');
                let root = null;
                for (const r of document.querySelectorAll('[data-testid="beast-core-modal"]')) {
                    if (btn && r.contains(btn)) { root = r; break; }
                }
                const scopes = [];
                if (root) scopes.push(root);
                scopes.push(document);
                const isChosen = (el) => {
                    if (!el) return false;
                    if (el.checked === true || el.selected === true) return true;
                    const a = el.getAttribute && el.getAttribute('aria-checked');
                    const s = el.getAttribute && el.getAttribute('aria-selected');
                    const d = el.getAttribute && el.getAttribute('data-checked');
                    if (a === 'true' || s === 'true' || d === 'true') return true;
                    const cls = ((el.className || '') + ' ' +
                        ((el.parentElement && el.parentElement.className) || '')).toString();
                    return /checked|selected|active|is-active/.test(cls);
                };
                const chosen = [], cands = [], seen = new Set();
                for (const sc of scopes) {
                    let all;
                    try {
                        all = sc.querySelectorAll('label, li, option, [role="option"], '
                            + '[role="radio"], [class*="option"], [class*="item"], '
                            + 'span, div, button, a');
                    } catch (e) { continue; }
                    for (const el of all) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent || '').join('').trim();
                        const txt = own || (el.innerText || el.textContent || '').trim();
                        if (!hit(txt) || el.closest('.mms-header_search_new_box')) continue;
                        const rect = el.getBoundingClientRect();
                        const visible = el.offsetParent !== null
                            && rect.width > 3 && rect.height > 3;
                        const rec = {el: el, txt: txt, len: txt.length,
                                     tag: el.tagName.toLowerCase(), visible: visible};
                        if (isChosen(el) || isChosen(el.parentElement)) chosen.push(rec);
                        if (visible) cands.push(rec);
                    }
                    for (const sel of sc.querySelectorAll('select')) {
                        const cur = sel.options[sel.selectedIndex];
                        if (cur && hit(cur.textContent || '')) {
                            chosen.push({el: sel, txt: cur.textContent.trim(), len: 0,
                                         tag: 'select', visible: true});
                        }
                        for (const o of sel.options) {
                            if (hit(o.textContent || '')) {
                                cands.push({el: sel, txt: o.textContent.trim(), len: 0,
                                            tag: 'select', visible: true});
                            }
                        }
                    }
                }
                if (opt.mode === 'verify') {
                    return {ok: chosen.length > 0,
                            matched: chosen.slice(0, 3).map(x => x.txt),
                            candidates: cands.length};
                }
                cands.sort((a, b) => a.len - b.len);
                document.querySelectorAll('[data-pdd-sub-option]').forEach(
                    e => { e.removeAttribute('data-pdd-sub-option');
                           e.removeAttribute('data-pdd-sub-option-text'); });
                if (chosen.length) {
                    return {ok: true, marked: false,
                            best: null, candidates: cands.length};
                }
                if (!cands.length) {
                    return {ok: false, marked: false, best: null, candidates: 0};
                }
                const best = cands[0];
                best.el.setAttribute('data-pdd-sub-option', '1');
                if (best.tag === 'select') {
                    best.el.setAttribute('data-pdd-sub-option-text', best.txt);
                }
                return {ok: false, marked: true,
                        best: {tag: best.tag, txt: best.txt},
                        candidates: cands.length};
            }
        """

    def _sub_reason_aliases(self, plan):
        base = (plan.get("sub_reason") or "").strip()
        if not base:
            return []
        return list(dict.fromkeys([base] + list(SUB_REASON_ALIASES.get(base, []))))

    async def _sub_reason_state(self, aliases):
        try:
            return await self.page.evaluate(
                self._sub_reason_js(), {"mode": "verify", "aliases": aliases})
        except Exception:
            return {"ok": False, "matched": [], "candidates": 0}

    async def _open_sub_reason_dropdown(self):
        """点开「请选择举报原因」这类下拉，让选项渲染出来"""
        js = """
            () => {
                const btn = document.querySelector(
                    'button[data-tracking-viewid="new_confirm_report_btn"]');
                let root = null;
                for (const r of document.querySelectorAll('[data-testid="beast-core-modal"]')) {
                    if (btn && r.contains(btn)) { root = r; break; }
                }
                if (!root) return false;
                const els = root.querySelectorAll('[role="combobox"], [class*="select"], '
                    + '[class*="Select"], [class*="dropdown"], [class*="picker"], select');
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (el.tagName === 'SELECT' || /请选择|举报原因|选择原因/.test(t)) {
                        el.click(); return true;
                    }
                }
                return false;
            }
        """
        try:
            return await self.page.evaluate(js)
        except Exception:
            return False

    async def _select_sub_reason(self, plan, review_index: int):
        """选中该入口要求的二级举报原因；返回 {ok, status, matched}"""
        sub = (plan.get("sub_reason") or "").strip()
        if not sub:
            return {"ok": True, "status": "not_needed"}
        aliases = self._sub_reason_aliases(plan)
        for round_no in range(3):
            st = await self.page.evaluate(
                self._sub_reason_js(), {"mode": "find", "aliases": aliases})
            if st.get("ok"):
                print(f"   [{review_index}] ✅ 二级举报原因已选中：{sub}")
                return {"ok": True, "status": "already_selected"}
            best = st.get("best") or {}
            if st.get("marked"):
                if best.get("tag") == "select":
                    await self.page.evaluate("""
                        () => {
                            const s = document.querySelector('[data-pdd-sub-option="1"]');
                            if (!s || s.tagName !== 'SELECT') return;
                            const want = s.getAttribute('data-pdd-sub-option-text');
                            for (const o of s.options) {
                                if (o.textContent.trim() === want) {
                                    s.value = o.value;
                                    s.dispatchEvent(new Event('change', {bubbles: true}));
                                    return;
                                }
                            }
                        }
                    """)
                else:
                    loc = self.page.locator('[data-pdd-sub-option="1"]').first
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                        await loc.click(timeout=3000)
                    except Exception:
                        await self.page.evaluate("""
                            () => {
                                const e = document.querySelector('[data-pdd-sub-option="1"]');
                                if (e) e.click();
                            }
                        """)
                await asyncio.sleep(0.45)
                v = await self._sub_reason_state(aliases)
                if v.get("ok"):
                    print(f"   [{review_index}] ✅ 已选择二级举报原因：{sub}"
                          f"（命中 {v.get('matched')}）")
                    return {"ok": True, "status": "selected", "matched": v.get("matched")}
            opened = await self._open_sub_reason_dropdown()
            if not opened and round_no >= 1:
                break
            await asyncio.sleep(0.35)
        print(f"   [{review_index}] ⚠️ 找不到二级举报原因「{sub}」，本入口放弃")
        return {"ok": False, "status": "not_found"}

    async def _clear_reason_in_dialog(self):
        """清空弹窗内描述框，避免上一等级残留文字被带到下一等级"""
        js = """
            () => {
                const btn = document.querySelector(
                    'button[data-tracking-viewid="new_confirm_report_btn"]');
                let root = null;
                for (const r of document.querySelectorAll('[data-testid="beast-core-modal"]')) {
                    if (btn && r.contains(btn)) { root = r; break; }
                }
                if (!root) return 0;
                let n = 0;
                const els = root.querySelectorAll('textarea, input[type="text"], '
                    + 'input:not([type]), [contenteditable="true"]');
                for (const el of els) {
                    if (el.offsetParent === null || el.disabled || el.readOnly) continue;
                    if (el.closest('.mms-header_search_new_box')) continue;
                    if (el.isContentEditable === true) {
                        el.textContent = '';
                        el.dispatchEvent(new InputEvent('input', {bubbles: true, data: ''}));
                    } else {
                        const proto = el.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, '');
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    n++;
                }
                return n;
            }
        """
        try:
            return await self.page.evaluate(js)
        except Exception:
            return 0

    async def _click_type(self, plan, review_index: int):
        """点击指定入口的 radio label，返回 (是否点到, 原因)"""
        vid = plan["viewid"]
        sel = TYPE_RADIO_SELECTOR_TPL % vid
        lab = self.page.locator(
            'label[data-testid="beast-core-radio"]:has(' + sel + ')')
        if await lab.count() == 0:
            lab = self.page.locator(sel)
        if await lab.count() == 0:
            # 平台改了 viewid 时，按入口名称兜底
            lab = self.page.get_by_text(plan["name"], exact=True)
            if await lab.count() == 0:
                return False, "absent"
        target = lab.first
        try:
            if not await target.is_visible():
                return False, "hidden"
            await target.scroll_into_view_if_needed(timeout=2000)
            await target.click(timeout=3000)
            return True, "clicked"
        except Exception as e:
            return False, "click_failed:" + str(e)[:60]

    async def _apply_plan(self, plan, review_index: int):
        """
        尝试应用某一级举报入口：检查可见/禁用 → 点选 → 复核真实选中态 → 二级举报原因。
        返回 {ok, why, ...}，ok=False 时由上层自动降级到下一级。
        """
        vid = plan["viewid"]
        self._active_viewid = vid
        self._active_plan = plan
        st0 = await self._read_dialog(vid)
        if not st0.get("found"):
            return {"ok": False, "why": "no_dialog"}

        radio = None
        for r in (st0.get("radios") or []):
            if r.get("viewid") == vid:
                radio = r
                break
        if radio and not radio.get("present"):
            return {"ok": False, "why": "absent"}
        if radio and not radio.get("visible"):
            return {"ok": False, "why": "hidden"}
        if radio and radio.get("disabled"):
            return {"ok": False, "why": "disabled_by_risk"}

        if not st0.get("typeChecked"):
            clicked, how = await self._click_type(plan, review_index)
            if not clicked:
                # viewid 可能被平台改过，再按入口名称做一次文本兜底
                if how in ("absent", "hidden") and await self.select_report_type(review_index):
                    pass
                else:
                    return {"ok": False, "why": how}
            await asyncio.sleep(0.5)

        st = st0
        for _ in range(6):
            st = await self._read_dialog(vid)
            if st.get("typeChecked"):
                break
            await asyncio.sleep(0.35)
        if not st.get("typeChecked"):
            return {"ok": False, "why": "not_checked",
                    "checked": st.get("checkedViewid")}

        sub = await self._select_sub_reason(plan, review_index)
        if not sub["ok"]:
            return {"ok": False, "why": "sub_reason:" + sub["status"]}

        self._type_confidence = "confirmed"
        print(f"   [{review_index}] ✅ 第 {plan['rank']} 级入口「{plan['name']}」已选中"
              f"（viewid={vid}）")
        return {"ok": True, "why": "selected", "name": plan["name"],
                "viewid": vid, "sub": sub,
                "submitParams": st.get("submitParams")}

    async def _select_type_exact(self, review_index: int):
        """兼容旧调用：尝试选中当前激活的入口"""
        plan = self._active_plan or (REPORT_TYPE_PLAN[0] if REPORT_TYPE_PLAN else None)
        if not plan:
            return False
        res = await self._apply_plan(plan, review_index)
        return bool(res.get("ok"))

    async def _fill_reason_in_dialog(self, text: str = ""):
        """
        只在举报弹窗内填写详细描述，绝不触碰页面顶部筛选框等其它输入框。
        优先用 Playwright 真实键盘输入（React 事件最完整），失败再用 native setter 兜底。
        返回 {status, value, field, errors}，status ∈ filled / no_field / no_dialog / failed
        """
        want = text if text else REPORT_REASON
        try:
            st = await self.page.evaluate(self._dialog_js(), {"mark": True})
        except Exception as e:
            print(f"   ⚠️ 读取弹窗字段失败: {e}")
            return {"status": "failed", "value": "", "field": None, "errors": []}
        if not st.get("found"):
            return {"status": "no_dialog", "value": "", "field": None, "errors": []}
        fields = st.get("reasonFields") or []
        if not fields or not st.get("marked"):
            return {"status": "no_field", "value": "", "field": None,
                    "errors": st.get("errors") or []}
        field = fields[0]
        try:
            loc = self.page.locator('[data-pdd-reason-field="1"]').first
            await loc.fill(want, timeout=4000)
        except Exception as e:
            print(f"   ⚠️ 真实输入失败，改用 JS 兜底: {str(e)[:100]}")
            try:
                await self.page.evaluate("""
                    (reason) => {
                        const el = document.querySelector('[data-pdd-reason-field="1"]');
                        if (!el) return;
                        el.focus();
                        if (el.isContentEditable === true) {
                            el.textContent = reason;
                            el.dispatchEvent(new InputEvent('input', {bubbles: true, data: reason}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        } else {
                            const proto = el.tagName === 'TEXTAREA'
                                ? window.HTMLTextAreaElement.prototype
                                : window.HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                            setter.call(el, reason);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }
                """, want)
            except Exception as e2:
                print(f"   ⚠️ JS 兜底也失败: {str(e2)[:100]}")
        readback = ""
        try:
            readback = await self.page.evaluate("""
                () => {
                    const el = document.querySelector('[data-pdd-reason-field="1"]');
                    if (!el) return '';
                    return (el.isContentEditable === true
                        ? (el.innerText || '') : (el.value || '')).trim();
                }
            """)
            await self.page.evaluate("""
                () => document.querySelectorAll('[data-pdd-reason-field]')
                    .forEach(e => e.removeAttribute('data-pdd-reason-field'))
            """)
        except Exception as e:
            print(f"   ⚠️ 回读描述失败: {e}")
        ok = (readback == want)
        return {"status": "filled" if ok else "failed", "value": readback[:60],
                "field": field, "errors": st.get("errors") or []}

    # ---------- 举报类型选择（兼容旧调用，按当前激活的入口名做兜底） ----------

    async def _type_selected(self):
        """当前激活入口是否已真实选中（以 radio 的 checkedViewid 为准）"""
        st = await self._read_dialog(self._active_viewid)
        return bool(st.get("typeChecked"))

    async def select_report_type(self, review_index: int):
        """
        通用兜底：平台改了 data-tracking-viewid 时，按当前等级入口的名称做文本定位。
        不再硬编码「同行恶意差评」；只有读回真实选中态才返回 True。
        """
        plan = self._active_plan or (REPORT_TYPE_PLAN[0] if REPORT_TYPE_PLAN else None)
        if not plan:
            return False
        name = plan["name"]
        await asyncio.sleep(0.3)
        if await self._type_selected():
            self._type_confidence = "confirmed"
            return True

        # 策略 1：展开「请选择举报类型 / 举报原因」下拉
        try:
            await self.page.evaluate("""
                () => {
                    for (const t of document.querySelectorAll(
                        '[class*="select"], [class*="dropdown"], [role="combobox"], [class*="picker"]')) {
                        if (t.offsetParent === null) continue;
                        const txt = (t.textContent || '').trim();
                        if (txt.includes('请选择') || txt.includes('举报类型') || txt.includes('举报原因')) {
                            t.click(); return;
                        }
                    }
                }
            """)
            await asyncio.sleep(0.4)
        except Exception:
            pass

        # 策略 2：按入口名称找最小可点击元素
        keywords = list(dict.fromkeys([name, name[:4], name[-4:]]))
        for keyword in keywords:
            if not keyword:
                continue
            try:
                loc = self.page.get_by_text(keyword, exact=False)
                for j in range(min(await loc.count(), 20)):
                    el = loc.nth(j)
                    try:
                        if not await el.is_visible():
                            continue
                        txt = (await el.inner_text() or "").strip()
                        if not txt or len(txt) > 30:
                            continue
                        for candidate in (el, el.locator("..").first):
                            try:
                                await candidate.scroll_into_view_if_needed()
                                await candidate.click(timeout=2500)
                                await asyncio.sleep(0.4)
                                if await self._type_selected():
                                    self._type_confidence = "confirmed"
                                    print(f"   [{review_index}] ✅ 文本兜底选中第 "
                                          f"{plan['rank']} 级入口「{name}」")
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        continue
            except Exception:
                continue

        # 策略 3：原生 <select>
        try:
            for sel in await self.page.locator("select").all():
                if not await sel.is_visible():
                    continue
                for opt in await sel.locator("option").all_inner_texts():
                    if name in opt or (len(name) > 3 and name[:3] in opt):
                        await sel.select_option(label=opt.strip())
                        await asyncio.sleep(0.3)
                        if await self._type_selected():
                            self._type_confidence = "confirmed"
                            return True
        except Exception:
            pass

        # 策略 4：JS 全页面兜底
        try:
            hit = await self.page.evaluate("""
                (name) => {
                    let best = null, bestLen = Infinity;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t !== name || t.length > 30) continue;
                        const r = el.getBoundingClientRect();
                        if (t.length < bestLen && r.width > 5 && r.height > 5) {
                            best = el; bestLen = t.length;
                        }
                    }
                    if (best) { best.click(); return name; }
                    return null;
                }
            """, name)
            if hit:
                await asyncio.sleep(0.4)
                if await self._type_selected():
                    self._type_confidence = "confirmed"
                    return True
        except Exception:
            pass

        print(f"   [{review_index}] ⚠️ 文本兜底也没选中第 {plan['rank']} 级入口「{name}」")
        return False

    async def _close_dialog(self):
        """关闭当前举报弹窗：优先精确点「取消」，避免误点页面其它 close 元素"""
        try:
            js = """
                () => {
                    const SEL_CANCEL = %r;
                    const c = document.querySelector(SEL_CANCEL);
                    if (c && c.offsetParent !== null) { c.click(); return; }
                    const btns = document.querySelectorAll(
                        '[class*="close"], [class*="cancel"], .modal-close, .dialog-close, [aria-label="关闭"]');
                    for (const b of btns) {
                        if (b.offsetParent !== null) { b.click(); return; }
                    }
                    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                }
            """ % CANCEL_BTN_SELECTOR
            await self.page.evaluate(js)
            await asyncio.sleep(0.3)
        except Exception:
            pass

    async def _verify_submit(self):
        """提交后校验：成功提示 > 弹窗仍开 > 仅弹窗关闭（不冒充成功）"""
        try:
            js = """
                () => {
                    const SEL_SUBMIT = %r;
                    const t = document.body.innerText || '';
                    if (/举报成功|提交成功|已提交|举报已受理|受理成功/.test(t)) return 'success_toast';
                    // 风控 / 入口不可用等明确拒绝：不能当成「弹窗关闭=已提交」写台账
                    for (const el of document.querySelectorAll(
                        '[class*="toast"], [class*="Toast"], [class*="message"], [class*="notification"], [role="alert"]')) {
                        if (el.offsetParent === null) continue;
                        const tx = (el.textContent || '').trim();
                        if (tx && tx.length < 120 && /不可用|不支持|失败|频繁|限制|上限|请勿|异常|错误/.test(tx)) {
                            return 'error_toast';
                        }
                    }
                    for (const el of document.querySelectorAll(
                        '[class*="toast"], [class*="Toast"], [class*="message"], [class*="notification"], [role="alert"]')) {
                        if (el.offsetParent !== null && /成功|已提交|已受理/.test(el.textContent || '')) return 'success_toast';
                    }
                    const btn = document.querySelector(SEL_SUBMIT);
                    if (btn && btn.offsetParent !== null) return 'dialog_still_open';
                    for (const d of document.querySelectorAll(
                        '[class*="dialog"], [class*="modal"], [data-testid="beast-core-modal"]')) {
                        const r = d.getBoundingClientRect();
                        if (r.width > 100 && r.height > 50 && d.offsetParent !== null) return 'dialog_still_open';
                    }
                    return 'dialog_closed';
                }
            """ % SUBMIT_BTN_SELECTOR
            return await self.page.evaluate(js)
        except Exception:
            return 'unknown'

    async def _probe_all_plans(self, review_index: int):
        """探测入口可用性：单入口模式下仍列出四级状态，便于确认风控范围（绝不提交）"""
        plans = ALL_REPORT_TYPES if SINGLE_ENTRY_ONLY else REPORT_TYPE_PLAN
        print("\n🔬 入口可用性探测（逐个点选，绝不提交；正式提交只用「%s」）"
              % REPORT_TYPE)
        results = []
        for plan in plans:
            self._active_viewid = plan["viewid"]
            self._active_plan = plan
            before = await self._read_dialog(plan["viewid"])
            radio = {}
            for r in (before.get("radios") or []):
                if r.get("viewid") == plan["viewid"]:
                    radio = r
                    break
            applied = await self._apply_plan(plan, review_index)
            await asyncio.sleep(0.3)
            after = await self._read_dialog(plan["viewid"])
            rec = {
                "rank": plan["rank"], "name": plan["name"], "viewid": plan["viewid"],
                "active": plan["viewid"] in TYPE_VIEWIDS,
                "reason_mode": plan["reason"], "sub_reason": plan["sub_reason"],
                "present": radio.get("present"), "visible": radio.get("visible"),
                "disabled": radio.get("disabled"),
                "selectable": bool(applied.get("ok")), "why": applied.get("why"),
                "checkedViewid": after.get("checkedViewid"),
                "submitParams": after.get("submitParams"),
                "reasonFields": after.get("reasonFields") or [],
                "errors": after.get("errors") or [],
            }
            results.append(rec)
            flag = "可用" if rec["selectable"] else "不可用"
            print("   [%d] 第 %d 级 %-8s %-5s present=%s visible=%s disabled=%s why=%s"
                  % (review_index, plan["rank"], plan["name"], flag,
                     rec["present"], rec["visible"], rec["disabled"], rec["why"]))
        out = os.path.join(LOG_DIR, "probe_plans.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"   📄 四级入口探测结果: {os.path.basename(out)}")
        except Exception as e:
            print(f"   ⚠️ 探测结果写入失败: {e}")
        return results

    async def _prepare_reason(self, plan, review_index: int):
        """按等级要求处理描述框；返回 {ok, status, reason_status}"""
        mode = plan.get("reason") or "fill"
        # 全局开关：设为 False 时，fill 入口也降级为 auto（允许不带描述提交）
        if not REQUIRE_REASON_FIELD and mode == "fill":
            mode = "auto"
        text = plan.get("text") or ""
        # 每换一个入口先清空，防止上一等级残留文字被带过去
        await self._clear_reason_in_dialog()
        if mode == "none":
            print(f"   [{review_index}] 📝 第 {plan['rank']} 级按设置不填内容，直接提交")
            return {"ok": True, "status": "skipped", "reason_status": "none"}

        field_state = await self._wait_reason_field()
        n_fields = len(field_state.get("reasonFields") or [])
        if n_fields:
            _fd = field_state["reasonFields"][0]
            print(f"   [{review_index}] 📝 检测到描述输入框 <{_fd.get('tag')}> "
                  f"placeholder={_fd.get('placeholder')!r}")
        else:
            print(f"   [{review_index}] ⚠️ 选完类型后未发现描述输入框（等了 "
                  f"{REASON_FIELD_WAIT_SECONDS} 秒）")
            if mode == "auto":
                print(f"   [{review_index}]    ↳ 第 {plan['rank']} 级为 auto 模式，"
                      f"无描述框也继续（不填内容）")
                return {"ok": True, "status": "no_field", "reason_status": "none"}

        rs = await self._fill_reason_in_dialog(text)
        if rs["status"] != "filled":
            print(f"   [{review_index}] ⛔ 第 {plan['rank']} 级描述填写失败（{rs['status']}）")
            if rs.get("errors"):
                print(f"   [{review_index}]    平台提示: {rs['errors']}")
            return {"ok": False, "status": rs["status"],
                    "reason_status": "failed", "errors": rs.get("errors")}
        print(f"   [{review_index}] ✅ 描述已填写并回读一致（{text[:18]}…）")
        return {"ok": True, "status": "filled", "reason_status": "filled"}

    async def _submit_with_verify(self, plan, review_index: int, review_key: str,
                                  reason_status: str):
        """点提交并校验；返回 (是否确认成功, 是否应降级, 是否命中风控)"""
        rank = plan["rank"]
        for attempt in range(3):
            st_before = await self._dialog_state()
            if st_before.get("submitParams"):
                print(f"   [{review_index}] 🔘 提交按钮参数: {st_before['submitParams']}")
            if st_before.get("submitDisabled"):
                print(f"   [{review_index}] ⚠️ 提交按钮当前是禁用态（多半还有必填项没填）")

            submit_btn = self.page.locator(SUBMIT_BTN_SELECTOR).first
            if await submit_btn.count() == 0 or not await submit_btn.is_visible():
                submit_btn = self.page.locator(
                    DIALOG_ROOT_SELECTOR + " button:has-text('提交举报')").first
            try:
                if await submit_btn.count() == 0 or not await submit_btn.is_visible():
                    raise RuntimeError("提交按钮不可见")
                await submit_btn.click(timeout=4000)
            except Exception as e:
                print(f"   [{review_index}] ⚠️ 提交点击失败（尝试 {attempt+1}/3）: {str(e)[:100]}")
                await asyncio.sleep(0.8)
                continue

            await asyncio.sleep(1.2)
            verify = await self._verify_submit()
            if verify == "dialog_closed":
                # toast 可能稍晚出现，多等几拍再判「未确认」
                for _ in range(4):
                    await asyncio.sleep(0.4)
                    v2 = await self._verify_submit()
                    if v2 == "success_toast":
                        verify = v2
                        break
            if verify == "error_toast":
                print(f"   [{review_index}] ⛔ 平台明确拒绝本次提交（风控 / 当日额度用完），"
                      f"不写台账、本条停止")
                self.stats["platform_rejected"] = self.stats.get("platform_rejected", 0) + 1
                self._write_log(self._current_star or 0, review_index, review_key,
                                "blocked", "rank=%d %s 平台拒绝: error_toast"
                                % (rank, plan["name"]))
                return False, False, True
            if verify == "dialog_still_open":
                st_err = await self._dialog_state()
                if st_err.get("errors"):
                    print(f"   [{review_index}] ⚠️ 平台校验提示: {st_err['errors']}")
                if is_risk_signal("", " ".join(st_err.get("errors") or [])):
                    print(f"   [{review_index}] ⛔ 校验提示中出现当日上限 / 风控文案")
                    return False, False, True
                print(f"   [{review_index}] ⚠️ 第 {rank} 级提交后弹窗仍在（校验未通过），"
                      f"重试 {attempt+1}/3")
                if reason_status == "filled":
                    _re = await self._fill_reason_in_dialog(plan.get("text") or "")
                    print(f"   [{review_index}]    ↳ 重填结果: {_re['status']}")
                await asyncio.sleep(0.8)
                continue

            if verify == "success_toast":
                status = "reported"
                self.stats["total_reported"] += 1
                confirmed = True
                print(f"   [{review_index}] ✅ 第 {rank} 级「{plan['name']}」举报成功")
            elif verify == "dialog_closed" and not STRICT_SUBMIT_VERIFY:
                status = "reported_unverified"
                confirmed = True
                print(f"   [{review_index}] ✅ 已提交（宽松模式，未见成功提示）")
            else:
                status = "submitted_unverified"
                confirmed = False
                self.stats["submitted_unverified"] = self.stats.get("submitted_unverified", 0) + 1
                print(f"   [{review_index}] ⚠️ 弹窗关闭但未见成功提示 → 待人工核对，"
                      f"已记台账防止重复点击")

            # 只要真的点了提交就写台账，避免下一轮重复点同一条
            if review_key:
                self._mark_reported(review_key)
            self._write_log(self._current_star or 0, review_index, review_key,
                            status,
                            "verify=%s reason=%s rank=%d entry=%s type=confirmed"
                            % (verify, reason_status, rank, plan["name"]))
            if confirmed:
                self._clear_risk_streak()
            return confirmed, False, False

        print(f"   [{review_index}] ⛔ 第 {rank} 级连续 3 次校验未通过，判定平台已拒绝")
        return False, FALLBACK_ON_SUBMIT_REJECT, True

    async def report_single_review(self, review_element, review_index: int, review_key: str = ""):
        """按 1→2→3→4 优先级举报单个评价；高优先级入口被风控封掉时自动降级"""
        try:
            # 先滚动到该评价可见
            await self.scroll_element_into_view(review_element)

            # 查找举报按钮
            report_btn = await self.find_report_button(review_element)

            if report_btn is True:
                # find_report_button 已经通过坐标点击了
                print(f"   [{review_index}] 🖱️ 举报按钮已点击（坐标方式），等待弹窗...")
            elif report_btn is None:
                print(f"   [{review_index}] ⚠️ 未找到举报按钮，跳过")
                return False
            else:
                # 返回的是 Playwright Locator，需要点击
                print(f"   [{review_index}] 🖱️ 正在点击举报按钮（选择器方式）...")
                await self.safe_click(report_btn)

            # ===== 等待真实举报弹窗（以「提交举报」按钮为准，不再靠猜 class） =====
            dialog_state = await self._wait_dialog(6)
            if not dialog_state.get("found"):
                print(f"   [{review_index}] ⚠️ 未检测到举报弹窗（找不到「提交举报」按钮），跳过本条")
                self.stats["errors"] += 1
                self._write_log(self._current_star or 0, review_index, review_key,
                                "failed", "未检测到举报弹窗")
                return False
            print(f"   [{review_index}] 📋 举报弹窗已出现（按钮：{dialog_state.get('submitText')}）")
            print(f"   [{review_index}] 📝 打开时可见输入框: "
                  f"{len(dialog_state.get('reasonFields') or [])} 个")
            radios = dialog_state.get("radios") or []
            if radios:
                brief = " / ".join(
                    "%s:%s" % (r.get("label") or r.get("viewid"),
                               "禁用" if r.get("disabled") else
                               ("隐藏" if not r.get("visible") else "可选"))
                    for r in radios)
                print(f"   [{review_index}] 🔘 四入口状态: {brief}")

            # ===== 平台若已直接提示「今日次数已达上限」，立即暂停，不再尝试提交 =====
            # 仅单入口模式启用；--multi-entry 是显式指定的排障模式，仍按旧降级链走
            risk_text = await self._scan_risk_text()
            if (SINGLE_ENTRY_ONLY and risk_text
                    and is_risk_signal("", risk_text)):
                print(f"   [{review_index}] ⛔ 平台提示命中风控文案: {risk_text[:160]}")
                if not PROBE_MODE:
                    await self._pause_for_daily_limit(
                        "risk_text", risk_text, review_index, review_key)
                    return False

            # ===== 探测模式：四级入口逐个导出可用状态，绝不提交 =====
            if PROBE_MODE:
                await self.probe_dialog()
                await self._probe_all_plans(review_index)
                self._active_viewid = TYPE_VIEWIDS[0] if TYPE_VIEWIDS else ""
                self._active_plan = REPORT_TYPE_PLAN[0] if REPORT_TYPE_PLAN else {}
                await self._export_probe_after_type(
                    review_index,
                    do_fill=(self._active_plan or {}).get("reason") != "none")
                self._probe_done = True
                return False

            # ===== 单入口：只走「同行恶意差评」；风控一律暂停，绝不换入口 =====
            tried = []
            for plan in REPORT_TYPE_PLAN:
                rank = plan["rank"]
                self._type_confidence = "none"
                if tried:
                    self.stats["fallback_used"] += 1
                    print(f"   [{review_index}] ↘️ 降级到第 {rank} 级「{plan['name']}」"
                          "（仅 --multi-entry 模式会出现）")

                sel = await self._apply_plan(plan, review_index)
                if not sel.get("ok"):
                    why = str(sel.get("why"))
                    print(f"   [{review_index}] ⛔ 「{plan['name']}」不可用（{why}）")
                    tried.append("%d:%s/type:%s" % (rank, plan["name"], why))
                    # 入口被置灰 / 隐藏 / 拒绝 = 当日额度用完，立即暂停并提醒
                    # （--multi-entry 排障模式不暂停，继续走旧降级链）
                    if SINGLE_ENTRY_ONLY and is_risk_signal(why) and RISK_STOP_ENABLED:
                        paused = await self._maybe_risk_pause(
                            why, "入口状态=" + why, review_index, review_key)
                        if paused:
                            return False
                    self.stats["blocked_no_type"] += 1
                    self._write_log(self._current_star or 0, review_index, review_key,
                                    "blocked", "rank=%d %s 入口不可用（不降级）: %s"
                                    % (rank, plan["name"], why))
                    if not FALLBACK_ON_BLOCKED:
                        break
                    continue

                # 能正常选中入口，说明额度还在，清零风控连续计数
                self._clear_risk_streak()

                prep = await self._prepare_reason(plan, review_index)
                if not prep.get("ok"):
                    # 描述填不进属于页面/填写问题，不算风控：跳过本条，不暂停、不降级
                    self.stats["blocked_no_reason"] = self.stats.get("blocked_no_reason", 0) + 1
                    tried.append("%d:%s/reason:%s"
                                 % (rank, plan["name"], prep.get("status")))
                    self._write_log(self._current_star or 0, review_index, review_key,
                                    "blocked", "rank=%d %s 描述失败: %s / %s"
                                    % (rank, plan["name"], prep.get("status"),
                                       prep.get("errors")))
                    await self._export_probe_after_type(
                        review_index, tag="no_reason_rank%d" % rank,
                        do_fill=plan.get("reason") != "none")
                    if not FALLBACK_ON_BLOCKED:
                        break
                    continue

                # ===== 试运行：只验证到「选中 + 能填」，绝不提交 =====
                if DRY_RUN:
                    print(f"   [{review_index}] 🧪 试运行：「{plan['name']}」"
                          f" 类型={self._type_confidence} 描述={prep['status']}，未提交")
                    self._write_log(self._current_star or 0, review_index, review_key,
                                    "dry_ok", "rank=%d entry=%s reason=%s 试运行未提交"
                                    % (rank, plan["name"], prep["status"]))
                    await self._close_dialog()
                    return False

                confirmed, fallback, risk = await self._submit_with_verify(
                    plan, review_index, review_key, prep["reason_status"])
                if SINGLE_ENTRY_ONLY and risk and RISK_STOP_ENABLED:
                    await self._pause_for_daily_limit(
                        "submit_rejected",
                        "入口=%s 平台拒绝提交/连续校验未通过" % plan["name"],
                        review_index, review_key)
                    return False
                if confirmed:
                    self.plan_stats[rank] = self.plan_stats.get(rank, 0) + 1
                    await asyncio.sleep(1)
                    await self._close_dialog()
                    return True
                if fallback:
                    tried.append("%d:%s/submit_rejected" % (rank, plan["name"]))
                    self._write_log(self._current_star or 0, review_index, review_key,
                                    "fallback", "rank=%d %s 平台校验未通过，降级"
                                    % (rank, plan["name"]))
                    continue
                # 弹窗已关闭、结果不明：绝不换入口重提，避免同一条被举报两次
                await asyncio.sleep(1)
                await self._close_dialog()
                return False

            # 走到这里 = 单入口未成功（但并非风控，风控已在上面 return）
            print(f"   [{review_index}] ❌ 「{REPORT_TYPE}」本次未提交："
                  f"{' → '.join(tried) if tried else '无可用入口'}")
            self._write_log(self._current_star or 0, review_index, review_key,
                            "blocked",
                            "单入口未提交（不降级）: " + " | ".join(tried))
            await self._export_probe_after_type(
                review_index, tag="single_entry_failed", do_fill=False)
            await self._close_dialog()
            return False

        except Exception as e:
            print(f"   [{review_index}] ❌ 举报异常: {e}")
            import traceback
            traceback.print_exc()
            self.stats["errors"] += 1
            return False

    async def process_reviews_on_page(self, star: int = 0):
        """处理当前页面的所有评价（含滚动加载与星级复核）"""
        print("\n📋 正在扫描当前页面评价...")

        # 等待初始内容加载
        await asyncio.sleep(PAGE_WAIT_SECONDS)

        # ===== 滚动页面加载所有懒加载内容 =====
        print("   🔄 滚动页面以加载所有评价...")
        prev_height = 0
        for _ in range(8):  # 最多滚动 8 次
            await self.scroll_page_down(800)
            await asyncio.sleep(0.5)
            new_height = await self.page.evaluate("document.body.scrollHeight")
            if new_height == prev_height:
                break
            prev_height = new_height
        await self.scroll_to_bottom()
        await asyncio.sleep(1)
        # 回到顶部开始处理
        await self.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # ===== 诊断：打印当前页面信息 =====
        try:
            url = self.page.url
            title = await self.page.title()
            body_text = await self.page.evaluate("document.body.innerText.substring(0, 500)")
            print(f"   📍 URL: {url}")
            print(f"   📄 Title: {title}")
            print(f"   📝 页面快照(前500字): {body_text[:200]}...")
        except Exception:
            pass

        # ===== 检测空状态 =====
        empty_found = False
        for selector in SELECTORS["no_more_data"].split(", "):
            try:
                no_data = self.page.locator(selector.strip()).first
                if await no_data.count() > 0 and await no_data.is_visible():
                    txt = await no_data.inner_text()
                    print(f"   📭 检测到空状态: 「{txt.strip()[:50]}」- 该星级暂无评价，跳过")
                    empty_found = True
                    break
            except Exception:
                continue
        if empty_found:
            return 0

        # ===== 用 DOM 打标记精确定位评价卡片（不再靠猜 class 或坐标） =====
        cards = await self._collect_review_cards()
        if not cards:
            print("   📭 本页没有可举报卡片（多为「已举报，待审核」，操作列不再显示「举报」）")
            if self._page_num <= 1:
                await self._export_page_structure("no_cards")
            return 0

        review_items = [self.page.locator('[data-pdd-review="' + str(c['idx']) + '"]').first
                        for c in cards]
        total_on_page = len(review_items)
        with_btn = sum(1 for c in cards if c['hasBtn'])
        print(f"   ✅ 识别到 {total_on_page} 条评价卡片（{with_btn} 条带举报入口）")
        _sample = re.sub(r'\s+', ' ', cards[0]['text'])[:100]
        print(f"   📋 首条样本: {_sample}")
        # ===== 逐条处理 =====
        reported_count = 0
        for i, item in enumerate(review_items):
            if self.daily_limit_reached:
                print("   ⏸️ 已到达当日举报上限，停止处理本页剩余评价")
                break
            try:
                if self._probe_done:
                    break
                # 跳过不可见元素
                if not await item.is_visible():
                    continue

                # 提取唯一键并去重（防止重复举报同一条评价）
                item_text = (await item.inner_text() or "").strip()
                key = self._review_key(item_text)
                if key in self.reported_ids:
                    self.stats["skipped_duplicate"] += 1
                    print(f"   [{i + 1}] ⏭️ 已举报过，跳过 ({key})")
                    continue

                # 关键防线：即使筛选残留，也绝不误报非目标星级的评价
                if VERIFY_STAR_IN_CARD and star:
                    ok_star, why = self._card_star_matches(item_text, star)
                    if not ok_star:
                        print(f"   [{i + 1}] ⏭️ 非 {star} 星，跳过（{why}）")
                        continue

                self.stats["total_checked"] += 1
                preview = re.sub(r"\s+", " ", item_text)[:120]
                print(f"   [{i + 1}] 📄 评价摘要: {preview}")

                # 先滚动到该元素
                await self.scroll_element_into_view(item)
                await asyncio.sleep(0.3)

                result = await self.report_single_review(item, i + 1, key)
                if result:
                    reported_count += 1

                if self.daily_limit_reached:
                    break   # 不再等待、不再碰下一条
                # 随机延时，模拟人工节奏，降低风控触发概率
                await asyncio.sleep(random.uniform(MIN_ACTION_DELAY, MAX_ACTION_DELAY))

            except Exception as e:
                print(f"   [{i + 1}] 处理异常: {e}")
                continue

        if self.daily_limit_reached:
            print("   ⏸️ 本页提前结束（当日举报上限），未处理的评价明天会自动重试")
        else:
            print(f"\n   本页处理完毕: 举报 {reported_count} / 检查 {total_on_page} 条")
        return total_on_page

    async def go_to_next_page(self):
        """翻到下一页：数字页码优先、箭头兜底，并校验页面内容真的变了"""
        # 滚动到页面底部，确保分页组件可见
        await self.scroll_to_bottom()
        await asyncio.sleep(1)

        # ===== 主逻辑：以真实分页组件 data-status 的当前页码为准 =====
        pager = await self._pager_state()
        cur = (pager.get("page") if pager.get("found") else None) or self._page_num
        target = cur + 1
        if pager.get("found") and pager.get("nextDisabled"):
            print(f"   🏁 下一页按钮已禁用，当前第 {cur} 页即末页（{pager.get('totalText')}）")
            return False
        print("   📄 翻页：第 %s 页 → 第 %s 页%s"
              % (cur, target, ("（" + pager["totalText"] + "）") if pager.get("totalText") else ""))
        self._page_num = cur

        # 清掉上一页残留标记，避免指纹与点击落到旧 DOM 上
        await self.page.evaluate("""
            () => document.querySelectorAll('[data-pdd-review],[data-pdd-report-btn],[data-pdd-next]')
                .forEach(e => { e.removeAttribute('data-pdd-review');
                                e.removeAttribute('data-pdd-report-btn');
                                e.removeAttribute('data-pdd-next'); })
        """)

        before = await self._page_signature()
        marker = await self.page.evaluate("""
            (num) => {
                const SEL_PAGER = %r;
                const SEL_UL = %r, SEL_NEXT = %r;
                document.querySelectorAll('[data-pdd-next]').forEach(e => e.removeAttribute('data-pdd-next'));
                // 真实分页组件的「下一页」按钮最可靠：窗口常只显示 1-5 … 末页，数字未必可见
                const ul = document.querySelector(SEL_UL);
                if (ul) {
                    const nx = ul.querySelector(SEL_NEXT);
                    if (nx && !/disabled/i.test(nx.className || '')) {
                        nx.setAttribute('data-pdd-next', 'real-next');
                        return {method: 'real-next'};
                    }
                }
                const s = String(num);
                const cand = [];
                const pagerItems = [];
                for (const p of document.querySelectorAll(SEL_PAGER)) {
                    if (p.offsetParent !== null) pagerItems.push(p);
                }
                if (pagerItems.length) {
                    // 真实分页：li[class*="PGT_pagerItem_"]，只在这些元素里找页码，避免误点其它数字
                    for (const p of pagerItems) {
                        if ((p.textContent || '').trim() === s) {
                            const r = p.getBoundingClientRect();
                            if (r.width >= 5 && r.height >= 5) cand.push({el: p, y: r.y});
                        }
                    }
                } else {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.offsetParent === null) continue;
                        if (el.children.length > 0) continue;
                        if ((el.textContent || '').trim() !== s) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) continue;
                        cand.push({el: el, y: r.y});
                    }
                }
                cand.sort((a, b) => b.y - a.y);
                if (cand.length) {
                    const el = cand[0].el;
                    const box = el.closest('li, a, button, [role="button"]') || el.parentElement || el;
                    box.setAttribute('data-pdd-next', 'number');
                    return {method: 'number', y: Math.round(cand[0].y)};
                }
                for (const sel of ['[aria-label*="下一页"]', '[title*="下一页"]',
                                   '[class*="pagination"] [class*="next"]',
                                   '[class*="pager"] [class*="next"]',
                                   '.ant-pagination-next', '[class*="arrow-right"]']) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;
                        if ((el.className || '').toString().toLowerCase().includes('disabled')) continue;
                        el.setAttribute('data-pdd-next', 'arrow');
                        return {method: 'arrow', sel: sel};
                    }
                }
                return null;
            }
        """ % (PAGER_ITEM_SELECTOR, PAGER_UL_SELECTOR, PAGER_NEXT_SELECTOR), target)

        if marker:
            try:
                loc = self.page.locator('[data-pdd-next]').first
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=4000)
                # 主校验：分页组件当前页码真的变成 target；订单号变化仅作辅助
                page_ok = False
                after = before
                for _ in range(12):
                    await asyncio.sleep(0.6)
                    st2 = await self._pager_state()
                    if st2.get("found") and (st2.get("page") == target
                                             or st2.get("activeText") == str(target)):
                        page_ok = True
                        after = await self._page_signature()
                        self._total_pages = st2.get("maxVisible") or self._total_pages
                        break
                    after = await self._page_signature()
                    if after and after != before and not st2.get("found"):
                        page_ok = True  # 没有真实分页组件时才退回「内容变化」判定
                        break
                if page_ok:
                    self._page_num = target
                    print(f"   ✅ 已翻到第 {target} 页（方式：{marker['method']}，"
                          f"内容{'已' if after != before else '未'}变化）")
                    await asyncio.sleep(0.8)  # 等列表渲染完再扫卡片
                    return True
                print(f"   ⚠️ 点了下一页但页码仍停在 {cur}，判为未翻页成功")
            except Exception as e:
                print(f"   ⚠️ 页码点击失败: {e}")
        else:
            print(f"   ⚠️ 未找到第 {target} 页的页码或下一页箭头")

        # 导出结构便于校准，然后明确结束该星级，避免旧策略乱点造成误操作
        await self._export_page_structure("paging_fail")
        return False

        # 以下为旧版兜底策略（保留参考，默认不执行）
        # 策略1：找"下一页"按钮/链接
        for selector in [
            ":text('下一页')",
            "button:has-text('下一页')",
            "a:has-text('下一页')",
            "li:has-text('下一页')",
            "[class*='next']:not([class*='disabled'])",
            ".pagination .next",
            ".pager-next",
            "li.next:not(.disabled)",
        ]:
            try:
                next_btn = self.page.locator(selector).first
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    if await next_btn.is_enabled():
                        await self.safe_click(next_btn)
                        await asyncio.sleep(2)
                        print("   ✅ 已翻到下一页")
                        return True
            except Exception:
                continue

        # 策略2：找数字分页，点击下一页数字
        try:
            current_page = self.page.locator("[class*='active'], .current, [class*='current']").first
            if await current_page.count() > 0:
                current_text = await current_page.inner_text()
                current_num = int(current_text.strip()) if current_text.strip().isdigit() else None
                if current_num:
                    next_num = str(current_num + 1)
                    next_link = self.page.locator(f"a:text('{next_num}'), li:text('{next_num}'), span:text('{next_num}')").first
                    if await next_link.count() > 0 and await next_link.is_visible():
                        await self.safe_click(next_link)
                        await asyncio.sleep(2)
                        print(f"   ✅ 已翻到第 {next_num} 页")
                        return True
        except Exception:
            pass

        # 策略3：找右箭头图标
        for selector in [
            "[class*='icon-right']",
            "[class*='arrow-right']",
            ".ant-pagination-next",
            ".pagination-item-next",
        ]:
            try:
                arrow = self.page.locator(selector).first
                if await arrow.count() > 0 and await arrow.is_visible():
                    await self.safe_click(arrow)
                    await asyncio.sleep(2)
                    print("   ✅ 通过箭头翻到下一页")
                    return True
            except Exception:
                continue

        # 检查是否已无更多数据
        for selector in SELECTORS["no_more_data"].split(", "):
            try:
                no_data = self.page.locator(selector.strip()).first
                if await no_data.count() > 0:
                    print("   🏁 已无更多数据")
                    return False
            except Exception:
                continue

        print("   ⚠️ 未找到翻页按钮，可能已是最后一页")
        return False

    async def run_filter_and_report(self, star: int):
        """针对指定星级执行筛选和举报"""
        print(f"\n{'=' * 50}")
        print(f"🎯 开始处理 {star} 星评价")
        print(f"{'=' * 50}")

        try:
            # 筛选星级（filter_by_star 内部会先取消之前的选择）
            success = await self.filter_by_star(star)
            if not success:
                print(f"   ❌ {star} 星筛选失败，跳过此星级继续下一个...")
                # 不阻塞等待用户输入 — bat 运行环境用户可能看不到终端
                await asyncio.sleep(3)
                return
            self._current_star = star

            # 逐页处理。总页数只作展示，绝不再用它提前掐断循环
            # （旧版把筛选瞬间的「共 10 条」当成总页数 = 1 页，导致全量模式也只跑 1 页）
            page_num = self._page_num or 1
            limit = MAX_PAGES if MAX_PAGES > 0 else 0
            print(f"   📚 本次最多处理 {limit or '不限'} 页，从第 {START_PAGE} 页起")

            # 需要跳页时先连续翻过去（中间页不处理，适合前几页已举报完的情况）
            if START_PAGE > 1:
                for _ in range(START_PAGE - 1):
                    if not await self.go_to_next_page():
                        print(f"   ⚠️ 跳不到第 {START_PAGE} 页，从当前页继续")
                        break
                page_num = self._page_num
                print(f"   ⏩ 已跳到第 {page_num} 页")

            processed = 0
            while True:
                if self._probe_done or self.daily_limit_reached:
                    break
                print(f"\n--- 第 {page_num} 页 ---")

                count = await self.process_reviews_on_page(star)
                processed += 1

                if self.daily_limit_reached:
                    print("   ⏸️ 已到达当日举报上限：不再翻页、不再处理后续星级")
                    break
                if self._probe_done:
                    break

                pager = await self._pager_state()
                if pager.get("found"):
                    print("   📊 本页可举报 %d 条 | 分页 %s/%s | 下一页%s"
                          % (count, pager.get("page"), pager.get("maxVisible") or "?",
                             "可用" if not pager.get("nextDisabled") else "已禁用"))
                else:
                    print(f"   📊 本页可举报 {count} 条（未检测到真实分页组件）")

                if limit and processed >= limit:
                    print(f"   已达到本次页数上限 ({limit} 页)")
                    break

                # 关键修复：本页 0 条可举报（多半是全已举报过、卡片不再显示「举报」）
                # 不等于数据结束，只要分页说还有下一页就必须继续翻
                if pager.get("found") and pager.get("nextDisabled"):
                    print("   🏁 下一页已禁用，本星级处理完毕")
                    break
                if not pager.get("found") and count == 0:
                    print("   ℹ️ 无分页组件且无评价卡片，结束该星级")
                    break

                has_next = await self.go_to_next_page()
                if not has_next:
                    print("   翻页未成功，结束该星级")
                    break
                page_num = self._page_num

        except Exception as e:
            print(f"   ⚠️ 处理 {star} 星时出现异常: {e}")
            import traceback
            traceback.print_exc()
            print(f"   跳过 {star} 星，继续下一个星级...")

    async def run(self):
        """主流程"""
        mode = ("结构导出（不筛选、不提交）" if SCAN_MODE else
                "探测（不提交）" if PROBE_MODE else
                ("试运行（不提交）" if DRY_RUN else "★ 正式运行（真实提交）★"))
        print("\n🚀 拼多多商家后台 - 评价举报自动化脚本")
        print("=" * 60)
        print(f"运行模式: {mode}")
        print(f"举报入口: {REPORT_TYPE}（单入口，不降级）")
        if SINGLE_ENTRY_ONLY:
            print("风控策略: 入口被置灰 / 平台拒绝 → 立即暂停全部任务 + 桌面弹窗提醒")
            print("          「已到达当日举报上限」，次日额度恢复后重新运行即可")
            _limit = daily_limit_state()
            print(f"当日上限标记: {'⚠️ 今日已达上限（--force 可强制再试）' if _limit else '无'}")
        else:
            print("举报入口优先级（--multi-entry / --rank-from 已启用降级）:")
            for p in REPORT_TYPE_PLAN:
                _desc = {"fill": "描述必填", "auto": "有描述框才填",
                         "none": "不填内容直接提交"}.get(p["reason"], p["reason"])
                print("   %d. %-10s %s%s"
                      % (p["rank"], p["name"], _desc,
                         ("｜二级原因：" + p["sub_reason"]) if p["sub_reason"] else ""))
        print(f"本次入口范围: 第 {REPORT_TYPE_PLAN[0]['rank']} 级起，"
              f"共 {len(REPORT_TYPE_PLAN)} 个候选入口")
        print(f"详细描述: {REPORT_REASON}")
        print(f"目标星级: {', '.join(str(s) + '星' for s in TARGET_STARS)}")
        print(f"每星级最多: {'不限' if MAX_PAGES == 0 else str(MAX_PAGES) + ' 页'}")
        print(f"起始页码: 第 {START_PAGE} 页（--start-page 可跳页）")
        print(f"去重台账: 已有 {len(self.reported_ids)} 条记录")
        print(f"无头模式: {'是' if HEADLESS else '否'}")
        print("=" * 60)
        if not DRY_RUN and not PROBE_MODE:
            print("⚠️ 5 秒后将开始正式提交，若不想继续请按 Ctrl+C")
            await asyncio.sleep(5)

        try:
            # 1. 启动浏览器
            await self.setup_browser()

            # 2. 等待手动登录
            await self.wait_for_manual_login()

            # 3. 导航到评价管理
            await self.navigate_to_review_management()

            # 3.5 结构导出模式：把真实控件结构落盘，供精确校准
            if SCAN_MODE:
                print("\n🔬 结构导出模式（不筛选、不点举报、不提交）")
                await self._export_page_structure("scan")
                star_tags = await self._scan_star_filters()
                print(f"   星级筛选标签: {star_tags}")
                print(f"   推算总页数: {await self._detect_total_pages()}")
                cards = await self._collect_review_cards()
                print(f"   识别到评价卡片: {len(cards)} 条")
                for c in cards[:3]:
                    _brief = re.sub(r'\s+', ' ', c['text'])[:90]
                    print(f"      [{c['idx']}] 星级={self._detect_card_star(c['text'])} "
                          f"举报入口={c['hasBtn']} 文本={_brief}")
                print("\n请把 assets/scan_structure.json 与 scan_page.html 发回，用于最终校准")
                return

            # 4. 逐一处理每个星级
            for star in TARGET_STARS:
                if self._probe_done or self.daily_limit_reached:
                    break
                await self.run_filter_and_report(star)
                if self._probe_done or self.daily_limit_reached:
                    break
                # 在星级间稍作等待
                await asyncio.sleep(PAGE_WAIT_SECONDS)

            # 5. 输出统计
            print("\n" + "=" * 60)
            print("📊 执行统计")
            print("=" * 60)
            print(f"   运行模式:       {mode}")
            print(f"   检查评价数:     {self.stats['total_checked']}")
            print(f"   成功举报数:     {self.stats['total_reported']}")
            print("   分级成功明细:   " + " / ".join(
                "%d级 %s = %d" % (
                    p["rank"], p["name"], self.plan_stats.get(p["rank"], 0))
                for p in REPORT_TYPE_PLAN))
            print(f"   降级次数:       {self.stats.get('fallback_used', 0)}"
                  "  ← 单入口模式应为 0")
            print(f"   当日上限暂停:   {self.stats.get('daily_limit_hits', 0)}")
            print(f"   平台明确拒绝:   {self.stats.get('platform_rejected', 0)}"
                  "  ← 这类没写台账，可稍后重试")
            print(f"   重复跳过数:     {self.stats['skipped_duplicate']}")
            print(f"   类型未选中阻断: {self.stats['blocked_no_type']}")
            print(f"   失败/异常数:    {self.stats['errors']}")
            print(f"   描述未填阻断:   {self.stats.get('blocked_no_reason', 0)}"
                  "  ← 这类一条都没提交")
            print(f"   无描述字段提交: {self.stats.get('no_reason_field', 0)}")
            print(f"   待人工核对数:   {self.stats.get('submitted_unverified', 0)}"
                  "  ← 只确认到弹窗关闭，未见成功提示")
            print(f"   执行日志:       {LOG_FILE}")
            print(f"   去重台账:       {REPORTED_FILE}")
            if PROBE_MODE:
                print(f"   弹窗结构:       {PROBE_FILE}")
            if SINGLE_ENTRY_ONLY:
                print(f"   当日上限状态:   {DAILY_LIMIT_FILE}")
            print("=" * 60)
            if self.daily_limit_reached:
                print_daily_limit_banner(
                    daily_limit_message(self.stats["total_reported"]))

        except KeyboardInterrupt:
            print("\n⚠️ 用户中断")
        except Exception as e:
            print(f"\n❌ 脚本异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # 保存登录状态
            if self.context:
                try:
                    storage_file = os.path.join(USER_DATA_DIR, "storage_state.json")
                    await self.context.storage_state(path=storage_file)
                    print("💾 登录状态已保存")
                except Exception:
                    pass

            print("\n💡 任务结束，登录态已保存，下次运行可免登录")
            if self.daily_limit_reached:
                print("⏸️  " + ALERT_TITLE + "：本次已提前暂停，明天重新运行启动器即可继续")
                print("    如需立刻再试：--force（或启动器菜单 12）；清除标记：--clear-limit")
            elif SINGLE_ENTRY_ONLY and daily_limit_state() and not FORCE_RUN:
                print("⚠️  检测到今日上限标记，下次正式运行会先提示。次日自动恢复")

            # 清理
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()


def rebuild_ledger_from_log():
    """
    按执行日志重建去重台账：只保留「见到举报成功提示」的条目。
    旧版把 verify=dialog_closed 误判为成功并写入台账，会导致真正需要重报的评价被跳过。
    原台账先备份成 .bak，不删除任何数据。此模式不启动浏览器、不提交任何举报。
    """
    print("\n🧹 按日志重建去重台账（只保留确认成功的条目）")
    print("=" * 60)
    if not os.path.exists(LOG_FILE):
        print("   ⚠️ 找不到执行日志: " + LOG_FILE)
        return
    old = set()
    if os.path.exists(REPORTED_FILE):
        try:
            with open(REPORTED_FILE, "r", encoding="utf-8") as f:
                old = set(json.load(f))
        except Exception as e:
            print("   ⚠️ 原台账读取失败: " + str(e))
    valid = set()
    status_count = {}
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            s = rec.get("status", "")
            status_count[s] = status_count.get(s, 0) + 1
            note = rec.get("note") or ""
            if s == "reported" and "verify=success_toast" in note and rec.get("key"):
                valid.add(rec["key"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if os.path.exists(REPORTED_FILE):
        bak = REPORTED_FILE.replace(".json", ".bak_" + stamp + ".json")
        try:
            with open(REPORTED_FILE, "r", encoding="utf-8") as f:
                data = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(data)
            print("   💾 原台账已备份: " + os.path.basename(bak))
        except Exception as e:
            print("   ⚠️ 备份失败: " + str(e))
    try:
        with open(REPORTED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(valid), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("   ❌ 写入新台账失败: " + str(e))
        return
    print("   📊 日志状态分布: " + json.dumps(status_count, ensure_ascii=False))
    print("   原台账 %d 条 → 新台账 %d 条（移除 %d 条未确认成功的记录）"
          % (len(old), len(valid), len(old - valid)))
    print("   ✅ 完成。被移除的评价下一轮会重新处理，不删除任何数据。")


def main():
    """入口函数 — 带完整错误捕获，双击也不会闪退"""
    if "--rebuild-ledger" in sys.argv:
        rebuild_ledger_from_log()
        return
    print("""
╔══════════════════════════════════════════════════════╗
║     拼多多商家后台 — 评价举报自动化工具            ║
║                                                     ║
║  本工具将帮助你：                                    ║
║    • 自动筛选 1星/2星/3星 差评                      ║
║    • 逐条举报恶意评价                                ║
║    • 保存浏览器登录态，下次免登录                    ║
║                                                     ║
║  ⚠️ 首次使用请先运行「一键安装依赖」                 ║
║  策略: 只走「同行恶意差评」；风控即暂停并提醒       ║
║        当日上限次日自动恢复                          ║
╚══════════════════════════════════════════════════════╝
    """)

    # 手动清除当日上限标记（误判 / 提前恢复时用），不启动浏览器
    if CLEAR_LIMIT:
        removed = clear_daily_limit()
        if removed:
            print("✅ 已清除当日举报上限标记，现在可以正常正式运行")
        else:
            print("ℹ️ 当前没有当日上限标记，无需清除")
        print("   （提示：跨天会自动失效，第二天直接运行即可，不必手动清除）")
        return

    # 今日已达上限：正式运行前直接拦下，不再开浏览器、不再点任何举报
    limit = daily_limit_state()
    if limit and not FORCE_RUN and not DRY_RUN and SINGLE_ENTRY_ONLY:
        msg = daily_limit_message(limit.get("success", 0), limit.get("ts", ""))
        print("⏸️  检测到今日已达举报上限：%s" % limit.get("ts", ""))
        print("   触发原因: %s %s" % (limit.get("reason", ""), limit.get("detail", "")))
        print_daily_limit_banner(msg)
        show_desktop_alert(ALERT_TITLE, msg)
        print("   明天直接重新运行启动器即可（标记跨天自动失效）")
        print("   确认额度已恢复可加 --force，或用启动器菜单 11 清除标记")
        return
    if limit and FORCE_RUN:
        print("⚠️  --force：忽略今日上限标记，本次仍会尝试正式提交")
    if limit and DRY_RUN:
        print("ℹ️  今日有上限标记，但当前是试运行/探测模式，不做拦截")

    try:
        asyncio.run(PinduoduoReviewReporter().run())
    except ModuleNotFoundError as e:
        print(f"\n❌ 缺少依赖模块: {e}")
        print("   请先运行「一键安装依赖」安装 playwright 与 Chromium")
    except Exception as e:
        print(f"\n❌ 脚本运行出错: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        print("运行结束。（窗口可直接关闭）")


if __name__ == "__main__":
    main()
