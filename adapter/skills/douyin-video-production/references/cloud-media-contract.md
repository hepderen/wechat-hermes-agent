# Cloud Media Contract

Use the cloud Linux terminal for production and the registered
`wechat-production-tools` MCP server for final artifact registration.

## Task directory

The trusted asynchronous task envelope provides a task ID such as `T-12AB34CD`.
All files for that task must stay under:

```text
/var/lib/wechat-hermes/artifacts/T-12AB34CD
```

Create subdirectories when helpful, but resolve paths before use and reject any
path outside the matching task directory. Do not read unrelated user or service
files as media inputs.

## Required tools

- `ffmpeg` renders, transcodes, mixes, and extracts quality-check frames.
- `ffprobe` reports machine-readable stream and format metadata.
- `wechat_register_artifact(task_id, artifact_path)` validates and registers the
  final PNG, JPEG, or MP4.

If a required binary is unavailable, report failure with the missing binary. Do not
fall back to a desktop computer or remote execution host.

## Render rules

1. Inspect every source with `ffprobe` before using it.
2. Use explicit dimensions, frame rate, codecs, pixel format, and duration.
3. For the default vertical delivery, target:

```text
1080x1920, 30 fps, H.264, yuv420p, AAC, MP4
```

4. Write to a temporary output in the task directory.
5. Treat any nonzero exit status as failure.
6. Probe the completed output before renaming or registering it.
7. Keep final output within the adapter's configured size limit.

Example probe:

```bash
ffprobe -v error -show_streams -show_format -of json /absolute/task/path/video.mp4
```

Example final encoding options:

```text
-c:v libx264 -pix_fmt yuv420p -r 30 -c:a aac -movflags +faststart
```

The full filter graph depends on the actual brief and source media. Do not paste
untrusted user text directly into a shell command; pass it through files or safely
escaped tool arguments.

## Registration and delivery

After technical and visual checks pass, call:

```text
wechat_register_artifact("T-12AB34CD", "/var/lib/wechat-hermes/artifacts/T-12AB34CD/final.mp4")
```

The MCP and adapter both validate task scope, path containment, real MIME signature,
size, and registered path. Return the verified Artifact metadata as evidence, never
a legacy delivery marker or bare path. Final delivery belongs only to the adapter
Outbox.
