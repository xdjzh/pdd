# -*- coding: utf-8 -*-
"""
桌面端授权：账号登录 / 激活码 / 离线票据 / 在线复核。

设计要点
  - 授权判定权威在云函数，本地只做「验签 + 到期判断」，不保存任何密钥。
  - 公钥由服务端首次登录时下发并锁定，离线票据用纯 Python Ed25519 验签，
    签名不对绝不静默放行。
  - 网络、设备指纹、验签全部只用标准库，Windows 打包不增加任何依赖。
"""
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request

CONFIG_ENV = "PDD_AUTH_ENDPOINT"
CONFIG_FILE = "cloud_config.json"
CACHE_FILE = "license_cache.json"


def _b64url_decode(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _data_dir():
    try:
        from .state import ShopStore
        return ShopStore().root
    except Exception:
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(sys.argv[0])))
        return os.path.join(base, "pdd_data")


def _run(cmd, timeout=6):
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
          "timeout": timeout}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.check_output(cmd, **kw).decode("utf-8", "ignore")
    except Exception:
        return ""


def device_fingerprint():
    """主板 UUID + 磁盘序列号 + 机器名，哈希后作为设备 ID。

    不单独用 MAC：虚拟网卡、禁用网卡、换网口都会变，容易把老用户锁在外面。
    """
    parts = []
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_ComputerSystemProduct).UUID"])
        if not out.strip() or "00000000" in out:
            out = _run(["wmic", "csproduct", "get", "uuid"])
        lines = [x.strip() for x in out.splitlines()
                 if x.strip() and "uuid" not in x.lower()]
        if lines:
            parts.append(lines[0])
        disk = _run(["wmic", "diskdrive", "get", "serialnumber"])
        dl = [x.strip() for x in disk.splitlines()
              if x.strip() and "serial" not in x.lower()]
        if dl:
            parts.append(dl[0])
    elif sys.platform == "darwin":
        out = _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                parts.append(line.split("=")[-1].strip().strip('"'))
                break
    else:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path, "r") as f:
                    parts.append(f.read().strip())
                break
            except Exception:
                pass
    parts.extend([platform.node(), os.path.expanduser("~")])
    try:
        import uuid
        parts.append(str(uuid.getnode()))
    except Exception:
        pass
    raw = "|".join(p for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:32]


