---
name: phone-remote
description: 手机局域网远程指令桥：在电脑启动本地网页服务，手机浏览器输入文字并入队，MiMo Desktop 将其当作通用指令执行，结果写回同一看板，手机与电脑打开同一地址即可看到相同内容。Use when the user says "手机远程", "手机发指令", "从手机控制电脑MiMo", "启动手机远程", "处理手机指令", "处理手机消息", "phone remote", or asks to input text on the phone and have MiMo Desktop run it and return the result. Do NOT use for Telegram/WeChat bots, cloud-sync folder bridges, or non-MiMo remote desktop control.
---

# Phone Remote（手机 → MiMo Desktop 同屏远程指令）

手机网页发任意文字 → 电脑 MiMo Desktop 执行 → 结果写回**同一看板**（手机与电脑显示相同内容）。

数据目录：`%USERPROFILE%\.local\share\mimocode\phone-remote\`（下称 `<home>`）  
桥接脚本：`scripts/bridge.py`（相对本技能目录）

## Important

1. **通用指令执行**：手机原文就是用户指令，按当前会话正常执行（问答、写代码、改文件、查资料等），不要只做固定脚本。
2. **同屏**：回复必须通过 `bridge.py complete` 写入 board；手机与电脑打开同一 URL 看到的内容必须一致。
3. **自动执行（默认开启）**：服务进程在 `POST /api/send` 后会后台调用 `mimo run --dangerously-skip-permissions` 消费 inbox，并把结果写回看板。手机发送后**无需**在电脑再说「处理手机指令」。
4. **独立会话 + 固定模型**：自动执行用 headless `mimo run`，跑在**手机自己的会话**里（id 存在 `phone_session_id.txt`，首次运行时自动创建）。不要让它复用你电脑端正在打开的那个会话：共用会话会让 headless 运行静默返回空输出。
   模型必须显式固定（`--model`，默认 `xiaomi/mimo-v2.6-pro`，用环境变量 `PHONE_REMOTE_MODEL` 覆盖，改完需重启服务；`bridge.py status` 或 `/api/status` 会打印当前 `model=`）。原因是会话若被 Desktop 用过会残留 `mimo-desktop/*` 这类**只有 Desktop 才注入的 provider**，独立 CLI 解析不到，于是模型一个字也不产出、进程 `exit=0`。
   v2.6 系列在 CLI 0.1.9 的内置目录里还查不到，已在**全局配置** `~/.config/mimocode/mimocode.jsonc` 的 `provider.xiaomi.models` 下登记（复用内置 `xiaomi` provider 的凭据与端点，只写了 `name`，未编造上下文/成本等元数据）。用 `mimo models xiaomi` 本地校验。
   headless 实测可用性：`xiaomi/mimo-v2.6-pro` ✅、`xiaomi/mimo-v2.6-pro-ultraspeed` ✅（均约 43s，端到端 ~150s，偏慢）、`xiaomi/mimo-v2.6-flash` ✅（约 20s，最快）、`xiaomi/mimo-v2.5` ✅、`deepseek/deepseek-v4-flash` ✅；`mimo/mimo-auto` ❌ 免费 API 已停服需登录；`xiaomi/mimo-v2.6` / `mimo-v2.5-pro-ultraspeed` ❌ 服务端 unsupported。模型错误桥接会解析 `type=error` 事件并原样显示在看板上。
5. **手动兜底**：若自动关闭或失败，仍可用「处理手机指令」或 `bridge.py process`。
6. **局域网**：默认绑定 `0.0.0.0:8765`。**必须开鉴权**——共享 Wi-Fi 等同公网边界。
7. **鉴权（安全加固）**：除 `127.0.0.1` 回环外，所有请求都要令牌。**手机端不使用固定令牌**，模型是两级：
   - **配对一次**（6 位码）→ 下发 `device_id` + `device_secret`（设备凭据，只存手机本地，**不直接当 API 令牌用**）。配对码 10 分钟有效、一码一用：电脑上 `bridge.py pair` 或本机 `/api/status` 的 `pair_code`（仅回环可见）。
   - **每次启动随机令牌**：App 启动时 `POST /api/session {device_id, device_secret}` → 兑换一个**全新的 48 位会话令牌**（只存内存，不落盘），同时**该设备上一枚令牌立即作废**。有效期 30 天。
   - 电脑侧另有主令牌 `<home>/auth_token.txt`（`bridge.py token` 可打印），只作 CLI/看板兜底，手机端默认不用。
   携带方式：`Authorization: Bearer <token>` / `X-Auth-Token` / `?token=`；`POST /api/login {token}` 直接校验令牌。错误尝试 180 秒内 10 次该 IP 限流；**正确令牌不受限流影响**。
8. **远程接入（出门可用）**：推荐 **Tailscale**（免费、点对点、不暴露公网）——电脑与手机各装客户端并登录同一账号，手机用 Tailscale 分配的 `100.x.x.x:8765` 访问；桥接本身无需改动（令牌鉴权仍然生效）。次选 Cloudflare Tunnel / frp（会暴露公网 URL，**必须**配合上面的令牌）。不要用端口转发直接暴露 8765。
7. **路径**：调用脚本时用技能目录下 `scripts/bridge.py` 的绝对路径；Python 用 `$env:MIMO_PYTHON`（若未设置则 `python`）。
8. **历史会话：可读，也可继续对话**：看板只承载手机↔电脑的消息流。要进入**任意已有会话**，点看板顶部「电脑端会话历史 →」或打开 `http://<LAN-IP>:<port>/history`：下拉切换会话 → 读取记录 → **也可在下方输入框继续发消息，会真的写进该会话**（`POST /api/chat` 后台执行 `mimo run --session <sid>`，`GET /api/chat/status?task_id=` 轮询进度，完成后自动刷新）。读取走 `mimo export`，本身不改动历史；**发送**才会新增消息。
9. **手机页面直接换模型**：看板底部输入框上方有「模型」下拉框，选项来自 `GET /api/models`（`xiaomi/*` + `deepseek/*` 中 CLI 已登记的模型）。选中即 `POST /api/model` 存到 `<home>/model.txt`，`run_agent_once` **每次运行时**读取——改完立即生效，**无需重启服务**。当前值见 `GET /api/status` 的 `model` 或 `bridge.py status` 的 `model=`；环境变量 `PHONE_REMOTE_MODEL` 只在没有 `model.txt` 时作为兜底默认。

## Workflow

### Step 1 · 启动服务（触发：启动手机远程 / phone remote / 手机发指令）

1. 启动（若已在跑会提示 already_running）：

```powershell
$skill = "$env:USERPROFILE\.config\mimocode\skills\phone-remote"
$py = if ($env:MIMO_PYTHON) { $env:MIMO_PYTHON } else { "python" }
Start-Process -FilePath $py -ArgumentList "`"$skill\scripts\bridge.py`"","start" -WindowStyle Hidden
Start-Sleep -Seconds 1
& $py "$skill\scripts\bridge.py" status
```

**启动方式（重要，服务会反复自己消失的根因在这里）**

- **不要**直接用 `Start-Process` 起 `bridge.py start`：那样它会挂在这个 shell 的进程树上，shell 一被回收服务就跟着死，且 pid 文件残留，造成「`status` 说还在跑、实际端口已不通」。
- 正确起法：用 `<home>` 下的 `launch.bat`（已把 stdout/stderr 重定向到 `<home>/stdout.log`，崩溃时能看到 traceback），并用**脱离进程树**的方式创建：
  ```powershell
  $home = "$env:USERPROFILE\.local\share\mimocode\phone-remote"
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
    -Arguments @{CommandLine = "cmd.exe /c `"$home\launch.bat`""}
  ```
- 同时保证**看门狗**在跑：`<home>/watchdog.py`（锁文件 `<home>/watchdog.pid`，日志 `<home>/watchdog.log`）。每 20 秒探测 `/api/status`，不可达就 `stop` 清残留 + 重新拉起。实测杀掉服务进程约 **10 秒自动恢复**。

2. 从 `status` 取 **LAN URL**（形如 `http://192.168.x.x:8765`），明确告诉用户：
   - 手机与电脑都打开该 URL
   - 手机输入文字 → 发送
   - **自动模式下**：发送后电脑后台立即执行，刷新看板即可看到回复
   - 若自动关闭：在电脑 MiMo 说「处理手机指令」
3. 可选：若用户要求电脑也打开页面，用默认浏览器打开 `http://127.0.0.1:8765`（或 status 里的本机 URL）。不要用外部服务托管。

### Step 2 · 处理手机/网页指令（触发：处理手机指令 / 处理手机消息 / poll phone）

1. 读取队列：

```powershell
& $py "$skill\scripts\bridge.py" pending
```

2. 若 `count == 0`：告知看板 URL、pending=0，说明没有待处理消息；不要编造内容。
3. 若有队列，**按 created_at 顺序逐条**：
   - 将 `text` 视为当前用户指令，在 MiMo Desktop **完整执行**（可用工具、可改文件、可查资料）。
   - 产出可给用户看的最终回复（markdown 纯文本即可；避免超长原始日志）。
   - 写回看板（先写临时文件再 complete，避免转义问题）：

```powershell
$tmp = Join-Path $env:TEMP ("mimo_phone_reply_" + $id + ".md")
# 将回复全文写入 $tmp
& $py "$skill\scripts\bridge.py" complete --id $id --status done --file $tmp
```

   - 失败时：`--status error --file $tmp`，`$tmp` 里写清原因与建议。
4. 处理完后在聊天中简要汇总：处理了几条、每条一句结果、看板 URL。完整内容以看板为准（与手机一致）。

### Step 3 · 查看/排障（触发：手机远程状态 / 看板 / 电脑端历史 / 看电脑上的对话）

```powershell
& $py "$skill\scripts\bridge.py" status
& $py "$skill\scripts\bridge.py" board
```

看/续任意会话（手机与电脑浏览器打开同一地址）：

```powershell
& $py "$skill\scripts\bridge.py" sessions                                   # 列出会话 id + 标题 + 更新时间
& $py "$skill\scripts\bridge.py" history --session ses_xxx --limit 40        # 取某会话最近 40 条
```

页面入口：`http://<LAN-IP>:<port>/history`（即 `status` 输出的 URL 后加 `/history`）。
默认选中**最新一条非 phone-remote 会话**（也就是你电脑端在聊的那个）；下拉可切换任意会话。
读取会调 `mimo export`，大会话需数秒，结果缓存 20 秒，本身不改动历史；**在输入框发送会向该会话新增消息**，用「执行中…/完成」轮询状态，完成后页面自动刷新。

### Step 4 · 关闭（触发：关闭手机远程 / 停止手机远程）

```powershell
& $py "$skill\scripts\bridge.py" stop
```

## Examples

| 用户说 | 动作 | 结果 |
|--------|------|------|
| 启动手机远程 | Step 1 | 启动服务，返回 LAN URL，说明用法 |
| 处理手机指令 | Step 2 | 执行 inbox 中全部 pending，并写回看板 |
| 手机远程状态 | Step 3 | running / port / pending / urls |
| 看某个会话 / 接着某个会话聊 / 看电脑端历史 | Step 3 | 返回 `/history` URL，下拉切会话，输入框可直接继续对话 |
| 换手机侧的模型 | 优先：直接在看板底部「模型」下拉框选，立即生效；或设 `PHONE_REMOTE_MODEL` 后重启 | `bridge.py status` / `/api/status` 的 `model=` 变为新模型 |
| 关闭手机远程 | Step 4 | 停止本地服务 |

**通用执行示例**：手机发送「帮我写一个计算阶乘的 Python 函数并解释」→ Desktop 完整作答 → `complete` 写回 → 手机与电脑看板均显示同一段回复。

## Troubleshooting

| 现象 | 原因 | 处理 |
|------|------|------|
| 手机打不开 URL | 不在同一 Wi-Fi / 防火墙拦 8765 / 服务未启动 | `status` 确认 running；允许 Python 专用网络；换 LAN IP；关 VPN 客户端再试 |
| `already_running` 但 pending 不消费 | 服务在跑，但没人执行 Step 2 | 在 Desktop 说「处理手机指令」 |
| `complete` 报 inbox item not found | id 写错或已被处理 | 重新 `pending`；`board` 核对消息 id |
| 端口被占用 | 8765 已占用 | `PHONE_REMOTE_PORT=8766` 后 start，把新 URL 给用户 |
| 手机发了但电脑看板不同步 | 打开了不同 URL/端口 | 两端都用 `status` 打印的同一 LAN URL |
| 权限/杀软拦截 Start-Process | 隐藏窗口启动被拦 | 在终端前台运行 `bridge.py start` |
| 手机看不到电脑上的历史对话 | 看板只承载手机↔电脑消息流，电脑端会话是另一个 session | 打开 `/history`，下拉切到目标会话 |
| `/history` 说 Session not found | 会话 id 不存在/已删除 | 先跑 `sessions` 拿正确 id |
| 改了 bridge.py 但手机端没变化 | 服务进程仍在跑旧代码 | `bridge.py stop` 再 `start`（重启后生效） |
| 手机发送后回复「mimo run 无文本输出 (exit=0)」 | 空输出：多是复用了 Desktop 正在用的会话，或会话残留 `mimo-desktop/*` 模型 | 删掉 `<home>/phone_session_id.txt` 让手机重建独立会话；确认 `PHONE_REMOTE_MODEL` 是 CLI 可用模型（`bridge.py` 已默认固定） |
| 服务跑一阵后自己消失 / `status` 说在跑但端口不通 | 曾用会被回收的 shell 启动，或进程被杀留下残留 pid | 先 `bridge.py stop` 清残留再启动；**用 `launch.bat` + 脱离进程树创建**，并让 `<home>/watchdog.py` 常驻自愈；查 `<home>/watchdog.log`、`<home>/stdout.log` |
| `/history` 报「读取失败：http 200」 | 接口成功但缺 `ok` 字段（旧版本 bug） | 已修；重启服务即可 |

## Protocol (summary)

- Inbox: `<home>/inbox/<req_id>.json` — `{id, role, source, text, status, created_at}`
- Board: `<home>/board.json` — 双端唯一数据源；`messages[]` 含 user + assistant
- `complete`：把对应 user 消息标 done，并追加 `id=<req_id>__reply` 的 assistant 消息
- History: `GET /history`（读 + 可对话页面）、`GET /api/sessions`、`GET /api/history?sid=&limit=`（读，数据源 `mimo export <sid>`）
- Chat: `POST /api/chat {sid,text,force?}` → `{task_id,status,risk}`；`GET /api/chat/status?task_id=` → `{status: running|awaiting_approval|done|error|rejected|cancelled, reply, risk, events[]}`（events 实时含 `tool/text/step/error`）；`POST /api/chat/approve|reject|cancel {task_id}`
- Tasks: `GET /api/tasks?limit=` → 任务卡片（状态/风险/步骤数/摘要，倒序）
- Files: `GET /api/fs/list?path=`、`GET /api/fs/read?path=&limit=` —— 仅限工作目录内，越界返回 400
- 风险闸门：`rm`/`taskkill`/`git push`/删库/提权等模式会停在 `awaiting_approval`，手机点「允许执行」才真正跑（`force:true` 可跳过）
- Model: `GET /api/models`、`POST /api/model {model}`（存 `<home>/model.txt`，下次运行即生效）
- HTTP：`GET /`、`GET /api/board`、`GET /api/pending`、`POST /api/send`、`POST /api/complete`

详见 `references/protocol.md`。
