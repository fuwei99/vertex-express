# Vertex API 改代码小结

日期：2026-05-10

## 背景

这次重写的目标是把 `other-code/vertex-api` 做成一个更接近 `src` 思路的本地 Vertex/OpenAI 兼容服务：

- 以 `other-code/vertex2api/refecode/vertex2openai` 为基础重新整理。
- 主请求链路尽量绕开 `google-genai` SDK 的网络层。
- 使用项目自己的代理节点池，而不是依赖外部 Clash。
- 加上管理面板、配置文件、订阅节点、429 后重试和切换节点能力。

## 主要改动

### 1. 新建独立仓库与基础工程

在 `other-code/vertex-api` 下初始化了独立 Git 仓库，并建立了可本地启动的工程结构。

相关内容：

- `start.bat`
- `requirements.txt`
- `.gitignore`
- `config.example.json`
- `app/`
- `static/admin.html`

### 2. 从 `.env` 改为优先使用 `config.json`

原来的 `.env` 配置比较难维护，所以新增了 `config.json` 支持。

当前逻辑：

- 优先读取 `config.json`
- 如果没有配置项，再回退到环境变量或 `.env`
- 管理面板保存设置时，会写入 `config.json`
- `config.json` 被 `.gitignore` 忽略，避免提交 API key、订阅链接等敏感信息

相关文件：

- `app/config_store.py`
- `app/config.py`
- `app/main.py`
- `config.example.json`

### 3. 替换 Gemini 主请求链路为 REST + curl_cffi

之前的错误栈里可以看到主请求走的是：

```text
google.genai -> aiohttp -> proxy -> WinError 64
```

所以后来把 Gemini 生成内容的主链路换成了自己拼 Vertex REST 请求，并用 `curl_cffi` 发送。

当前主请求不再走：

```python
current_client.aio.models.generate_content_stream(...)
```

而是走：

```python
stream_generate_content(...)
generate_content(...)
```

相关文件：

- `app/gemini_rest_client.py`
- `app/api_helpers.py`
- `app/routes/chat_api.py`

### 4. SOCKS5 改为 curl 侧远程 DNS

本地 Windows + Clash/fake-ip 场景下，普通 `socks5://` 容易把域名解析成 `198.18.x.x` 这类 fake-ip，导致请求失败。

所以在 `curl_cffi` 代理配置里把：

```text
socks5://127.0.0.1:10808
```

内部转换为：

```text
socks5h://127.0.0.1:10808
```

这样 DNS 解析交给代理节点远端处理。

相关文件：

- `app/gemini_rest_client.py`

### 5. 加入订阅节点池和节点切换

项目启动时会读取订阅链接，拉取节点，筛选可用节点，然后启动本地 worker/sing-box 中转。

当前链路是：

```text
vertex-api -> 127.0.0.1:10808 -> worker/sing-box -> Google
```

相关文件：

- `app/node_manager.py`
- `app/transport/`
- `app/routes/admin_ui.py`

### 6. 区分总重试次数和切换节点阈值

原来 `max retries` 的语义容易混乱，所以拆成两个概念：

- `max_retries_429`：一次请求最多重试多少次。
- `retries_before_switch`：当前节点连续失败几次后切换到下一个节点。

管理面板里也对应改成两个字段：

- `Total retry limit`
- `Failures before node switch`

相关文件：

- `app/api_helpers.py`
- `static/admin.html`
- `config.example.json`

### 7. 修复图片返回格式

Gemini REST 返回的图片 `inlineData.data` 是 base64 字符串，而旧逻辑按 bytes 处理，导致：

```text
Error converting image to markdown: a bytes-like object is required, not 'str'
```

后来改成同时兼容：

- bytes
- base64 string
- data URL string

相关文件：

- `app/message_processing.py`

### 8. 修复管理面板乱码

曾经因为 PowerShell 写文件编码问题，把 `static/admin.html` 写坏过，出现页面乱码和按钮文案错乱。

