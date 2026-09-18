# Quark-HiFiNi

一个面向音乐资源发布场景的本地自动化工具：从夸克网盘的 `Share/Media` 扫描素材，获取百度百科歌词，生成 HiFiNi 发帖草稿，并在人工明确确认后完成发布与后续审核追踪。

项目把目录筛选、分享、歌词查找、格式排版、重复检查和审核跟踪集中为可审计流程，避免重复发帖、错误链接和漏看审核结果。

## 功能

- 检查夸克登录状态和 `Share/Media` 工作目录。
- 识别“歌曲名 - 歌手名”及“歌手 - 歌曲名”；混合曲自动跳过。
- 通过百度百科验证歌手、歌曲与“歌曲歌词”章节，保留歌词逐行布局。
- 自动创建无需提取码的公开夸克分享链接。
- 自动选择 HiFiNi 分区：华语、日韩或欧美。
- 正文先写歌词；网盘链接放在底部，与歌词保留两行空白，并设为“回复可见”。
- 每次最多提交 1 篇、每日最多提交 3 篇。
- 提交后主题出现在“我的帖子”即记为成功提交；审核结果留待后续任务检查。
- 每次任务开始检查待审核主题：审核中推送飞书等待时长，审核失败告警，但不阻塞当天新任务。
- SQLite 保存素材、分享、提交、审核和失败次数；JSONL 保存逐步运行日志。

## 为什么需要它

手工发布音乐资源包含许多容易遗漏的步骤。任一步骤出错都可能造成重复发帖、错误链接或漏看审核结果。本项目将这些约束集中为可重复执行的状态机，同时保留最终发布前的人工确认。

## 环境要求

- Windows 10/11
- Python 3.11+
- Node.js 16+
- 已安装并授权的夸克网盘官方 Skill
- Git Bash（默认路径：`D:\Git\bin\bash.exe`）
- Chrome 已开启远程调试，并登录 HiFiNi；默认 CDP 地址为 `http://127.0.0.1:9501`

## 配置

复制模板并填写真实值：

```powershell
Copy-Item .env.example .env
```

`.env` 仅包含以下配置，且始终被 Git 忽略：

```env
# 可选：飞书告警
FEISHU_WEBHOOK=
FEISHU_SECRET=

# 必填：HiFiNi 登录信息
HIFINI_USERNAME=
HIFINI_PASSWORD=

# 可选：不填时使用 http://127.0.0.1:9501
HIFINI_CDP_URL=http://127.0.0.1:9501
```

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 手动部署与运行

先打开固定 Chrome 并登录 HiFiNi、确认夸克 Skill 已授权，然后执行安全演练：

```powershell
python -B main.py run --dry-run
```

演练不会点击“发表主题”，且会保留本次新建的 Chrome 标签页供人工检查。

正式发布只在人工检查草稿并明确允许后执行：

```powershell
python -B main.py run --publish
```

正式运行结束时，程序只关闭本次新建的任务标签页，始终保留 Chrome 和既有标签页在线。

飞书配置测试：

```powershell
python -B main.py test-feishu
```

## AI / Codex 部署建议

1. 让 AI 仅执行 `--dry-run`，产出可人工审阅的发帖页。
2. 人工检查标题、分区、歌词、分享链接和“回复可见”设置。
3. 仅在准备点击“发表主题”时，对该次发布给予明确确认。
4. 通过任务计划程序按日运行；建议先安排 `--dry-run`，稳定后再由人工触发正式发布。

## 状态说明

| 状态 | 含义 |
| --- | --- |
| `DRAFT_READY` | 草稿已生成，尚未提交。 |
| `SUBMITTED` | 主题已出现在“我的帖子”，等待下次任务审核。 |
| `PENDING_REVIEW` | 主题页显示“该主题正在由管理员审核中……”。 |
| `PUBLISHED` | 后续检查确认主题页可正常访问。 |
| `REJECTED` | 主题页显示审核失败、删除或不可访问。 |
| `FAILED` | 当前处理步骤失败；下次任务才会再次尝试。 |
| `SKIPPED_TOO_MANY_FAILURES` | 失败次数超过 3 次，停止自动尝试。 |

## 本地数据与隐私

项目运行时只会在本地生成两类数据：

- `data/quark-hifini.db`：SQLite 状态数据库。
- `logs/YYYY-MM-DD.jsonl`：结构化运行日志。

数据库、日志、`.env`、缓存、虚拟环境和 IDE 文件均不会被推送到 GitHub。请不要在问题反馈、日志或提交信息中粘贴账号密码、WebHook、分享链接或个人资源名称。

## 许可证

本项目采用 [MIT License](LICENSE)。
