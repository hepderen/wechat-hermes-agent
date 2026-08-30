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

- Ubuntu、systemd、Python 3.11、`venv`、`curl`、`rsync` 和 `acl`。
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

`db-config.json` 放在 Bridge 工作目录，配置 Chat API URL、群 ID、轮询和文字分块。`chat_structured_metadata_wait_seconds` 默认是 `2.0`，用于等待同一数据库记录补齐原生 `@` 或引用 XML；取值限制在 `0-10` 秒，设为 `0` 可关闭等待。`chat_group_listener_enabled=true` 或 Bridge 环境中的 `HERMES_WECHAT_GROUP_LISTENER_ENABLED=true` 会转发该指定群所有结构化有效的文字/引用消息；环境变量优先。关闭时保留只在控制命令、真实 `@` 或回复机器人时转发的旧行为。结构化 Bridge 没有 OCR 路径。

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
5. 应用 Hermes 日志、API scope 和工具证据加固补丁。
6. 禁用 Hermes 动态 `skills` 工具集，部署锁定提交和哈希的 CCV3 规范、MIT License、`xiaoge.card.json` 与 Sophia 署名归档；只加载卡片安全文本字段，不加载 Humanizer、语音、wife mode、todo、Telegram 或原生 memory 接口。
7. 安装 Chat API、结构化 Bridge、systemd、cleanup timer 和 logrotate。
8. 写入四个隔离环境文件。

当前聊天发布将 `HERMES_WECHAT_CHAT_ONLY=true`，并把 Hermes 默认模型切换为 `gpt-5.4-mini`。执行型消息会在 Adapter 路由层降级为普通文字会话，已有生产任务会被取消并抑制交付。Bridge 与 Adapter 均启用 `HERMES_WECHAT_GROUP_LISTENER_ENABLED=true`：未点名消息会先经过低信号过滤和房间节流，再让模型决定是否插话；它们始终禁工具且不创建任务。默认节流为 12 秒与 3 个群消息，直接文本叫“小格”可绕过节流。`HERMES_WECHAT_SESSION_GENERATION` 设为 `12`，使已有群聊创建带 CCV3 人格、群时间线和自然段落节奏的新 Hermes Session；旧 Session 只保留为历史记录。小格始终使用轻松娱乐的陪聊口吻，不提供关键词人设切换。Adapter 按群保存 24 小时、最多 120 条结构化时间线，每轮只注入最近 16 条，并把房间摘要保留最多 30 天。生产只使用群级时间线和共享摘要，不创建、读取或注入 `(room_id, sender_id)` 成员关系档案，也不运行成员定向主动消息；旧环境中的 `HERMES_WECHAT_RELATIONSHIP_*` 变量会被忽略。

Hermes 环境同时固定 `HERMES_HOME_MODE=2770`。Hermes 每次启动都会维持运行目录的组访问权限，使独立清理用户能够执行日志和 Session 保留策略；文件本身继续使用私有权限。

脚本固定使用 `/home/ubuntu`、`/opt`、`/var/lib/wechat-hermes` 和 `/etc/wechat-hermes`，路径不同的环境应先修改并重新运行安装契约测试。

## SSH 管理面加固

公网 SSH 使用密码认证时，持续爆破会占用未认证连接槽并拖高小规格主机负载。仓库提供 `adapter/deploy/sshd-wechat-hermes.conf`，关闭密码、交互式和 root 登录，只允许 `ubuntu` 使用公钥，并收紧 `LoginGraceTime`、`MaxAuthTries` 与 `MaxStartups`。

先保持当前管理会话，在第二个全新会话确认生产密钥有效：

```bash
ssh -o BatchMode=yes -i KEY ubuntu@HOST true
```

确认后安装并 reload；reload 不终止已有 SSH 会话：

```bash
sudo install -o root -g root -m 0644 \
  adapter/deploy/sshd-wechat-hermes.conf \
  /etc/ssh/sshd_config.d/00-wechat-hermes.conf
sudo sshd -t
sudo systemctl reload ssh.service
ssh -o BatchMode=yes -i KEY ubuntu@HOST true
```

若生产管理账户不是 `ubuntu`，先修改配置中的 `AllowUsers`。不要在最后一个可用管理会话内直接套用未经验证的账户或密钥设置。

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

默认 `WECHAT_WEB_SEARX_MERGE_ENABLED=false`，并启用
`WECHAT_WEB_DOMESTIC_MERGE_ENABLED=true`。中文查询会把固定国内搜索端点与
国际结果共同重排；每个国内端点有独立熔断。SearXNG 仍作为可选后端运行，
显式启用合并前应确认其引擎在服务器网络环境下稳定。

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

## 已部署服务升级

已运行的实例更新 CCV3 人格或发送逻辑时，不运行完整安装器。先将公开仓库固定到待发布提交，记录当下的 PID、哈希和 inode，再使用三个仅替换发布文件的脚本。它们不会重启微信或 Hermes Worker；任一步失败会恢复该服务的前一版本。

~~~bash
REPO=/var/lib/wechat-hermes/candidates/ccv3-RELEASE_ID
COMMIT=FULL_40_CHARACTER_COMMIT
RELEASE_ID=ccv3-SHORT_COMMIT

