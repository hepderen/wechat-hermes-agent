# 快速开始

本地验证使用假 Hermes、假 Chat API、临时 SQLite 和临时目录，不连接真实微信。

## 环境

- Python 3.10 或 3.11
- Git
- 可用的本机回环端口

## 安装

```bash
git clone https://github.com/OWNER/wechat-hermes-agent.git
cd wechat-hermes-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-ci.txt
```

Windows PowerShell：

```powershell
git clone https://github.com/OWNER/wechat-hermes-agent.git
Set-Location wechat-hermes-agent
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements-ci.txt
```

## 运行测试

```bash
cd adapter
../.venv/bin/python -m pytest -q

cd ../chat-api
../.venv/bin/python -m pytest -q

cd ..
.venv/bin/python adapter/scripts/live_fake_stack.py
```

Windows PowerShell：

```powershell
Set-Location adapter
..\.venv\Scripts\python -m pytest -q

Set-Location ..\chat-api
..\.venv\Scripts\python -m pytest -q

Set-Location ..
.\.venv\Scripts\python adapter\scripts\live_fake_stack.py
```

重点检查：

1. 固定孙笑川章节、来源锁和许可证通过完整性校验。
2. 结构化 `@`、回复关系和可信昵称传入 Adapter。
3. 三条相同正文、不同真实消息 ID 的消息分别处理。
4. 自发消息、机器人身份和伪装入站记录不进入聊天上下文。
5. 群上下文维持 24 小时和 120 条存储上限，模型提示只取最近 16 条。
6. 每轮前后清理 Hermes Session，服务端历史不会越过 Adapter 的上下文上限。
7. 到场确认、重复段落和跨轮相似回复得到清理。
8. 内部执行入口返回已停用状态，普通聊天请求始终以 `disable_tools=true` 发送。

## 本地 Adapter

从 `adapter/deploy/adapter.env.example` 创建本地私有环境文件，填入独立测试令牌和回环地址：

```bash
cd adapter
set -a
. deploy/adapter.env.local
set +a
python -m uvicorn app.main:app --host 127.0.0.1 --port 18000
```

`adapter.env.local`、数据库、日志和密钥文件保持在 Git 工作树外。

## 生产

生产服务继续运行在 Linux 云服务器。完整配置、基线核对、发布、就绪检查与回滚步骤见 [生产部署](production-deployment.md)。