之后恢复为正常 UTF-8，并提交了修复。

相关文件：

- `static/admin.html`

### 9. 订阅抓取绕开环境代理

管理面板提交订阅时，之前会被系统环境代理或本地代理影响，导致 `/api/admin/subscribe` 可能 400。

后来改成订阅抓取显式绕开环境代理：

```python
trust_env=False
proxy=None
```

相关文件：

- `app/routes/admin_ui.py`

### 10. 增强 Gemini REST 错误日志

之前只显示 `HTTPError`，看不出到底是不是 429。

后来改成把 Gemini REST 的 HTTP 状态码和响应体打印出来，例如：

```text
HTTP 429 from Gemini REST: RESOURCE_EXHAUSTED
```

这样可以明确区分：

- 429：Vertex/API key/project/model 配额问题
- curl 97：代理节点无法建立 SOCKS5 到目标站点的连接
- TLS/WinError：本地代理链路或网络层问题

相关文件：

- `app/gemini_rest_client.py`
- `app/api_helpers.py`

## 当前关键结论

### 1. 关掉 Clash 后请求恢复，说明本地代理链路打架是核心原因之一

之前看到的：

```text
curl: (97) cannot complete SOCKS5 connection to aiplatform.googleapis.com
```

不是所有节点都坏了，也不是 REST 方案一定错了。

实际原因是 Clash 开着时，可能和项目自己的 worker/sing-box 抢占或干扰了本地 `127.0.0.1:10808` 代理链路。

现在建议本地运行 `vertex-api` 时：

- 关闭 Clash
- 或确保 Clash 不占用、不接管、不改写 `127.0.0.1:10808`

### 2. 429 不是节点必然能解决的问题

日志里的：

```text
HTTP 429 from Gemini REST: RESOURCE_EXHAUSTED
```

更像是 Vertex Express 的 API key / project / model 配额限制。

切换节点只能改变出口网络，不能保证解决 project 级别或 key 级别的 429。

后续如果要继续优化，应该考虑：

- 429 优先轮换 Express API key/project
- 代理连接错误再切换节点
- 把 429 和网络错误分成两套策略

### 3. `src` 和当前 `vertex-api` 不是同一条上游链路

`src` 不是直接打 Vertex Express REST，它走的是 Cloud Console GraphQL/recaptcha/session 那条链路。

所以它看起来更丝滑，是因为它的：

- 上游入口不同
- 节点池逻辑更完整
- transport codec 更丰富
- 错误分类和切换策略更成熟

当前 `vertex-api` 是 Vertex REST 兼容服务，已经尽量借鉴了它的节点池思路，但不是完全一样的协议路径。

## 最近提交记录

```text
bb82527 Log Gemini REST HTTP errors
7d1ec10 Bypass env proxy for subscription fetch
b091b87 Fix admin panel encoding
fd161d0 Separate retry and node switch limits
581ce96 Handle REST image data strings
557d021 Add config json support
1f7a873 Use remote DNS for SOCKS proxy
e2f5466 Use curl REST for Gemini calls
0307cf5 Initial vertex-api baseline
```

## 后续如果继续做，建议方向

1. 把 429 重试从“切节点优先”改成“切 API key/project 优先”。
2. 对每个新激活节点做一次 `aiplatform.googleapis.com` 连通性健康检查。
3. 继续对齐 `src/transport/codec.py`，提高订阅节点格式兼容度。
4. 管理面板增加当前 key/project、当前节点、最近错误的可视化状态。
5. 把 SDK 相关残留进一步清理，只保留类型转换或彻底替换为纯 REST 数据结构。

## 当前运行建议

本地启动时建议：

```powershell
cd C:\Users\zhishang\Desktop\vertex-v1.0.4\other-code\vertex-api
.\start.bat
```

并保持 Clash 关闭，避免本地代理链路互相干扰。