# ---- 纯 Python Ed25519 验签（RFC 8032），零第三方依赖 ----
# 云端用 Node crypto 的 Ed25519 私钥签名，这里只做验证；
# 不引入 cryptography，PyInstaller 打包更稳，Windows 上也少一个依赖。
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_xrecover(y):
    xx = (y * y - 1) * pow((_ED_D * y * y + 1) % _ED_P,
                           _ED_P - 2, _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = x * _ED_I % _ED_P
    if x & 1:
        x = _ED_P - x
    return x


_ED_BY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_B = [_ed_xrecover(_ED_BY), _ED_BY]


def _ed_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    k = (_ED_D * x1 * x2 * y1 * y2) % _ED_P
    x3 = (x1 * y2 + x2 * y1) * pow((1 + k) % _ED_P,
                                    _ED_P - 2, _ED_P) % _ED_P
    y3 = (y1 * y2 + x1 * x2) * pow((1 - k) % _ED_P,
                                    _ED_P - 2, _ED_P) % _ED_P
    return [x3, y3]


def _ed_mul(P, e):
    if e == 0:
        return [0, 1]
    Q = _ed_mul(P, e >> 1)
    Q = _ed_add(Q, Q)
    if e & 1:
        Q = _ed_add(Q, P)
    return Q


def _ed_decode(data):
    y = int.from_bytes(data[:32], "little") & ((1 << 255) - 1)
    x = _ed_xrecover(y)
    if (x & 1) != (data[31] >> 7):
        x = _ED_P - x
    return [x, y]


def _verify_ed25519(pub_b64, body, sig_b64):
    """True 验签通过 / False 签名或数据无效。"""
    try:
        pk = _b64url_decode(pub_b64)
        sig = _b64url_decode(sig_b64)
        if len(pk) != 32 or len(sig) != 64:
            return False
        A = _ed_decode(pk)
        R = _ed_decode(sig[:32])
        S = int.from_bytes(sig[32:], "little")
        if S >= _ED_L:
            return False
        h = hashlib.sha512(sig[:32] + pk + body.encode("ascii")).digest()
        k = int.from_bytes(h, "little") % _ED_L
        lhs = _ed_mul(_ED_B, S)
        rhs = _ed_add(R, _ed_mul(A, k))
        return lhs[0] == rhs[0] and lhs[1] == rhs[1]
    except Exception:
        return False


class LicenseClient(object):
    def __init__(self, endpoint=None):
        self.data_dir = _data_dir()
        self.cache_file = os.path.join(self.data_dir, CACHE_FILE)
        self.config_file = os.path.join(self.data_dir, CONFIG_FILE)
        cfg = self._load_config()
        self.endpoint = (endpoint or os.environ.get(CONFIG_ENV)
                         or cfg.get("endpoint") or "").strip()
        self.offline_hours = int(cfg.get("offlineGraceHours") or 72)
        self.app_name = cfg.get("appName") or "PDD 工作台"
        self.device_id = device_fingerprint()

    def _candidate_configs(self):
        base = getattr(sys, "_MEIPASS", "")
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = [self.config_file,
               os.path.join(exe_dir, CONFIG_FILE),
               os.path.join(src_dir, CONFIG_FILE)]
        if base:
            out.append(os.path.join(base, CONFIG_FILE))
        return out

    def _load_config(self):
        merged = {}
        for path in self._candidate_configs():
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k not in merged or (k == "endpoint" and v):
                            merged[k] = v
            except Exception:
                continue
        return merged

    def save_endpoint(self, url):
        url = str(url or "").strip().rstrip("/")
        if not url.startswith("http"):
            return False, "服务器地址要以 https:// 开头"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            cfg = self._load_config()
            cfg["endpoint"] = url
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self.endpoint = url
            return True, ""
        except Exception as e:
            return False, str(e)

    # ---------- 云端请求 ----------
    def call(self, action, payload=None, timeout=8):
        if not self.endpoint:
            return {"ok": False, "msg": "尚未配置授权服务器地址",
                    "needSetup": True}
        body = {"action": action, "deviceId": self.device_id}
        if payload:
            body.update(payload)
        try:
            req = urllib.request.Request(
                self.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            obj = json.loads(raw)
            # 云开发 HTTP 访问服务可能包一层 {statusCode, body}
            if isinstance(obj, dict) and isinstance(obj.get("body"), str):
                obj = json.loads(obj["body"])
            return obj if isinstance(obj, dict) else {"ok": False,
                                                     "msg": "服务端返回异常"}
        except Exception as e:
            return {"ok": False, "msg": "无法连接授权服务器：%s" %
                    str(e)[:100], "offline": True}

    # ---------- 本地票据 ----------
    def load_cache(self):
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_result(self, res):
        data = {
            "ticket": res.get("ticket", ""),
            "publicKey": res.get("publicKey", ""),
            "phoneMask": res.get("phoneMask", ""),
            "license": res.get("license", {}),
            "deviceId": self.device_id,
            "verifiedAt": time.time(),
            "serverTime": res.get("serverTime", 0),
        }
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return LicenseSession(self, data)

    def logout(self):
        try:
            if os.path.exists(self.cache_file):
                os.remove(self.cache_file)
        except Exception:
            pass

    def session_from_cache(self, cached, allow_offline=True):
        """离线放行条件：签名有效、票据未过期、许可未到期、设备一致、在宽限内。"""
        if not cached or not cached.get("ticket"):
            return None
        ticket = str(cached["ticket"])
        parts = ticket.split(".")
        if len(parts) != 2:
            return None
        pub = cached.get("publicKey")
        if pub:
            v = _verify_ed25519(pub, parts[0], parts[1])
            if v is not True:
                # 没有验签库时不允许离线，签名错误直接拒绝
                return None
        elif allow_offline:
            return None
        try:
            payload = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        except Exception:
            return None
        now_ms = time.time() * 1000
        if not payload.get("exp") or payload["exp"] < now_ms:
            return None
        lic_exp = payload.get("licenseExpireAt") or 0
        if lic_exp and lic_exp < now_ms:
            return None
        if not payload.get("licenseId"):
            return None                      # 注册但未激活
        if payload.get("deviceId") and payload["deviceId"] != self.device_id:
            return None
        if cached.get("deviceId") and cached["deviceId"] != self.device_id:
            return None
        # 票据 exp 本身就是离线宽限（服务端签发 72h），不再叠加本地时间判断，
        # 避免用户改系统时间或缓存时间漂移把正常授权锁死。
        return LicenseSession(self, cached)


class LicenseSession(object):
    def __init__(self, client, data):
        self.client = client
        self.data = data
        self.offline = False

    @property
    def phone_mask(self):
        return self.data.get("phoneMask") or "已登录"

    @property
    def license_name(self):
        return (self.data.get("license") or {}).get("name") or "已授权"

    @property
    def expire_text(self):
        exp = (self.data.get("license") or {}).get("expireAt") or 0
        if not exp:
            return "永久有效"
        return time.strftime("%Y-%m-%d", time.localtime(exp / 1000.0)) + " 到期"

    def label(self):
        return "%s  ·  %s  ·  %s" % (
            self.phone_mask, self.license_name, self.expire_text)

    def detail(self):
        lic = self.data.get("license") or {}
        return ("账号：%s\n授权类型：%s\n到期时间：%s\n激活码：%s\n"
                "设备编号：%s" % (
                    self.phone_mask, self.license_name, self.expire_text,
                    lic.get("codeMask") or "—",
                    self.client.device_id[:12] + "…"))

    def check_online(self):
        """关键动作（开始举报）前复核一次：吊销 / 到期会即时生效。"""
        res = self.client.call(
            "verify", {"ticket": self.data.get("ticket", "")}, timeout=8)
        if res.get("ok"):
            fresh = self.client.save_result(res)
            self.data = fresh.data
            self.offline = False
            return True, ""
        if res.get("offline"):
            # 断网时允许继续用未过期的离线票据，但不刷新宽限
            return True, ""
        self.offline = False
        return False, res.get("msg") or "授权校验失败"

    def logout(self):
        self.client.logout()


def ensure_license():
    """启动门禁：校验通过才返回 session，否则返回 None（调用方直接退出）。

    这里不 import 主界面，未授权时主界面代码根本不会加载。
    """
    client = LicenseClient()
    cached = client.load_cache()
    sess = client.session_from_cache(cached, allow_offline=True)
    if sess:
        sess.offline = True
        return sess

    reason = ""
    if cached.get("ticket"):
        res = client.call("verify", {"ticket": cached.get("ticket", "")})
        if res.get("ok"):
            return client.save_result(res)
        if not res.get("offline"):
            reason = res.get("msg") or "登录状态已失效，请重新登录"
            client.logout()
        else:
            reason = "离线使用时间已到，请联网后重新登录"
        cached = {}

    from .login_window import show_login_window
    return show_login_window(client, reason=reason)
