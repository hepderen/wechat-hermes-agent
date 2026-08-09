# 生产部署

## 适用范围

仓库内的 `install_cloud.sh` 用于迁移一台已经运行 Linux 微信、旧 Bridge/AI 服务和 Hermes 源码环境的 Ubuntu 服务器。脚本会检查微信 PID、三个受保护状态文件、服务状态、固定目录和 Hermes Python 3.11 运行时，并安装版本化 Adapter/Hermes 目录。

新机器部署需要先完成 Linux 微信、数据库解密密钥、Hermes Agent 和对应目录布局，再按实际路径调整 installer 与 systemd 单元。

## 参考拓扑

| 服务 | 地址 | 用户 |
| --- | --- | --- |
| `wechat-chat-api.service` | `127.0.0.1:8765` | `ubuntu` |
| `linux-wechat-bridge.service` | 无监听端口 | `ubuntu` |
| `wechat-hermes-adapter.service` | `127.0.0.1:8000` | `wechat-hermes` |
| `hermes-worker.service` | `127.0.0.1:8642` | `wechat-hermes-runner` |
| `wechat-searxng.service` | `127.0.0.1:8651` | rootless container |

不要把这些端口发布到公网。跨主机部署需要增加经过身份认证的私网传输，并重新评估本仓库的回环 URL 启动校验。

## 前置条件

- Ubuntu、systemd、Python 3.11、`venv`、`curl`、`rsync`、`acl` 和 `bubblewrap`。
- 一个持续运行的 Linux 微信桌面进程和可用 DISPLAY。
- 微信消息数据库路径、对应数据库密钥 JSON 和目标群 ID。
- Hermes Agent 源码 Git checkout，且 `venv/bin/hermes` 可执行。
- 旧服务和状态文件完整，已有回滚演练。
- 磁盘可容纳 1 GB 下载、500 MB 任务 Artifact 和版本化发布副本。

## 配置文件

### Chat API

从 `chat-api/config.example.json` 创建生产 `config.json`。关键字段：

| 字段 | 说明 |
| --- | --- |
| `db_path` | 微信消息数据库绝对路径 |
| `keys_file` | 数据库密钥 JSON，权限 `0600` |
| `db_key_name` | 密钥 JSON 内对应数据库的键名 |
| `cache_dir` | Chat API 私有快照与发送状态目录 |
| `outbound_control_db` | 独立发送栅栏 SQLite |
| `group_id` | 精确的 `...@chatroom` ID |
| `bot_wxid` | 机器人真实 wxid |
| `window_*` / `*_point` | 当前微信版本和分辨率下的发送 UI 参数 |

`db-config.json` 放在 Bridge 工作目录，配置 Chat API URL、群 ID、轮询和文字分块。`chat_structured_metadata_wait_seconds` 默认是 `2.0`，用于等待同一数据库记录补齐原生 `@` 或引用 XML；取值限制在 `0-10` 秒，设为 `0` 可关闭等待。可见 mention 只用于决定是否等待，不能触发 Agent。结构化 Bridge 没有 OCR 路径。

### 四类令牌

分别生成四个随机值：

```bash
openssl rand -base64 48
openssl rand -base64 48
openssl rand -base64 48
openssl rand -base64 48
```

| 令牌 | 持有方 | 用途 |
| --- | --- | --- |
| `BRIDGE_TOKEN` | Bridge、Adapter | 认证 `/api/chat` 可信入口 |
| `HERMES_WECHAT_INTERNAL_TOKEN` | Adapter、Hermes | 认证 MCP 内部工具 |
| `WECHAT_CHAT_API_TOKEN` | Bridge、Adapter、Chat API | 认证发送与控制接口 |
| `HERMES_API_KEY` / `API_SERVER_KEY` | Adapter、Hermes | 认证 Hermes API |

环境文件权限设为 `root:root 0600`：

```text
/etc/wechat-hermes/adapter.env
/etc/wechat-hermes/hermes.env
/etc/wechat-hermes/chat-api.env
/etc/wechat-hermes/bridge.env
```

参考值分别位于 `adapter/deploy/*.env.example` 和 `chat-api/*.env.example`。模型供应商密钥只放在 Hermes 私有环境或配置，不写入仓库、任务提示和共享记忆。

## 白名单与权限

