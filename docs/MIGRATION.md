# Windows → Ubuntu 迁移模板

## 迁移范围

目标：Ubuntu 桌面，当前用户运行 Python、浏览器和 systemd 用户服务。保留歌曲去重、实际码率检测、歌词校验、每日一篇、未知提交保护和飞书回执。不要通过更改历史时间、删除失败记录或清空数据库来解除限制。

本仓库只提供程序与模板，不能代替原设备运行状态。切换前记录最后成功提交的帖子、未知提交数量、上传结果未知记录、总结回执及剩余待处理任务。

## 一、准备新机器

1. 克隆仓库，安装 Python 3.12、venv、Bash、curl、FFmpeg、Node.js、Chrome/Chromium。
2. 在 Ubuntu 重建 `.venv`；Windows 的虚拟环境和二进制工具不能复制使用。
3. 本机创建 `.env`，填写配置，确保不被 Git 跟踪。夸克官方 Skill 安装在用户全局目录，完成新设备授权。
4. 启动独立 Chrome profile，登录 HiFiNi，确认 CDP 仅监听本机。不要把远程调试端口暴露到局域网或公网。
5. 检查代理、TUN、DNS：HiFiNi、歌词来源、夸克授权/下载服务和飞书均需真实连通。旧系统的直连规则不能假定自动生效。

## 二、冻结旧端并复制状态

1. 停用旧端正式定时任务，等待所有上传、发布、通知进程结束。整个迁移期间只允许一个正式执行端。
2. 使用 SQLite 一致性备份复制 `data/quark-hifini.db`。若仍有 WAL，不能只复制主数据库。示例在旧项目根目录执行：

   ```python
   import sqlite3
   from pathlib import Path
   source_path = Path('data/quark-hifini.db').resolve()
   source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
   destination = sqlite3.connect('quark-hifini.migration.db')
   source.backup(destination)
   destination.close()
   source.close()
   ```

3. 将备份通过私有通道放到 Ubuntu 的 `data/quark-hifini.db`，保留原机备份。运行 `PRAGMA quick_check` 并核对数量。
4. 一并迁移 `data/daily-summaries/`、恢复记录，以及业务实际需要的 `data/` 状态文件。不要迁移 Windows 的工具二进制、浏览器 profile 和包含 Windows 路径的 `runtime.json`。
5. `upload-files/` 只复制仍待上传的实际文件，不根据历史日志重新生成文件。旧 `uploads.local_path` 保留为审计证据，不批量覆盖；新任务写入 Linux 路径。
6. 认证配置单独创建或重新授权，禁止上传 GitHub。Chrome 的 Cookie 可能依赖旧操作系统的加密存储，应重新登录。

## 三、验收

- [ ] 全部离线单元测试通过，Shell 脚本语法通过。
- [ ] `scripts/run.sh check` 能识别 Bash、curl、Node.js、ffprobe、Skill 和数据库；未知提交已核实或明确保留。
- [ ] 能从独立浏览器获取完整“我的帖子”列表，正确识别审核状态。
- [ ] 夸克授权有效，读取 `Share/Media` 并核对原有资源，无新增分享或重复上传。
- [ ] 上传 dry-run 与发布 dry-run 输出符合预期；没有正式提交。
- [ ] 经授权用一个受控样例验证通知；不要重发旧 `UNKNOWN`/`SENDING` 消息。
- [ ] 检查北京时间跨日的额度、任务日期去重和总结归属。
- [ ] 验证每日入口在发布被阻止时仍执行上传，上传失败也保留独立记录。

仅在上述确认后，启用新端 timer，并进行一次正式受控运行。首次正式成功后只读核对帖子链接、上传核验和飞书回执。

## 四、回滚

先停用 Ubuntu 的定时器和正在执行的正式任务，再核对新端是否产生成功或未知提交。将新端完整数据库和回执安全同步到旧端；待核实歌曲及提交记录必须完整迁移，保持这些歌曲不可自动重发，其他歌曲可按后续日期的额度继续处理。不能直接使用迁移前旧数据库恢复，以免重复提交或重复通知。

## 待新设备验证

真实浏览器/桌面会话、夸克账号授权、目标网络/TUN 规则和休眠唤醒行为须在 Ubuntu 实机确认。离线测试和容器测试不能证明这些外部条件可用。
