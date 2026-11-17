# PDD Review Helper · Windows EXE 云端构建仓库

这个仓库只做一件事：在 GitHub Actions 的 **Windows 云端机器**上构建
`PDD_Review_Helper.exe`。你自己的电脑**不用装 Python、不用是 Windows**。

## 一次推送 = 自动构建 + 自动下载链接

把本目录的文件推到 GitHub 的 `main`（或 `master`）分支后，workflow 会自动：

1. 安装 playwright + pyinstaller
2. 校验必需文件与 `cloud_config.json` 的授权服务器地址
3. 打包成单文件 exe（内置热更新引导）
4. 上传 Artifact，并**自动创建一个 Release**

构建约 5–10 分钟。去仓库的 **Releases** 页面下载 `PDD_Review_Helper.exe`，
或者进 **Actions → 本次运行 → Artifacts** 下载。

也可以在 Actions 页面点 **Run workflow** 手动触发；
勾 `Embed Chromium` 才会把浏览器内核一起打进去（多约 150MB），
一般电脑上用系统自带 Edge / Chrome，直接默认即可。

## 下载链接能不能直接发给使用者？

| 仓库类型 | 结果 |
|---|---|
| **Private（推荐）** | Release 链接需要登录你的 GitHub 才能打开。自己下载 exe 后转发给使用者即可 |
| Public | 链接任何人可下载，但仓库源码也会公开，请自行权衡 |

GitHub Actions 免费额度：私有仓库每月 2000 分钟，构建一次约 5–10 分钟，完全够用。

## 安全说明

- `cloud_config.json` 只含授权服务器地址和版本号，**不含任何密钥**。
- 私钥、管理员密钥、激活码数据、登录态、台账都在本地，未放进本仓库（见 `.gitignore`）。
- 使用者拿到 exe 后，仍需注册账号 + 输入你发的激活码才能使用。
- 云端已部署热更新，以后改 `gui/*.py` / `scripts/*.py` 不用重新构建 exe。

## 怎么把文件推上去

- 先在 GitHub 网页手动建一个空仓库（勾 `Add a README file`，建议 Private），
  再让 WorkBuddy 通过 GitHub 连接器把本目录推到该仓库的 `main` 分支。
  注意：连接器的授权令牌不允许「新建仓库」，这一步只能在网页上点一次。
- 或在本目录双击 `推送到GitHub.command`（macOS）/ `推送到GitHub.bat`（Windows），
  粘贴新建的空仓库地址即可（推送认证要用 Personal Access Token，不是账号密码）。
- 或在 GitHub 网页手动上传文件，详见桌面《GitHub云端构建_操作图解.html》。

## 本地构建（备选）

在 Windows 上双击 `build_exe.bat` 也能构建，但需要本机装 Python 3.9+。
