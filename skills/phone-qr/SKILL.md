---
name: phone-qr
description: 生成手机配对二维码并展示在对话里。Use when the user says 「二维码」「扫码」「扫码连接」「配对码」「显示配对二维码」「让手机连上来」「phone pairing QR」「show QR code」, or wants to connect the phone app to MiMo Desktop by scanning.
---

# 手机配对二维码（Phone Pair QR）

在 MiMo Desktop 对话里调用本技能：生成一枚**手机配对二维码**并贴在聊天中，用户用手机相机扫一扫即可打开手机 App 并自动配对，随后就能在手机上监督/执行电脑端任务。

## Workflow

1. 生成二维码（用 PowerShell，Python 优先取 `$env:MIMO_PYTHON`）：

```powershell
$py = if ($env:MIMO_PYTHON) { $env:MIMO_PYTHON } else { "python" }
$bridge = "$env:USERPROFILE\.config\mimocode\skills\phone-remote\scripts\bridge.py"
& $py $bridge qr
```

输出三行：`配对码：`、`链接：`、`二维码图片：`（PNG 绝对路径）。

2. 用 `present_files` 把那张 PNG 展示给用户（`files` 里放 PNG 路径，`cwd` 取 PNG 所在目录），explanation 写「手机扫码即可连接 MiMo」。

3. 回复里说明三件事，保持简短：
   - 用手机**相机**扫对话里的二维码（或点图片长按识别）
   - 扫码后会自动打开 App、自动配对、自动登录，**无需输入任何内容**
   - 配对码 10 分钟内有效、**一码一用**；过期就再调用一次本技能

4. 可选补充（用户问「电脑上怎么出码」时）：电脑本机浏览器打开 `http://127.0.0.1:8765/pair` 是大图二维码页面；`bridge.py pair` 只打印 6 位数字。

## Important

- 二维码里的 URL 形如 `http://<电脑局域网IP>:8765/app#pair=<6位码>`，其中 `#pair=` 就是一次性配对码，**不含**设备凭据与长期令牌。
- 不要把 `auth_token.txt` 的内容贴进对话或二维码；手机端永远不使用固定令牌（配对成功后由 `/api/session` 下发当次随机令牌）。
- 若 `qr` 命令报「二维码生成失败」，检查 `<home>/qrtool/node_modules/qrcode` 是否存在（`<home>` = `~\.local\share\mimocode\phone-remote`），必要时在 `qrtool` 目录执行 `npm install qrcode`。
- 若手机扫不开链接，多半是手机与电脑不在同一 Wi-Fi；出门场景需要先配好 Tailscale。
