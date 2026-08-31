# 生产部署

## 适用范围

本文针对一台已运行 Linux 微信的云服务器。当前发布仅提供“小格”群聊：固定孙笑川人格、结构化消息识别、常态监听、同步文本回复、重复控制和停止栅栏。

服务拓扑：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| `wechat-chat-api.service` | `127.0.0.1:8765` | 结构化读库、微信 UI 发送与确认 |
| `linux-wechat-bridge.service` | 无监听端口 | 游标消费与 Adapter 请求 |
| `wechat-hermes-adapter.service` | `127.0.0.1:8000` | 入站账本、上下文与聊天路由 |
| `hermes-worker.service` | `127.0.0.1:8642` | Hermes Session Chat |

所有端口保留在服务器回环地址。

## 发布前基线

在候选发布前记录以下数据：

```bash
EXPECTED_WECHAT_PID=$(pgrep -x wechat)
EXPECTED_DB_STATE_SHA256=$(sudo sha256sum /home/ubuntu/linux-wechat-bot/db-state.json | awk '{print $1}')
EXPECTED_SEND_STATE_SHA256=$(sudo sha256sum /home/ubuntu/.cache/wechat-chat-api/send-state.json | awk '{print $1}')
EXPECTED_BOT_DB_SHA256=$(sudo sha256sum /opt/wechat-ai-bot/data/bot.db | awk '{print $1}')
EXPECTED_DB_STATE_INODE=$(sudo stat -c '%d:%i' /home/ubuntu/linux-wechat-bot/db-state.json)
EXPECTED_SEND_STATE_INODE=$(sudo stat -c '%d:%i' /home/ubuntu/.cache/wechat-chat-api/send-state.json)
EXPECTED_BOT_DB_INODE=$(sudo stat -c '%d:%i' /opt/wechat-ai-bot/data/bot.db)
```

发布流程不重启微信，不删除或改写 `db-state*`、`send-state*`、`bot.db`。预期微信 PID 必须与发布前一致。

## 配置

从仓库中的示例文件创建服务器私有配置：

```text
/etc/wechat-hermes/adapter.env
/etc/wechat-hermes/hermes.env
/etc/wechat-hermes/chat-api.env
/etc/wechat-hermes/bridge.env
```

权限使用 `root:root 0600`。以下令牌必须是互不相同的随机值：

```bash
openssl rand -base64 48
openssl rand -base64 48
openssl rand -base64 48
openssl rand -base64 48
```

| 变量 | 使用方 |
| --- | --- |
| `BRIDGE_TOKEN` | Bridge、Adapter |
| `HERMES_WECHAT_INTERNAL_TOKEN` | Adapter 内部接口 |
| `WECHAT_CHAT_API_TOKEN` | Chat API、Bridge、Adapter |
| `HERMES_API_KEY` | Adapter、Hermes |

Adapter 环境至少包含：

```text
ALLOWED_WECHAT_ROOM_IDS=ROOM_ID
WECHAT_BOT_WXID=BOT_WXID
HERMES_WECHAT_CHAT_ONLY=true
HERMES_WECHAT_GROUP_LISTENER_ENABLED=true
HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS=12
HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES=3
HERMES_WECHAT_GROUP_LISTENER_NAMES=小格,Hermes
HERMES_WECHAT_SESSION_GENERATION=14
```

`ALLOWED_WECHAT_ROOM_IDS` 只填入明确允许常态聊天的群。`bot_wxid`、群 ID、令牌和模型供应商凭据只存放在服务器私有配置中。

## 候选验证

先在候选源码中运行本地测试。它们使用临时数据库和假发送端：

```bash
python3.11 -m venv /tmp/wechat-hermes-test
/tmp/wechat-hermes-test/bin/python -m pip install -r requirements-ci.txt

(cd adapter && /tmp/wechat-hermes-test/bin/python -m pytest -q)
(cd chat-api && /tmp/wechat-hermes-test/bin/python -m pytest -q)
/tmp/wechat-hermes-test/bin/python adapter/scripts/live_fake_stack.py
```