`ALLOWED_WECHAT_ROOM_IDS` 接受逗号分隔的精确群 ID。白名单群内成员拥有相同 Agent 能力；权限仍受 Worker 用户、工具策略、目录、网络、费用和资源上限约束。`ALLOW_PRIVATE_WECHAT_CHAT=false` 是默认值。

## 本地候选验收

先在发布源码上运行：

```bash
python3.11 -m venv /tmp/wechat-hermes-test
/tmp/wechat-hermes-test/bin/python -m pip install -r requirements-ci.txt
(cd adapter && /tmp/wechat-hermes-test/bin/python -m pytest -q)
(cd chat-api && /tmp/wechat-hermes-test/bin/python -m pytest -q)
(cd web-research && /tmp/wechat-hermes-test/bin/python -m pytest -q)
/tmp/wechat-hermes-test/bin/python adapter/scripts/live_fake_stack.py
```

自动测试只使用假发送端。生产切换前不要向真实群发送自动探测消息。

## 迁移安装器

将完整仓库放入候选目录，记录当前基线：

```bash
export EXPECTED_WECHAT_PID=PID
export EXPECTED_DB_STATE_SHA256=HASH
export EXPECTED_SEND_STATE_SHA256=HASH
export EXPECTED_BOT_DB_SHA256=HASH
export EXPECTED_DB_STATE_INODE=DEVICE:INODE
export EXPECTED_SEND_STATE_INODE=DEVICE:INODE
export EXPECTED_BOT_DB_INODE=DEVICE:INODE
export ALLOWED_ROOM_ID=ROOM_ID
export BOT_WXID=BOT_WXID
export RELEASE_ROOT=/var/lib/wechat-hermes/candidates/core/RELEASE_ID
```

哈希使用 `sha256sum`，inode 使用 `stat -c '%d:%i'`。确认变量来自当前服务器，而不是旧记录。

安装器需要 root，并且默认不会启动新服务：

```bash
sudo --preserve-env \
  bash adapter/deploy/install_cloud.sh
```

它会：

1. 复核微信 PID、旧服务和受保护文件。
2. 创建独立服务账户与受限目录。
3. 备份并迁移 Adapter SQLite。
4. 安装版本化 Adapter 和 Hermes runtime。
5. 应用 Hermes 日志、API scope、工具证据和 Skill reload 加固补丁。
6. 安装 Bubblewrap 专用 AppArmor `userns` 配置并执行非 root 沙箱自检。
7. 生成部署专属 Skills 完整性锁，其中包含无代码的混合人格 Skill。
8. 安装 Chat API、结构化 Bridge、systemd、cleanup timer 和 logrotate。
9. 写入四个隔离环境文件。

该版本将 `HERMES_WECHAT_SESSION_GENERATION` 设为 `3`，使已有群聊创建带新人格提示词的新 Hermes Session；旧 Session 只保留为历史记录。

脚本固定使用 `/home/ubuntu`、`/opt`、`/var/lib/wechat-hermes` 和 `/etc/wechat-hermes`，路径不同的环境应先修改并重新运行安装契约测试。

## 搜索候选

搜索依赖不直接提交到 Git。构建脚本按哈希锁下载 Trafilatura 及依赖，并创建隔离 Hermes home：

```bash
sudo bash web-research/scripts/build_candidate.sh \
  "$PWD/web-research" RELEASE_ID
```

构建结果位于 `/var/lib/wechat-hermes/candidates/web-research/RELEASE_ID`。依次运行只读 probe、候选 Gateway 和压力脚本，确认国内、国外、新闻、URL 提取、缓存与熔断。通过后执行：

```bash
sudo WECHAT_PID=PID \
  bash web-research/scripts/install_production.sh \
  /var/lib/wechat-hermes/candidates/web-research/RELEASE_ID RELEASE_ID
```

默认 `WECHAT_WEB_SEARX_MERGE_ENABLED=false`。SearXNG 仍作为可选后端运行；显式启用合并前，应确认其引擎在服务器网络环境下稳定。

## 切换

建议在维护窗口按以下顺序操作：

