# TV Web Server - 远程音乐控制 API

服务器默认运行在 **8788** 端口（可用 `python3 server.py --port <端口>` 覆盖）。

## 端点

```
POST /api/remote/music
Content-Type: application/json
```

## 通过 frpc 隧道访问

启动 frpc 后，外部可通过云服务器访问：

```
POST http://124.222.43.107:39988/api/remote/music
```

---

## 播放音乐（含成交祝贺展示）

使用 config.json 中已配置的音乐链接，并展示成交祝贺页面：

```bash
curl -X POST http://124.222.43.107:39988/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{
    "action": "play",
    "salesperson": "张三",
    "amount": "128000",
    "deal_time": "2026-06-30 14:30:00"
  }'
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | string | ✅ | 固定为 `"play"` |
| music_url | string | ❌ | 音乐链接，不传则使用 config 中已配置的 |
| salesperson | string | ❌ | 成交销售员姓名，展示在祝贺页面 |
| amount | string | ❌ | 销售额（纯数字），前端自动格式化为 ¥128,000 |
| deal_time | string | ❌ | 成交时间，如 `"2026-06-30 14:30:00"` |

> 当传入 `salesperson` 或 `amount` 时，客户端会展示全屏祝贺页面（金色纸屑 + 烟花动效）。
> 不传这些参数时，行为与之前一致（跳转到第一个网页）。

指定音乐链接播放（会覆盖 config 中的 music_url）：

```bash
curl -X POST http://124.222.43.107:39988/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{
    "action": "play",
    "music_url": "https://example.com/music.mp3",
    "salesperson": "李四",
    "amount": "256000",
    "deal_time": "2026-06-30 15:00:00"
  }'
```

### 成功响应 (200)

```json
{
  "status": "ok",
  "action": "play",
  "message": "音乐播放指令已发送",
  "music_url": "https://image.toomotoo.online/public/congratulations.mp3",
  "cache_url": "/cache/music_abc123.mp3",
  "celebration": {
    "salesperson": "张三",
    "amount": "128000",
    "deal_time": "2026-06-30 14:30:00"
  }
}
```

### 失败响应 - 未配置链接 (400)

```json
{
  "status": "error",
  "action": "play",
  "message": "未配置音乐链接，请先在config或请求中提供music_url"
}
```

### 失败响应 - 下载失败 (500)

```json
{
  "status": "error",
  "action": "play",
  "message": "音乐下载失败: HTTP Error 404: Not Found"
}
```

---

## 停止音乐

停止音乐播放并关闭祝贺页面，恢复正常网页轮播：

```bash
curl -X POST http://124.222.43.107:39988/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{"action": "stop"}'
```

### 成功响应 (200)

```json
{
  "status": "ok",
  "action": "stop",
  "message": "音乐已停止"
}
```

---

## 错误情况

### 无效的 action (400)

```bash
curl -X POST http://124.222.43.107:39988/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{"action": "pause"}'
```

```json
{
  "status": "error",
  "message": "无效的action: 'pause'，支持: play, stop"
}
```

### 无效的 JSON (400)

```bash
curl -X POST http://124.222.43.107:39988/api/remote/music \
  -H "Content-Type: application/json" \
  -d 'not json'
```

```json
{
  "status": "error",
  "message": "请求体必须是有效的JSON"
}
```

---

## POST 原文 (Raw HTTP)

### 播放（含祝贺数据）

```http
POST /api/remote/music HTTP/1.1
Host: 124.222.43.107:39988
Content-Type: application/json

{"action": "play", "salesperson": "张三", "amount": "128000", "deal_time": "2026-06-30 14:30:00"}
```

### 停止

```http
POST /api/remote/music HTTP/1.1
Host: 124.222.43.107:39988
Content-Type: application/json
Content-Length: 18

{"action": "stop"}
```

### 指定链接播放

```http
POST /api/remote/music HTTP/1.1
Host: 124.222.43.107:39988
Content-Type: application/json

{"action": "play", "music_url": "https://example.com/music.mp3", "salesperson": "王五", "amount": "88000", "deal_time": "2026-06-30 16:00:00"}
```

---

## 本地测试（不经过 frpc）

```bash
# 播放（含祝贺展示）
curl -X POST http://localhost:8788/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{
    "action": "play",
    "salesperson": "测试员",
    "amount": "99999",
    "deal_time": "2026-06-30 12:00:00"
  }'

# 停止
curl -X POST http://localhost:8788/api/remote/music \
  -H "Content-Type: application/json" \
  -d '{"action": "stop"}'
```

---

## frpc 隧道管理（管理面板 /admin 也提供同名按钮）

### 启动隧道

启动本地 frpc 子进程；启动前会自动把 frpc.toml 的 localPort 同步为当前服务器端口：

```bash
curl -X POST http://localhost:8788/api/frpc_control \
  -H "Content-Type: application/json" \
  -d '{"action": "start"}'
```

成功响应 (200)：

```json
{"status": "ok", "action": "start", "message": "frpc 已启动"}
```

已在运行时返回 (400)：`{"status": "error", "action": "start", "message": "frpc 已在运行中"}`

### 停止隧道

```bash
curl -X POST http://localhost:8788/api/frpc_control \
  -H "Content-Type: application/json" \
  -d '{"action": "stop"}'
```

### 查询隧道状态

```bash
curl http://localhost:8788/api/frpc_status
```

```json
{"running": false, "tunnel_url": "http://124.222.43.107:39988"}
```

> 服务器进程退出（Ctrl+C / SIGTERM）时会自动结束由它启动的 frpc 隧道。