验收重点：

1. 固定人格来源、许可证、章节和 SHA-256 一致。
2. 三条相同文本但不同真实消息 ID 的 `@` 依次处理。
3. 自发消息和机器人身份记录被 Bridge 过滤。
4. 最近群聊转录最多 16 条，当前昵称只来自可信元数据；每轮 Hermes Session 均在前后清理。
5. 低信息到场确认、重复段落和跨轮相似回复被抑制。
6. 停止栅栏提交后旧发送数量为零。
7. Adapter、Chat API 仅绑定回环地址，微信 PID 与受保护文件基线一致。

生产候选验证不向真实微信群发送自动测试文字或媒体。

## 安装

首次迁移使用：

```bash
sudo --preserve-env bash adapter/deploy/install_cloud.sh
```

已部署实例使用版本化发布脚本。脚本文件名 `deploy_ccv3_adapter_release.sh` 为历史兼容；当前脚本只校验并发布孙笑川单人格资源。

```bash
sudo env \
  SOURCE_ROOT=/path/to/repository/adapter \
  RELEASE_ID=sunxiaochuan-RELEASE_ID \
  EXPECTED_SOURCE_COMMIT=FULL_40_CHARACTER_COMMIT \
  EXPECTED_WECHAT_PID="$EXPECTED_WECHAT_PID" \
  EXPECTED_DB_STATE_SHA256="$EXPECTED_DB_STATE_SHA256" \
  EXPECTED_SEND_STATE_SHA256="$EXPECTED_SEND_STATE_SHA256" \
  EXPECTED_BOT_DB_SHA256="$EXPECTED_BOT_DB_SHA256" \
  EXPECTED_DB_STATE_INODE="$EXPECTED_DB_STATE_INODE" \
  EXPECTED_SEND_STATE_INODE="$EXPECTED_SEND_STATE_INODE" \
  EXPECTED_BOT_DB_INODE="$EXPECTED_BOT_DB_INODE" \
  bash adapter/deploy/deploy_ccv3_adapter_release.sh
```

发布时 Adapter 会：

1. 校验微信 PID、状态文件和候选源码。
2. 校验固定人格的来源锁、许可证、章节边界与哈希。
3. 安装版本化 Adapter 目录与独立虚拟环境。
4. 写入 `HERMES_WECHAT_SESSION_GENERATION=14`。
5. 隔离旧 SQLite 中未完成的遗留记录。
6. 仅重启 Adapter 与其依赖的必要 Hermes 组件。

Chat API 和 Bridge 的更新使用各自的 `chat-api/deploy` 脚本，并传入同一组基线变量。

## 就绪检查

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/metrics
curl -fsS -H "Authorization: Bearer TOKEN" http://127.0.0.1:8765/health
systemctl --no-pager --full status \
  wechat-chat-api linux-wechat-bridge hermes-worker wechat-hermes-adapter
```

预期 `/health` 返回：

```json
{
  "live": true,
  "ready": true,
  "degraded": false,
  "chat_only": true
}
```

首次真实群聊消息可以作为受控烟雾测试。发布期间观察日志中的 `request_id`、`room_id`、`sender_id`、`source_local_id` 与状态，不记录消息正文。

## 回滚

人格回滚使用：

```bash
sudo EXPECTED_WECHAT_PID=PID \
  bash adapter/deploy/rollback_persona.sh PREVIOUS_RELEASE_ID
```

脚本恢复前一版 Adapter 发布，并将会话代次提升到 `15`，使旧会话与回滚会话分离。SQLite、游标、发送状态、固定来源归档和受保护微信文件都会保留。

旧 AI 服务回滚需使用部署前保存的 root 私有环境备份，并按既有运维流程恢复 `:8000` 监听。整个过程中保持微信进程持续运行。

## 密钥轮换

模型密钥通过 `adapter/scripts/rotate_hermes_model.py` 处理。密钥文件放在仓库外的 root 私有目录，权限为 `0600`。轮换后重启 Hermes Worker 和 Adapter，再次执行就绪检查与 PID/文件基线核对。