1. 记录微信 PID、启动时间、服务状态、端口和受保护文件哈希/inode。
2. 启动并检查 Chat API 与 Hermes Worker。
3. 确认 `127.0.0.1:8765/health`、`:8642/health` 正常。
4. 停止占用 `127.0.0.1:8000` 的旧 AI 服务。
5. 启动 Adapter，等待 `/health` 中 `ready=true`。
6. 重启 Structured Bridge，使其加载 `bridge.env` 和新代码。
7. 启用 cleanup timer；检查 `/metrics` 与 journal。
8. 再次确认微信 PID和三个受保护文件未变化。

首次正常用户请求作为生产烟雾测试，不自动向群发送测试内容。

## 运行检查

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/metrics
curl -fsS -H "Authorization: Bearer TOKEN" \
  http://127.0.0.1:8765/health
systemctl --no-pager --full status \
  wechat-chat-api linux-wechat-bridge \
  hermes-worker wechat-hermes-adapter
```

日志审计应只出现 `request_id/task_id/run_id/room_id/sender_id`、状态、耗时、大小和错误类型。

## 回滚

切换前将旧 Adapter/Hermes 环境保存到 root 私有备份目录。回滚脚本要求该目录包含 `adapter.env` 和 `hermes.env`：

```bash
sudo EXPECTED_WECHAT_PID=PID \
  bash adapter/deploy/rollback_to_legacy.sh /root/private-backup/RELEASE_ID
```

回滚停止新 Adapter/Hermes，恢复旧环境并启动旧 AI 服务。保留新 SQLite、Artifact、栅栏和发送状态用于对账；不要清理数据库游标或 `uncertain` 媒体。

搜索模块使用它自己的版本化回滚：

```bash
sudo WECHAT_PID=PID \
  bash web-research/scripts/rollback_production.sh RELEASE_ROOT
```

## 密钥轮换

模型供应商凭据使用 `rotate_hermes_model.py` 轮换。脚本只接受权限为
`0600` 的 JSON 文件，不接受命令行 Key；默认仅执行模型列表查询和最小聊天
预检，不修改配置。模型别名只有在供应商 `/models` 返回唯一匹配时才会解析，
例如 `5.6sol` 可以解析到唯一的 `gpt-5.6-sol`，歧义或未命中都会终止。

先在仓库外创建一次性文件：

```bash
sudo install -d -m 0700 /root/wechat-hermes-secrets
sudo install -m 0600 /dev/stdin \
  /root/wechat-hermes-secrets/hermes-model-secret.json <<'JSON'
{"api_key":"PROVIDER_KEY","model":"MODEL_OR_ALIAS"}
JSON
```

记录配置哈希并执行只读预检：

```bash
CONFIG=/var/lib/wechat-hermes/workspace/home/.hermes/config.yaml
SECRET=/root/wechat-hermes-secrets/hermes-model-secret.json
EXPECTED_SHA256=$(sudo sha256sum "$CONFIG" | awk '{print $1}')

sudo /opt/hermes-runtime/venv/bin/python \
  adapter/scripts/rotate_hermes_model.py \
  --config "$CONFIG" \
  --secret-file "$SECRET"
```

确认输出中的 `chat_status=200` 等价字段、解析后的模型 ID 和配置哈希后，使用
相同哈希执行原子替换：

```bash
sudo /opt/hermes-runtime/venv/bin/python \
  adapter/scripts/rotate_hermes_model.py \
  --config "$CONFIG" \
  --secret-file "$SECRET" \
  --expected-sha256 "$EXPECTED_SHA256" \
  --apply

sudo systemctl restart hermes-worker.service
sudo /opt/wechat-hermes-adapter/.venv/bin/python \
  /opt/wechat-hermes-adapter/scripts/smoke_cloud.py --read-only
sudo rm -f -- "$SECRET"
```

每次成功替换都会在 `/var/backups/wechat-hermes/model-rotation-*` 创建权限为
`0600` 的旧配置和无密钥 manifest。`wechat-hermes-adapter.service` 依赖 Hermes，
因此重启 Worker 时 Adapter 会被 systemd 有序重启；微信、Chat API 和 Bridge
不在该重启事务中。切换后应再次核对微信 PID、受保护文件 inode/hash、三个
健康端点以及同步 Session 和异步 Run。

其他服务令牌应在持有双方写入新值后按依赖顺序重启。模型供应商、GitHub、
搜索或云密钥还应在供应商控制台撤销旧值。历史 Git 中出现过的密钥应视为已
公开，仅删除当前文件不构成轮换。
