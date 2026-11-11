# Quark-HiFiNi

面向 Ubuntu 桌面的 Quark → HiFiNi 自动上传、发布与每日总结工具，使用 Python 工作流完成任务。

任务由 Python 和 systemd 独立运行，AI 直接调用配置的本地服务或云端接口，无需打开 ChatGPT/Codex 客户端。网盘仍需要独立安装夸克官方 CLI 和完成设备授权，浏览器需具备 HiFiNi 登录状态。

这是可配置的独立项目，不携带原设备的数据库、网盘授权、浏览器登录、密钥或定时任务。正式使用前必须完成本机授权和一次预览验收。

## 功能与边界

- 将 `upload-files/` 中命名为 `歌手 - 歌名.mp3` 的文件上传到已配置网盘的 `Share/Media`，保留云端歌曲去重及上传结果未知保护。
- 核对歌曲与歌词、检查实际 MP3 码率、创建夸克分享并准备 HiFiNi 草稿。只有显式 `--publish` 才会提交。
- 每个北京时间自然日最多确认提交一篇，结果未知时阻止自动重发。支持人工核实记录、后续审核状态检查。
- 发布与上传状态合并成每日飞书总结。歌词查询失败只保留日志和总结，不发送即时飞书告警。
- 支持本地或云端 Chat Completions 兼容 AI 接口，审核版本及歌词来源证据。默认只记录建议；歌词原文、去重、每日额度与正式提交确认由程序规则处理。

请仅处理自己有权上传、分享和发布的内容。

## Ubuntu 准备

基线为 Ubuntu 24.04 LTS、Python 3.12。还需要 Bash、curl、FFmpeg/ffprobe、符合夸克官方 Skill 要求的 Node.js，以及 Google Chrome 或可用的 Chromium 浏览器。

```bash
git clone https://github.com/danielchan-25/Quark-HiFiNi.git
cd Quark-HiFiNi
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

## 本地与云端 AI

在本机 `.env` 设置以下三个参数，定时任务会自动读取；不需要向聊天助手发送每日运行指令。

```dotenv
AI_API_URL=http://127.0.0.1:11434/v1
AI_API_KEY=
AI_MODEL=YOUR_LOCAL_MODEL
```

云端服务使用同样的参数，将 `AI_API_URL` 改为服务商提供的兼容接口，将 `AI_MODEL` 改为其模型名。无鉴权的本地服务允许空密钥；服务端支持 `auto` 别名时也可直接使用。URL 可以是服务器根地址、`/v1` 等 API 基址，或完整 `/chat/completions` 地址。请求直接发送到指定服务，HTTPS 保持证书校验，重定向不会携带密钥继续请求。

可选配置：`AI_TIMEOUT=60`（1～300 秒）、`AI_MAX_CALLS=6`（每轮最多 1～20 次）、`AI_MAX_TOKENS=4096`。默认 `AI_JSON_MODE=1` 请求 JSON 输出；不支持此参数的接口可设置为 `0`，回复仍须通过严格校验。无效回复最多重试一次，计入该轮预算。`AI_MODE=shadow` 记录审核建议，`AI_MODE=off` 关闭 AI。模型只能基于提供的文件名、页面摘要和歌词行做结构化选择；不会把账号密钥、Cookie、网盘 FID 或授权文件发给模型。兼容接口返回的选择以 one-hot 形式保存到旧审计结构，这只是选择编码，不是校准过的概率。

```bash
bash scripts/run.sh test-ai       # 真实调用配置的模型，绕过缓存，不操作网盘或发帖
bash scripts/run.sh check         # 显示当前 AI provider/model 和本机依赖
bash scripts/daily-run.sh         # 工作流预览
```

AI 运行结果、响应模型、耗时和服务端报告的 token 用量保存在 `ai_judgments`；超时、鉴权失败或无效回复会明确记录，单轮熔断和预算限制避免重复请求。成功结果按接口与模型区分缓存。只有三项 `AI_*` 连接参数全部为空时，才回退到旧 `TYPESAFE_*` 配置；旧配置不影响新接口。

夸克 CLI 的安装路径与历史授权渠道名称属于网盘适配器配置，与 AI 模型无关。已有设备授权可以继续复用；迁移到新机器时仍须按官方流程授权。每轮工作流首次使用 CLI 时检查安装，之后复用该轮检查结果。

## 手动任务

```bash
# 上传预览
bash scripts/run.sh upload --dry-run --session-input '预览音乐上传' --session-id 'UNIX_SECONDS-ABC123'

