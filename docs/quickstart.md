# 快速开始

这个流程验证 Adapter 的生产状态机，不连接真实微信。模拟栈包含假的 Hermes、假的 Chat API、临时 SQLite、临时 Artifact 目录和真实运行的 Adapter 进程。

## 环境

- Python 3.10 或 3.11
- Git
- 可用的本机回环端口

## 获取和安装

```bash
git clone https://github.com/hepderen/wechat-hermes-agent.git
cd wechat-hermes-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r adapter/requirements-dev.txt
```

Windows PowerShell 使用：

```powershell
git clone https://github.com/hepderen/wechat-hermes-agent.git
Set-Location wechat-hermes-agent
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r adapter\requirements-dev.txt
```

## 运行模拟全链路

Linux/macOS：

```bash
.venv/bin/python adapter/scripts/live_fake_stack.py
```

Windows：

```powershell
.\.venv\Scripts\python adapter\scripts\live_fake_stack.py
```

脚本自动分配端口，并依次验证：

1. 三条相同文本、不同 local ID 的结构化 `@` 均被处理。
2. 带工具成功证据的执行任务成功。
3. 声称完成但没有工具证据的任务失败。
4. 停止栅栏在 Hermes Run stop 之前提交。
5. 被停止任务的旧 Outbox 被抑制。
6. 媒体发送返回 `409 uncertain` 后，Adapter 重启不会重发媒体或总结。
7. `/metrics` 包含 Outbox 指标。

输出示例：

```json
{
  "status": "ok",
  "checks": {
    "three_structured_mentions": 3,
    "execution_with_evidence": "succeeded",
    "execution_without_evidence": "failed",
    "stop_barrier_before_run_stop": true,
    "media_calls_after_restart": 1,
    "summary_calls_after_restart": 1,
    "metrics": true
  }
}
```

临时目录在进程退出时清理，不会写入仓库。

## 运行测试

一次安装所有 CI 依赖：

```bash
python -m pip install -r requirements-ci.txt
```

分别运行，便于定位失败组件：

```bash
cd adapter
python -m pytest -q

cd ../chat-api
python -m pytest -q

cd ../web-research
python -m pytest -q
```

Chat API 测试会模拟数据库、窗口、发送确认和停止竞态，不操作桌面微信。Web Research 测试使用 `httpx` mock，不访问真实搜索站点。

## 开发 Adapter

Adapter 启动时会校验四个不同令牌、回环 URL、绝对数据库路径和 Artifact 路径。直接启动前可从 `adapter/deploy/adapter.env.example` 创建本地环境，并提供假的 Hermes/Chat API。

```bash
cd adapter
set -a
. deploy/adapter.env.local
set +a
python -m uvicorn app.main:app --host 127.0.0.1 --port 18000
```

不要把 `adapter.env.local` 加入 Git；根目录 `.gitignore` 已覆盖本地环境文件。

## 常见问题

### Adapter 启动时报令牌重复

`BRIDGE_TOKEN`、`HERMES_WECHAT_INTERNAL_TOKEN`、`WECHAT_CHAT_API_TOKEN` 和 `HERMES_API_KEY` 必须是四个不同值。

### `/health` 显示 cleanup pending

新实例在配置的宽限时间内允许清理状态为 pending。生产环境应启用 `wechat-hermes-cleanup.timer`，并让它写入 `HERMES_WECHAT_CLEANUP_STATUS_PATH`。

### Windows 出现符号链接测试 skipped

普通 Windows 用户通常没有创建符号链接权限，相关安全测试会跳过；Linux CI 会执行这些用例。

### 搜索插件测试通过但尚未执行真实搜索

单元测试只验证解析、回退、缓存、熔断和 URL 安全。真实网络验收需要按 [生产部署](production-deployment.md) 构建候选包后运行只读 probe。
