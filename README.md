# Quark-HiFiNi-Linux

面向 Ubuntu 桌面的 Quark → HiFiNi 自动上传、发布与每日总结模板，基于 [Quark-HiFiNi](https://github.com/danielchan-25/Quark-HiFiNi) 的 Python 工作流。

这是可配置的独立项目，不携带原设备的数据库、网盘授权、浏览器登录、密钥或定时任务。正式使用前必须完成本机授权和一次预览验收。

## 功能与边界

- 将 `upload-files/` 中命名为 `歌手 - 歌名.mp3` 的文件上传到已配置网盘的 `Share/Media`，保留云端歌曲去重及上传结果未知保护。
- 核对歌曲与歌词、检查实际 MP3 码率、创建夸克分享并准备 HiFiNi 草稿。只有显式 `--publish` 才会提交。
- 每个北京时间自然日最多确认提交一篇，结果未知时阻止自动重发。支持人工核实记录、后续审核状态检查。
- 发布与上传状态合并成每日飞书总结。歌词查询失败只保留日志和总结，不发送即时飞书告警。
- TypeSafe 可选，只做影子判断；不生成、补写或修改歌词。

请仅处理自己有权上传、分享和发布的内容。

## Ubuntu 准备

基线为 Ubuntu 24.04 LTS、Python 3.12。还需要 Bash、curl、FFmpeg/ffprobe、符合夸克官方 Skill 要求的 Node.js，以及 Google Chrome 或可用的 Chromium 浏览器。

```bash
git clone https://github.com/danielchan-25/Quark-HiFiNi-Linux.git
cd Quark-HiFiNi-Linux
bash scripts/setup-ubuntu.sh --install-system-packages
cp .env.example .env
```

安装脚本只安装系统/Python 依赖并建立本地虚拟环境，不安装浏览器、不替你授权网盘，也不会启用定时发布。若系统依赖已准备好，使用不带参数的 `bash scripts/setup-ubuntu.sh`。

在当前用户的全局 Skill 目录安装夸克网盘官方 Skill，并通过其官方登录流程授权；如目录不同，在 `.env` 设置 `QUARK_SKILL_DIR`。不要将 Skill 的账号配置放进本仓库。填写本机需要的 `.env` 参数，浏览器可用 `BROWSER_BIN` 指定。

```bash
bash scripts/start-browser.sh
# 在独立浏览器窗口登录 HiFiNi 后继续
bash scripts/run.sh check
bash scripts/run.sh run --dry-run --session-input '检查发布草稿' --session-id "$(date +%s)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
```

`check` 只检查本机配置、依赖和数据库状态，不证明网络授权可用。草稿预览会读取网盘和歌词网站，并操作本机浏览器；不会正式发布。

## 手动任务

```bash
# 上传预览
bash scripts/run.sh upload --dry-run --session-input '预览音乐上传' --session-id 'UNIX_SECONDS-ABC123'

# 当日正式上传，日期防重复执行
bash scripts/run.sh upload --apply --slot-date YYYY-MM-DD --session-input '执行音乐上传' --session-id 'UNIX_SECONDS-ABC123'

# 正式发布，一次最多一篇
bash scripts/run.sh run --publish --session-input '执行一次正式发布' --session-id 'UNIX_SECONDS-ABC123'

# 本地预览每日总结；加 --send 才发送
bash scripts/run.sh daily-summary --date YYYY-MM-DD
```

示例中的 `UNIX_SECONDS-ABC123` 必须替换成当轮 Unix 秒时间戳、连字符和六位随机字母数字；同轮所有夸克操作复用该标识，`--session-input` 使用该轮真实指令原文。

## 定时运行

默认采用 **systemd 用户级 timer**：北京时间 06:00 上传与发布、23:55 每日总结。配置文件随仓库提供，生成到当前用户后仍需手动启用。

```bash
bash scripts/install-systemd.sh
systemctl --user daemon-reload
systemctl --user enable --now quark-hifini-daily.timer quark-hifini-summary.timer
systemctl --user list-timers 'quark-hifini-*'
journalctl --user -u quark-hifini-daily.service
```

安装器会将当前仓库路径写入用户服务配置。定时器不自动补跑关机期间错过的时段；历史恢复应核实记录后显式执行。机器需要开机，并具备可用用户服务、浏览器和网络环境。桌面退出后的浏览器运行不能仅靠启用 timer 保证。

可见 Chrome 依赖已登录的桌面会话及正确的 `DISPLAY`、`WAYLAND_DISPLAY`、`DBUS_SESSION_BUS_ADDRESS` 等会话环境；必要时在桌面终端执行 `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS`。无桌面或退出登录后需要运行时，应先在本机验证授权可用的 headless 配置，不能把开启 linger 当成浏览器可用的证明。

日常执行入口是 `bash scripts/daily-run.sh --execute`；先运行上传，即使发布被旧未知提交阻止，上传仍可独立完成。正式运行需要准确的任务原文，默认读取 `scripts/daily-task.txt`，可用 `--session-input-file` 指定文件。不要把自动化平台和 systemd 同时配置为正式执行者。

停止定时任务：

```bash
systemctl --user disable --now quark-hifini-daily.timer quark-hifini-summary.timer
```

## 迁移与状态

迁移步骤和验收清单见 [docs/MIGRATION.md](docs/MIGRATION.md)。数据库、分享状态、任务日期和总结发送回执应通过私有通道迁移；不要从空库开始后直接对已有素材正式发布。

本项目将每日额度按北京时间计算。未知提交必须先人工核实；确认已提交时记录真实帖子链接，确认未提交后才能释放保护。没有证据时保留未知状态。

```bash
bash scripts/run.sh resolve-submission ATTEMPT_ID --url 'https://www.hifini.net/thread-THREAD_ID.htm'
# 仅在人工确认未提交后使用：
bash scripts/run.sh resolve-submission ATTEMPT_ID --not-submitted
```

总结每个日期只尝试发送一次。只有旧记录证明请求尚未发出的 DNS/连接建立失败，且经过明确授权，才可使用 `daily-summary --date YYYY-MM-DD --send --retry-undelivered`；原回执保留，响应丢失或发送中断不会自动重试。

## 测试

```bash
.venv/bin/python -m unittest discover -v
find scripts -name '*.sh' -exec bash -n {} \;
```

单元测试模拟网盘、网站和通知，不读取生产认证、不实际上传或发布。GitHub Actions 在 Ubuntu 执行离线测试及 Shell 语法检查；真实账号、浏览器登录、TUN/代理和网站变更仍需部署时验收。

MIT License，保留原项目许可证。
