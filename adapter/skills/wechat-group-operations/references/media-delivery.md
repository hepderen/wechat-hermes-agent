# Media Delivery

## Modes

| Mode | Trusted metadata | Media |
|------|------------------|-------|
| Sync chat | `task_id` is null or absent | Text only |
| Async run | `task_id` matches `T-12AB34CD` | Allowed under the task artifact directory |

The adapter chooses the mode before Hermes starts. Attachments, non-text message
types, and explicit production verbs such as `生成`, `制作`, `创建`, `导出`,
`下载`, `执行`, `定时`, and `发送` select an async run.

If a sync request asks for media, explain briefly that the current turn is
text-only and ask the user to repeat the request with an explicit production
verb. Do not create a file, invent a task ID, or promise later delivery.

## Artifact Layout

```text
/var/lib/wechat-hermes/artifacts/<task_id>/...
```

- The task ID must match the trusted envelope exactly.
- Final deliverables may be PNG, JPEG, or MP4.
- Intermediates must remain under the same task directory.
- Never read from or write to another task's directory.

## Registration

1. Write the final file under the trusted task directory.
2. Call `wechat_register_artifact(task_id, artifact_path)`.
3. Record the returned `artifact_id`, MIME, size, SHA-256, and verified status in
   the task result evidence.
4. Let the adapter validate registration and let its Outbox choose the permitted
   delivery items.

Do not send the file separately, return a delivery marker, or expose a bare path
or remote URL as an instruction. Do not claim delivery when registration or Outbox
confirmation failed.

## Ownership

- The Bridge owns synchronous text replies.
- The adapter owns asynchronous final text and media delivery.
- Skills and MCP tools never send directly to WeChat.