# 从当前服务器读取，不能复用旧记录。
EXPECTED_WECHAT_PID=$(pgrep -x wechat)
EXPECTED_DB_STATE_SHA256=$(sudo sha256sum /home/ubuntu/linux-wechat-bot/db-state.json | awk '{print $1}')
EXPECTED_SEND_STATE_SHA256=$(sudo sha256sum /home/ubuntu/.cache/wechat-chat-api/send-state.json | awk '{print $1}')
EXPECTED_BOT_DB_SHA256=$(sudo sha256sum /opt/wechat-ai-bot/data/bot.db | awk '{print $1}')
EXPECTED_DB_STATE_INODE=$(sudo stat -c '%d:%i' /home/ubuntu/linux-wechat-bot/db-state.json)
EXPECTED_SEND_STATE_INODE=$(sudo stat -c '%d:%i' /home/ubuntu/.cache/wechat-chat-api/send-state.json)
EXPECTED_BOT_DB_INODE=$(sudo stat -c '%d:%i' /opt/wechat-ai-bot/data/bot.db)

sudo env SOURCE_ROOT="${REPO}/adapter" RELEASE_ID="${RELEASE_ID}" \
  EXPECTED_SOURCE_COMMIT="${COMMIT}" \
  EXPECTED_WECHAT_PID="${EXPECTED_WECHAT_PID}" \
  EXPECTED_DB_STATE_SHA256="${EXPECTED_DB_STATE_SHA256}" \
  EXPECTED_SEND_STATE_SHA256="${EXPECTED_SEND_STATE_SHA256}" \
  EXPECTED_BOT_DB_SHA256="${EXPECTED_BOT_DB_SHA256}" \
  EXPECTED_DB_STATE_INODE="${EXPECTED_DB_STATE_INODE}" \
  EXPECTED_SEND_STATE_INODE="${EXPECTED_SEND_STATE_INODE}" \
  EXPECTED_BOT_DB_INODE="${EXPECTED_BOT_DB_INODE}" \
  bash "${REPO}/adapter/deploy/deploy_ccv3_adapter_release.sh"

sudo env SOURCE_ROOT="${REPO}" RELEASE_ID="${RELEASE_ID}" \
  EXPECTED_SOURCE_COMMIT="${COMMIT}" \
  EXPECTED_WECHAT_PID="${EXPECTED_WECHAT_PID}" \
  EXPECTED_DB_STATE_SHA256="${EXPECTED_DB_STATE_SHA256}" \
  EXPECTED_SEND_STATE_SHA256="${EXPECTED_SEND_STATE_SHA256}" \
  EXPECTED_BOT_DB_SHA256="${EXPECTED_BOT_DB_SHA256}" \
  EXPECTED_DB_STATE_INODE="${EXPECTED_DB_STATE_INODE}" \
  EXPECTED_SEND_STATE_INODE="${EXPECTED_SEND_STATE_INODE}" \
  EXPECTED_BOT_DB_INODE="${EXPECTED_BOT_DB_INODE}" \
  bash "${REPO}/chat-api/deploy/deploy_bridge_release.sh"

sudo env SOURCE_ROOT="${REPO}" RELEASE_ID="${RELEASE_ID}" \
  EXPECTED_SOURCE_COMMIT="${COMMIT}" \
  EXPECTED_WECHAT_PID="${EXPECTED_WECHAT_PID}" \
  EXPECTED_DB_STATE_SHA256="${EXPECTED_DB_STATE_SHA256}" \
  EXPECTED_SEND_STATE_SHA256="${EXPECTED_SEND_STATE_SHA256}" \
  EXPECTED_BOT_DB_SHA256="${EXPECTED_BOT_DB_SHA256}" \
  EXPECTED_DB_STATE_INODE="${EXPECTED_DB_STATE_INODE}" \
  EXPECTED_SEND_STATE_INODE="${EXPECTED_SEND_STATE_INODE}" \
  EXPECTED_BOT_DB_INODE="${EXPECTED_BOT_DB_INODE}" \
  bash "${REPO}/chat-api/deploy/deploy_chat_api_release.sh"
~~~

在两次切换后运行 adapter/scripts/smoke_cloud.py --read-only。该检查只读取健康状态、鉴权和工具目录，不会调用模型或向群发送任何内容。

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

人格回滚保留 Hermes Adapter 和 Adapter SQLite，只切换回已安装的上一版 Adapter 发布。它会把 `HERMES_WECHAT_SESSION_GENERATION` 设为 `13`；生产仍只使用群级上下文，不会读取旧成员关系数据，也不会删除数据库、重启微信、清理游标或发送状态：

```bash
sudo EXPECTED_WECHAT_PID=PID \
  bash adapter/deploy/rollback_persona.sh PREVIOUS_RELEASE_ID
```

`PREVIOUS_RELEASE_ID` 必须是 `/opt/wechat-hermes-adapter-releases/` 下已有的、非当前的版本目录。脚本只重启 Adapter，不重启 Hermes Worker 或微信；它会等待本地 `/health` 返回 `ready=true`，若新版本未就绪则恢复先前的 Adapter 链接与环境文件并重新启动先前版本。

需要退回旧 AI 服务时，切换前将旧 Adapter/Hermes 环境保存到 root 私有备份目录。旧服务回滚脚本要求该目录包含 `adapter.env` 和 `hermes.env`：

```bash
sudo EXPECTED_WECHAT_PID=PID \
  bash adapter/deploy/rollback_to_legacy.sh /root/private-backup/RELEASE_ID
```

旧服务回滚停止新 Adapter/Hermes，恢复旧环境并启动旧 AI 服务。两种回滚都保留新 SQLite、Artifact、栅栏和发送状态用于对账；不要清理数据库游标或 `uncertain` 媒体。

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
