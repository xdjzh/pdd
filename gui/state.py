# -*- coding: utf-8 -*-
"""
多店铺配置与目录管理。

数据一律放在「程序所在目录 / pdd_data」，而不是打包后的临时解压目录，
这样 exe 关掉重开、换电脑拷贝文件夹，店铺登录态和台账都还在。

    pdd_data/
      shops.json                    店铺清单
      shops/<shop_id>/
        browser_data/storage_state.json   该店铺独立登录态
        reported_ids.json                 该店铺独立去重台账
        report_log.jsonl                  该店铺独立日志
        daily_limit.json                  该店铺独立当日上限标记
        worker.log                        该店铺控制台输出
      gui.log                       整个程序控制台输出（--noconsole 时靠它排错）
"""
import json
import os
import re
import sys
import threading
import time
import uuid


def app_base_dir():
    """程序所在目录：打包后 = exe 所在目录，源码运行 = 技能根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def skill_base_dir():
    """技能根目录：源码运行 = app_base_dir；打包后 = _MEIPASS（核心脚本被收进包里）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", app_base_dir())
    return app_base_dir()


def core_script_path():
    """核心举报脚本路径。

    热更新补丁优先：exe 内置的是基线版本，若 pdd_data/update/current 里有
    云端下发的新版 scripts/，就用补丁里的那份。补丁包里的 state.py 同样带
    这段逻辑，所以后续版本不会退化回内置脚本。
    """
    patch = os.path.join(app_base_dir(), "pdd_data", "update", "current",
                         "scripts", "report_reviews.py")
    if os.path.isfile(patch):
        return patch
    return os.path.join(skill_base_dir(), "scripts", "report_reviews.py")


class ShopStore(object):
    """店铺清单 + 每店铺独立目录，线程安全。"""

    def __init__(self, base_dir=None):
        self.root = os.path.join(base_dir or app_base_dir(), "pdd_data")
        self.shops_dir = os.path.join(self.root, "shops")
        self.config_file = os.path.join(self.root, "shops.json")
        self.gui_log = os.path.join(self.root, "gui.log")
        self._lock = threading.RLock()
        os.makedirs(self.shops_dir, exist_ok=True)
        self._shops = self._load()

    # ---------- 配置 ----------
    def _load(self):
        if not os.path.exists(self.config_file):
            return []
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            shops = data.get("shops", []) if isinstance(data, dict) else []
            out = []
            for s in shops:
                if isinstance(s, dict) and s.get("id") and s.get("name"):
                    out.append({"id": str(s["id"]), "name": str(s["name"]),
                                "created": s.get("created", ""),
                                "auto_name": bool(s.get("auto_name"))})
            return out
        except Exception:
            return []

    def _save(self):
        tmp = self.config_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"shops": self._shops}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_file)

    def list(self):
        with self._lock:
            return [dict(s) for s in self._shops]

    def get(self, shop_id):
        with self._lock:
            for s in self._shops:
                if s["id"] == shop_id:
                    return dict(s)
        return None

    def add(self, name, auto_name=False):
        """auto_name=True 表示这是扫码前的临时名，登录成功后可用平台店铺名覆盖。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("店铺名称不能为空")
        with self._lock:
            if any(s["name"] == name for s in self._shops):
                raise ValueError("已存在同名店铺")
            sid = "shop_" + uuid.uuid4().hex[:10]
            shop = {"id": sid, "name": name,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "auto_name": bool(auto_name)}
            self._shops.append(shop)
            self._save()
        os.makedirs(self.shop_dir(sid), exist_ok=True)
        return shop

    def rename(self, shop_id, new_name):
        """改名后清除 auto_name，之后不再被自动获取的名字覆盖（尊重人工命名）。"""
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("店铺名称不能为空")
        with self._lock:
            for s in self._shops:
                if s["id"] == shop_id:
                    s["name"] = new_name
                    s["auto_name"] = False
                    self._save()
                    return True
        return False

    def remove(self, shop_id, delete_data=False):
        """默认只从清单移除，保留登录态与台账，避免误删。"""
        with self._lock:
            self._shops = [s for s in self._shops if s["id"] != shop_id]
            self._save()
        if delete_data:
            import shutil
            d = self.shop_dir(shop_id)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)

    # ---------- 路径 ----------
    def shop_dir(self, shop_id):
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(shop_id))
        d = os.path.join(self.shops_dir, safe)
        os.makedirs(d, exist_ok=True)
        return d

    def browser_dir(self, shop_id):
        d = os.path.join(self.shop_dir(shop_id), "browser_data")
        os.makedirs(d, exist_ok=True)
        return d

    def storage_file(self, shop_id):
        return os.path.join(self.browser_dir(shop_id), "storage_state.json")

    def worker_log(self, shop_id):
        return os.path.join(self.shop_dir(shop_id), "worker.log")

    def has_login(self, shop_id):
        p = self.storage_file(shop_id)
        if not os.path.exists(p):
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return len(d.get("cookies") or []) > 0
        except Exception:
            return False


DEFAULT_SETTINGS = {
    "auto_task": False,        # 自动任务总开关：开机后自动登录+举报到上限
    "auto_startup": False,     # 开机自启（注册表 Run 项）
    "auto_date": "",           # 最近一次自动任务执行日期 YYYY-MM-DD（当天不重复）
    "auto_result": "",         # 最近一次自动任务结果摘要（界面展示用）
    "close_tip_shown": False,  # 是否已提示过「点 X 只是最小化到任务栏」
}


class SettingsStore(object):
    """全局设置（自动任务 / 开机自启）。独立 settings.json，线程安全。

    与 ShopStore 分开，避免动到已经稳定的店铺清单格式。任何人都可以随时
    把 auto_task 关掉——这就是用户说的「手动取消自动任务」，引擎每次循环
    都会重新读这个开关，关掉后立刻停止，无需重启。
    """

    def __init__(self, base_dir=None, root=None):
        self.root = root or os.path.join(base_dir or app_base_dir(), "pdd_data")
        self.file = os.path.join(self.root, "settings.json")
        self._lock = threading.RLock()
        os.makedirs(self.root, exist_ok=True)
        self._data = self._load()

    def _load(self):
        out = dict(DEFAULT_SETTINGS)
        try:
            if os.path.exists(self.file):
                with open(self.file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k in DEFAULT_SETTINGS:
                        if k in d:
                            out[k] = d[k]
        except Exception:
            pass
        return out

    def _save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.file)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save()

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                if k in DEFAULT_SETTINGS:
                    self._data[k] = v
            self._save()

    def snapshot(self):
        with self._lock:
            return dict(self._data)


def blank_state(name=""):
    """GUI 表格所需的全部状态字段。"""
    return {
        "name": name,
        "online": False,
        "status": "未登录",
        "star1": 0,
        "star2": 0,
        "star3": 0,
        "stars_ok": False,       # 未确认时表格显示「—」，不展示假数字
        "stars_source": "",      # api / tab / pager / none
        "star_note": "",
        "reported": 0,          # 本次举报数量
        "last_check": "",
        "daily_limit": False,
        "message": "",
    }
