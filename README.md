# rclone 目录监视同步与每日检查

程序启动后会递归监视 `LOCAL_SYNC_DIR`。检测到文件新增、修改、移动或删除后，等待短暂的防抖时间，再执行 `rclone sync` 将目录镜像到 `RCLONE_REMOTE`。启动时也会先执行一次同步，补齐程序未运行期间的变更。

每日在 `.env` 指定的服务器本地时间，程序会在本地目录创建唯一探测文件，并用 `rclone lsjson` 确认它已到达远端；检查成功或失败都会通过 SMTP 发邮件。`--now` 仍可用于立即执行一次检查并退出。

## 准备

1. 安装 Python 3.10+ 和 rclone。用运行程序的 Linux 用户执行 `rclone config` 配好 remote，并确认 `rclone lsd your-remote:` 可访问。
2. 编辑 `.env`：填写远端目录、本地同步目录、SMTP 发件账号和收件地址。邮箱服务通常需要应用专用密码。
3. 安装依赖并运行：

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python main.py
   ```

程序需要持续运行才能执行每日计划。可通过 systemd 配置开机启动和异常重启，服务用户必须能访问 rclone 配置和同步目录。

需要立即检查一次并发送邮件时，运行：

```sh
python main.py --now
```

`--now` 模式检查并发送后就退出。检查成功退出码为 `0`，同步失败/超时为 `1`，邮件发送失败为 `3`。

## 配置说明

- `RCLONE_REMOTE`：远端及目录，例如 `mydrive:Backups/healthcheck`。
- `LOCAL_SYNC_DIR`：rclone 同步任务监控的本地目录。
- `CHECK_TIME`：每日检查时间，使用服务器本地时区及 `HH:MM` 格式。
- `SYNC_TIMEOUT_SECONDS` / `POLL_INTERVAL_SECONDS`：等待同步的最长时间和查询间隔。
- `SMTP_*`、`EMAIL_TO`：邮件服务器、发件账号和收件地址。

`rclone sync` 会让远端内容与本地目录一致，因此也会删除远端中本地已不存在的内容。每日检查的探测文件会保留在本地和远端，名称带日期及随机标识。`.env` 中含密码，已加入 `.gitignore`，请勿提交。
