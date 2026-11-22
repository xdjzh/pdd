# -*- coding: utf-8 -*-
"""
启动热更新（独立于 gui 包的顶层模块，零第三方依赖）。

设计要点
  - exe 只是「引导壳」：里面那份 gui/ 与 scripts/ 是基线版本，永远不会变。
  - 云端放一个几十 KB 的业务代码包（zip），启动时检查版本、下载、校验、
    解压到 pdd_data/update/current，再把该目录插到 sys.path[0]，
    这样 `import gui.app` 会优先加载补丁里的全套模块。
  - 必须在任何 `import gui.*` 之前调用，否则 gui 包的 __path__ 会被锁死在
    PyInstaller 的 _MEIPASS 里，补丁就覆盖不了了。
  - 断网 / 云端异常 / 校验失败一律静默降级到 exe 内置代码，绝不阻塞登录。
  - 安全模式：环境变量 PDD_NO_UPDATE=1，或 pdd_data/update/SAFE 文件存在，
    强制忽略所有补丁，回到 exe 内置版本（用于坏包回滚）。
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

CONFIG_FILE = "cloud_config.json"
MANIFEST = "manifest.json"
REQUIRED = (
    "manifest.json",
    "gui/__init__.py", "gui/app.py", "gui/state.py", "gui/theme.py",
    "gui/license.py", "gui/login_window.py", "gui/adapter.py",
    "gui/engine.py", "gui/autostart.py", "scripts/report_reviews.py",
)
MAX_ZIP = 2 * 1024 * 1024          # 压缩包上限 2MB
MAX_UNPACKED = 6 * 1024 * 1024    # 解压后上限 6MB
MAX_FILES = 120
ALLOW_EXT = (".py", ".json", ".txt", ".md")


# ---------- 基础设施（不 import gui，避免提前锁定包路径） ----------
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def update_root():
    return os.path.join(app_dir(), "pdd_data", "update")


def patch_dir():
    return os.path.join(update_root(), "current")


def _log(msg):
    try:
        root = update_root()
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "update.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _config_paths():
    out = [
        os.path.join(app_dir(), "pdd_data", CONFIG_FILE),   # 用户在界面里改过的地址
        os.path.join(app_dir(), CONFIG_FILE),               # exe 同级
        os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE),
    ]
    base = getattr(sys, "_MEIPASS", "")
    if base:
        out.append(os.path.join(base, CONFIG_FILE))          # 打包进 exe 的默认配置
    return out


def load_config():
    cfg = {}
    for path in _config_paths():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k not in cfg or (k == "endpoint" and v):
                        cfg[k] = v
        except Exception:
            continue
    return cfg


def _int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def endpoint():
    return str(os.environ.get("PDD_AUTH_ENDPOINT") or
               load_config().get("endpoint") or "").strip().rstrip("/")


def base_version():
    return _int(load_config().get("appVersion"), 0)


def read_manifest():
    try:
        with open(os.path.join(update_root(), MANIFEST), "r",
                  encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _post(url, body, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        obj = json.loads(resp.read().decode("utf-8", "ignore"))
    if isinstance(obj, dict) and isinstance(obj.get("body"), str):
        obj = json.loads(obj["body"])
    return obj if isinstance(obj, dict) else {}


# ---------- 包校验（防坏包 / 防目录穿越） ----------
def validate_zip(data):
    if not data.startswith(b"PK\x03\x04"):
        raise ValueError("不是有效的 ZIP 包")
    if len(data) > MAX_ZIP:
        raise ValueError("更新包超过 2MB")
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if len(names) > MAX_FILES:
        raise ValueError("更新包文件数过多")
    total = 0
    for n in names:
        n = n.replace("\\", "/")
        if n.startswith("/") or ".." in n.split("/") or ":" in n:
            raise ValueError("更新包含非法路径：" + n[:60])
        if n.endswith("/"):
            continue
        if not n.lower().endswith(ALLOW_EXT):
            raise ValueError("更新包含不允许的文件：" + n[:60])
        blob = zf.read(n)
        total += len(blob)
        if total > MAX_UNPACKED:
            raise ValueError("解压后体积过大")
        if n.endswith(".py"):
            compile(blob.decode("utf-8"), n, "exec")   # 语法错误直接拒收
    low = set(n.replace("\\", "/") for n in names)
    for need in REQUIRED:
        if need not in low:
            raise ValueError("更新包缺少 " + need)
    try:
        m = json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception:
        raise ValueError("manifest.json 无法解析")
    if not _int(m.get("version")):
        raise ValueError("manifest.json 缺少版本号的")
    zf.close()
    return m


def _install(data, meta):
    """先解到 staging 校验通过再原子换目录，失败绝不破坏现有补丁。"""
    root = update_root()
    staging = os.path.join(root, "staging")
    current = os.path.join(root, "current")
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    zf = zipfile.ZipFile(io.BytesIO(data))
    for n in zf.namelist():
        rel = n.replace("\\", "/")
        if rel.endswith("/"):
            continue
        dest = os.path.join(staging, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(zf.read(n))
    zf.close()
    # 换目录：先备份旧的，成功后再删备份
    backup = os.path.join(root, "previous")
    if os.path.isdir(backup):
        shutil.rmtree(backup, ignore_errors=True)
    had = os.path.isdir(current)
    if had:
        os.rename(current, backup)
    os.rename(staging, current)
    if had:
        shutil.rmtree(backup, ignore_errors=True)
    state = {
        "version": _int(meta.get("version")),
        "label": str(meta.get("label") or ""),
        "changelog": str(meta.get("changelog") or ""),
        "sha256": str(meta.get("sha256") or ""),
        "appliedAt": int(time.time()),
    }
    with open(os.path.join(root, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 主流程 ----------
def apply_update(timeout_check=4, timeout_get=15):
    """返回最终生效的版本号；任何异常都吞掉并回退到 exe 内置代码。"""
    root = update_root()
    safe = (os.environ.get("PDD_NO_UPDATE") == "1" or
            os.path.exists(os.path.join(root, "SAFE")))
    base = base_version()
    state = read_manifest()
    applied = _int(state.get("version"))

    if safe:
        _log("安全模式：忽略所有更新，使用 exe 内置版本 %s" % base)
        return base

    # 补丁比 exe 基线旧（换了新 exe）→ 丢弃，避免降级
    if applied and applied < base:
        _log("补丁版本 %s 低于 exe 基线 %s，忽略" % (applied, base))
        state, applied = {}, 0

    use_patch = bool(applied and applied >= base and
                     os.path.isfile(os.path.join(patch_dir(),
                                                "gui", "__init__.py")))
    if use_patch:
        sys.path.insert(0, patch_dir())

    url = endpoint()
    if not url:
        _log("未配置授权服务器地址，跳过在线检查")
        return applied or base

    local = max(base, applied)
    try:
        info = _post(url, {"action": "check_update", "appVersion": local},
                     timeout_check)
        if info.get("rollback"):
            # 云端一键回滚：丢掉本地补丁，回到 exe 内置版本
            pd = patch_dir()
            while pd in sys.path:
                sys.path.remove(pd)
            shutil.rmtree(pd, ignore_errors=True)
            try:
                os.remove(os.path.join(root, MANIFEST))
            except Exception:
                pass
            _log("收到云端回滚指令，已丢弃补丁，回到基线 %s" % base)
            return base
        if not info.get("ok") or not info.get("hasUpdate"):
            _log("已是最新版本 %s" % local)
            return applied or base
        remote = _int(info.get("version"))
        if remote <= local:
            _log("云端版本 %s 不高于本地 %s，跳过" % (remote, local))
            return applied or base
        n = _int(info.get("chunks"))
        if n > 0:
            pieces = []
            for i in range(n):
                part = _post(url, {"action": "get_update_chunk", "index": i},
                             timeout_get)
                if not part.get("ok") or not part.get("data"):
                    raise ValueError(part.get("msg") or
                                     "第 %d 个分片下载失败" % i)
                pieces.append(base64.b64decode(part["data"]))
            data = b"".join(pieces)
        else:
            # 兼容旧版云端：整包直下
            res = _post(url, {"action": "get_update", "appVersion": local},
                        timeout_get)
            if not res.get("ok") or not res.get("zipBase64"):
                raise ValueError(res.get("msg") or "云端未返回更新包")
            data = base64.b64decode(res["zipBase64"])
        digest = hashlib.sha256(data).hexdigest()
        expected = str(info.get("sha256") or "")
        if expected and digest != expected:
            raise ValueError("校验和不匹配，拒绝安装")
        meta = validate_zip(data)
        if _int(meta.get("version")) != remote:
            raise ValueError("包内版本与云端版本不一致")
        meta["sha256"] = digest
        _install(data, meta)
        _log("热更新成功：%s -> %s（%d 字节，%s）" %
             (local, remote, len(data), digest[:12]))
        # 新补丁目录必须在 sys.path 最前；若之前插的是旧补丁，先移除
        pd = patch_dir()
        while pd in sys.path:
            sys.path.remove(pd)
        sys.path.insert(0, pd)
        return remote
    except Exception as e:
        _log("热更新失败，已回退：%s" % str(e)[:180])
        return applied or base


def version_info():
    """给主界面展示用：当前生效版本、是否已打补丁。"""
    base = base_version()
    state = read_manifest()
    applied = _int(state.get("version"))
    active = applied if (applied > base and not os.path.exists(
        os.path.join(update_root(), "SAFE"))) else base
    return {
        "version": active,
        "base": base,
        "patched": active != base,
        "label": str(state.get("label") or ""),
        "appliedAt": _int(state.get("appliedAt")),
    }