# 当日正式上传，日期防重复执行
bash scripts/run.sh upload --apply --slot-date YYYY-MM-DD --session-input '执行音乐上传' --session-id 'UNIX_SECONDS-ABC123'

# 正式发布，一次最多一篇
bash scripts/run.sh run --publish --session-input '执行一次正式发布' --session-id 'UNIX_SECONDS-ABC123'

# 人工补发指定目录（可重复 --only；--daily-limit 只覆盖本次进程的每日额度）
bash scripts/run.sh run --publish --only '张敬轩 - 笑忘书' --daily-limit 3 \
  --session-input '人工补发笑忘书' --session-id 'UNIX_SECONDS-ABC123'

# 本地预览每日总结；加 --send 才发送
bash scripts/run.sh daily-summary --date YYYY-MM-DD
```

示例中的 `UNIX_SECONDS-ABC123` 必须替换成当轮 Unix 秒时间戳、连字符和六位随机字母数字；同轮所有夸克操作复用该标识，`--session-input` 使用该轮真实指令原文。

## 定时运行

默认采用 **systemd 用户级 timer**：北京时间 06:00 上传与发布、23:55 每日总结。配置文件随仓库提供，生成到当前用户后仍需手动启用。

```bash
bash scripts/install-systemd.sh
systemctl --user daemon-reload
bash scripts/monitor.sh init
systemctl --user enable --now quark-hifini-daily.timer quark-hifini-summary.timer quark-hifini-monitor.timer quark-hifini-backup.timer
systemctl --user list-timers 'quark-hifini-*'
journalctl --user -u quark-hifini-daily.service
```

安装器会将当前仓库路径写入用户服务配置。定时器不自动补跑关机期间错过的时段；历史恢复应核实记录后显式执行。机器需要开机，并具备可用用户服务、浏览器和网络环境。桌面退出后的浏览器运行不能仅靠启用 timer 保证。

可见 Chrome 依赖已登录的桌面会话及正确的 `DISPLAY`、`WAYLAND_DISPLAY`、`DBUS_SESSION_BUS_ADDRESS` 等会话环境；必要时在桌面终端执行 `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS`。无桌面或退出登录后需要运行时，应先在本机验证授权可用的 headless 配置，不能把开启 linger 当成浏览器可用的证明。

日常执行入口是 `bash scripts/daily-run.sh --execute`；先运行上传，再检查本机运行环境。未确认提交只隔离对应歌曲，不阻止后续日期处理其他歌曲。正式运行需要准确的任务原文，默认读取 `scripts/daily-task.txt`，可用 `--session-input-file` 指定文件。不要把自动化平台和 systemd 同时配置为正式执行者。

停止定时任务：

```bash
systemctl --user disable --now quark-hifini-daily.timer quark-hifini-summary.timer quark-hifini-monitor.timer quark-hifini-backup.timer
```

## 日志与运行监测

每日任务和总结通过 `scripts/monitor.sh run daily` / `run summary` 记录一次真实执行；上传、预检、浏览器和发布阶段分别记录开始时间、耗时和退出码。标准输出和错误输出保存在 `logs/monitor/<任务>/<日期>/`，每次日志最多三份、每份 10 MiB，保留 30 天；`data/monitor/runs/` 保存运行摘要及对应业务运行 ID、实际提交结果、上传计数和警告类型，保留 90 天。日志经过密钥脱敏，仍属于本机私有运行数据，不提交 Git。

独立巡检每 5 分钟运行，检查两个业务定时器是否启用、计划时间后 10 分钟是否启动、每日任务是否超过 45 分钟、总结是否超过 3 分钟、是否异常中断，以及数据库、未确认提交和磁盘余量。06:00 及 23:55 的计划继续按北京时间执行；跨午夜仍会核对前一晚的总结。执行 `init` 后只观察后续计划，不对迁入的 Windows 历史补跑或补发。

巡检异常沿用 `.env` 的飞书通道，记录发送状态、curl 退出码、远端错误码及脱敏错误原因。明确未发送的通知可间隔 5 分钟有限重试，最多三次；响应丢失、发送中断及旧 UNCONFIRMED 记录不自动重发。成功提交后其余候选素材处理失败，显示为 `PARTIAL_FAILURE` 警告并保存详情。上传失败、未确认提交和真正执行失败仍告警。监测不会自动重跑上传、发帖或每日总结。

日志或监测状态写入失败时，输出降级至 journal，业务子进程继续执行；监测存储问题与真实提交结果分开记录。巡检本身检查不完整时，不把旧故障标记为已恢复。工作流前置失败同时保存到 `data/workflows/`，日报据此报告预检、浏览器等阻断原因；同一文件当天失败后恢复成功，会显示最终状态及恢复数量。网站明确拒绝的素材进入人工处理，旧拒绝记录在下次执行时也会识别，避免重复提交。

“本地预检”检查数据库、运行程序和配置。每次检查把具体阻断原因及检查时间保存到 `data/preflight/runs/` 和 `data/preflight/latest.json`，定时任务按工作流 ID 关联证据；待核实歌曲不再构成全局预检失败。检查本身不调用 AI 或发帖。

日报保留原任务是否执行的事实，并区分“当时受阻”和“后来预检已恢复”。监测只有在原任务因预检受阻、且存在更晚的预检通过记录时才解除该项活动告警；数据库、定时器、未知提交和真正发布失败仍分别检查。旧工作流及已发送汇总不改写、不自动重发，预检恢复也不会自动补跑旧任务。

```bash
# 最近执行、最后巡检和证据路径；不访问外部服务
bash scripts/monitor.sh status
# 立即本地巡检，不发送通知
bash scripts/monitor.sh check
# 巡检和告警发送结果
tail -n 30 logs/monitor/watchdog.log
journalctl --user -u quark-hifini-monitor.service --since today
```

发现问题时先检查 `data/monitor/last-check.json` 和对应运行 JSON，再按其中的 `log_path` 查询完整日志。`evidence.attempts` 的 `CONFIRMED` 表示确认提交，`UNKNOWN` 表示必须人工核实；不能仅凭非零退出码重跑任务。机器关机时无法实时告警，开机后的巡检会检查观察期内当天及前一天应执行的计划。若巡检自身停止，需通过 `systemctl --user list-timers 'quark-hifini-*'` 及 journal 核对。

## 迁移与状态

### MySQL 存储

部署可通过 `STORAGE_BACKEND=mysql` 使用 MySQL 8。当前部署的数据库为 `quark`，歌曲主表为 `hifini`；运行、事件、草稿、上传、AI 审核、历史迁移等表均以 `hifini_` 开头，不访问同库其他项目的表。

`hifini_state` 保存歌词词条补录、歌词验证结果、汇总发送回执、工作流、预检、监测与备份状态及导入的原始文件；`hifini_logs` 保存新增日志。MySQL 模式不再写本地 SQLite、状态 JSON、应用日志或文件锁，采用数据库事务和命名锁；数据库不可用时停止任务，不回退空库或本地状态。预览使用内存数据库隔离业务变更，第三方 CLI 的临时输出放在 `/tmp` 并自动清理。程序、连接凭据、浏览器和网盘设备授权仍由本机管理，`upload-files/` 是用户提供新文件的输入目录。

先备份、停止本项目任务、用 `migrate_mysql.py` 导入并逐行校验，再启用 MySQL 配置；不能仅改连接地址后从空库运行。迁移保持原主键、提交日期、防重发状态、历史原始 JSON 和通知回执，导入文件按字节校验。迁移工具默认只预览，`--execute` 才导入，不会替你切换生产。

```bash
bash scripts/run.sh check
bash scripts/run.sh logs --task daily --date 2026-11-09 --lines 100
```

MySQL 模式下，03:30 备份导出本项目的 MySQL 表、状态及日志，并在唯一的临时表前缀下真实恢复、逐行比较，然后清除这些测试表。完整归档继续保存在 `/mnt/data/backup/Quark-HiFiNi/`，执行记录在 `/mnt/data/record/Quark-HiFiNi/`；这些长期备份独立于正式运行数据，不放在本机项目的数据目录。

每天北京时间 **03:30** 自动备份，默认保留 **14 天**。SQLite 模式使用一致性备份 API，在本地临时目录创建快照后释放业务锁，再传输到 `/mnt/data/backup/Quark-HiFiNi/daily/`；MySQL 模式使用上文的表导出和隔离恢复。备份包含业务数据库、回执、运行状态、本机 `.env`、源代码和本应用的用户服务配置，不含虚拟环境或浏览器 profile。凭据只进入私有备份，不提交 Git。

每份备份验证成员校验和、远端整包 SHA-256，并在隔离临时目录恢复数据库，检查完整性、外键及各表数量。审计和清单放在 `/mnt/data/record/Quark-HiFiNi/daily-backup/`；`data/backup/last-backup.json` 保存最近结果。监测会检查备份失败、定时器停用以及超过 26 小时没有成功备份。首次部署后可立即运行一次备份验收；其后由 timer 执行。

```bash
bash scripts/backup.sh                 # 只预览
bash scripts/backup.sh --execute       # 生成并校验私有备份
```

恢复备份应在隔离目录检查后进行；不能用旧数据库覆盖已产生的新提交记录来解除防重发保护。

迁移步骤和验收清单见 [docs/MIGRATION.md](docs/MIGRATION.md)。数据库、分享状态、任务日期和总结发送回执应通过私有通道迁移；不要从空库开始后直接对已有素材正式发布。

本项目将每日额度按北京时间计算。未确认提交的歌曲进入“待核实”（资源状态 `SUBMISSION_UNKNOWN`，提交记录 `UNKNOWN`），立即通过飞书告警请用户确认。该歌曲不会自动重发，未确认提交仍占用提交当天的额度；后续日期可正常处理其他歌曲。`check` 会列出待核实记录，但不因它们单独返回失败。日报单独列出待核实歌曲，监测持续告警，均不自动重跑发布。

确认已发布时记录真实帖子链接；用户已确认但暂时没有链接时可使用 `--submitted`，保留原提交日期，随后补充链接。只有人工明确确认未发布后，才能解除该歌曲的保护。没有证据时保留待核实状态。

```bash
bash scripts/run.sh resolve-submission ATTEMPT_ID --url 'https://www.hifini.net/thread-THREAD_ID.htm'
# 用户确认已发布，暂时没有链接：
bash scripts/run.sh resolve-submission ATTEMPT_ID --submitted
# 仅在人工确认未提交后使用：
bash scripts/run.sh resolve-submission ATTEMPT_ID --not-submitted
```

总结每个日期只尝试发送一次。只有旧记录证明请求尚未发送（例如未配置 webhook 或单次 DNS/连接建立失败），且经过明确授权，才可使用 `daily-summary --date YYYY-MM-DD --send --retry-undelivered`；原回执保留，响应丢失或发送中断不会自动重试。普通通知可用 `retry-alert ALERT_ID` 显式恢复；同样只接受有证据的未发送记录，未知结果会被拒绝。

## 测试

```bash
.venv/bin/python -m unittest discover -v
find scripts -name '*.sh' -exec bash -n {} \;
```

单元测试模拟网盘、网站和通知，不读取生产认证、不实际上传或发布。GitHub Actions 在 Ubuntu 执行离线测试及 Shell 语法检查；真实账号、浏览器登录、TUN/代理和网站变更仍需部署时验收。

MIT License，保留原项目许可证。

### 面板共享状态与备份

歌词人工词条与验证结果通过 `hifini_state` 的 `data/lyrics_overrides.json`、`data/lyrics_override_status.json` 虚拟路径共享，面板和定时任务均读写同一数据库文档，不创建对应本地文件。多词条及验证结果使用固定顺序的事务行锁，修改链接或清除词条后会丢弃旧链接的异步验证结果。

`/root/quark-panel` 的状态保存在 `panel_state`，浏览器在线率样本保存在 `panel_cdp_samples`，面板应用日志保存在 `panel_logs`。本机 schema 标记显式声明这三张附加表，每日 03:30 的备份同时导出并真实隔离恢复这 21 张应用表，保留面板程序和服务配置，不包含未声明的其他业务表。

状态数据库不可用时明确失败，不使用空状态或本地副本。程序、依赖环境、连接配置与第三方浏览器／网盘授权仍保留在对应应用位置；音乐原文件保留在夸克，`upload-files/` 仅作为本地文件上传入口。
