# ============================================================
#  MiMo POCKET — 一键安装
#  用法：在 PowerShell 里进入本目录后执行
#        powershell -ExecutionPolicy Bypass -File .\install.ps1
#  参数：-NoSkills     不安装 MiMo 技能（不注册 /二维码 命令）
#        -NoAutostart  不注册开机自启
# ============================================================
param(
    [switch]$NoSkills,
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeDir = Join-Path $Root "bridge"
$DataDir = Join-Path $env:USERPROFILE ".local\share\mimocode\phone-remote"
$SkillsDir = Join-Path $env:USERPROFILE ".config\mimocode\skills"

Write-Host "==> [1/5] 检查依赖" -ForegroundColor Cyan
$py = $env:MIMO_PYTHON
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "    未找到 Python，请先安装 Python 3.10+ 或设置 MIMO_PYTHON"; exit 1 }
Write-Host "    Python : $py"

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { Write-Host "    未找到 Node.js，请先安装 Node 18+（用于生成二维码）"; exit 1 }
Write-Host "    Node   : $node"

$mimo = (Get-Command mimo -ErrorAction SilentlyContinue).Source
if (-not $mimo) {
    $guess = Join-Path $env:APPDATA "npm\node_modules\@mimo-ai\cli\bin\mimo"
    if (Test-Path $guess) { $mimo = $guess }
}
if (-not $mimo) {
    Write-Host "    未找到 MiMoCode CLI（mimo）。请先安装并登录：npm i -g @mimo-ai/cli && mimo" -ForegroundColor Yellow
    Write-Host "    （桥接靠它在电脑本地执行指令，没有它无法工作）"
    exit 1
}
Write-Host "    mimo   : $mimo"

Write-Host "==> [2/5] 安装二维码依赖 (qrcode)" -ForegroundColor Cyan
Push-Location $BridgeDir
& $node (Join-Path (Split-Path $node) "..\lib\node_modules\npm\bin\npm-cli.js") install --no-fund --no-audit 2>$null
if (-not (Test-Path (Join-Path $BridgeDir "node_modules\qrcode"))) {
    npm install --no-fund --no-audit
}
Pop-Location
if (-not (Test-Path (Join-Path $BridgeDir "node_modules\qrcode"))) {
    Write-Host "    qrcode 安装失败，请手动在 bridge 目录执行 npm install"; exit 1
}
Write-Host "    OK"

if (-not $NoSkills) {
    Write-Host "==> [3/5] 安装 MiMo 技能（/手机配对二维码、/手机远程指令）" -ForegroundColor Cyan
    foreach ($s in @("phone-remote", "phone-qr")) {
        $src = Join-Path $Root "skills\$s"
        $dst = Join-Path $SkillsDir $s
        if (Test-Path $src) {
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Copy-Item -Recurse -Force (Join-Path $src "*") $dst
            Write-Host "    已安装技能 $s"
        }
    }
} else {
    Write-Host "==> [3/5] 跳过技能安装" -ForegroundColor DarkGray
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not $NoAutostart) {
    Write-Host "==> [4/5] 注册开机自启（启动文件夹，无需管理员）" -ForegroundColor Cyan
    $startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    $vbs = Join-Path $startup "mimo-phone-bridge.vbs"
    $wd = Join-Path $BridgeDir "watchdog.py"
    @(
        'Set sh = CreateObject("WScript.Shell")',
        ('sh.Run """{0}"" ""{1}""", 0, False' -f $py, $wd)
    ) | Set-Content -Path $vbs -Encoding ASCII
    Write-Host "    已写入 $vbs"
} else {
    Write-Host "==> [4/5] 跳过开机自启" -ForegroundColor DarkGray
}

Write-Host "==> [5/5] 启动服务（看门狗会自动拉起桥接）" -ForegroundColor Cyan
Start-Process -FilePath $py -ArgumentList "`"$BridgeDir\watchdog.py`"" -WindowStyle Hidden
Start-Sleep -Seconds 6

Write-Host ""
Write-Host "安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  1) 获取配对二维码（三选一）：" -ForegroundColor Yellow
Write-Host "       · 在 MiMo Desktop 说「给我配对码」"
Write-Host "       · 电脑浏览器打开 http://127.0.0.1:8765/pair"
Write-Host "       · 终端运行：  `"$py`" `"$BridgeDir\bridge.py`" qr"
Write-Host ""
Write-Host "  2) 手机扫码 → 自动打开页面并配对 → 建议「添加到主屏幕」"
Write-Host ""
Write-Host "  3) 以后直接点手机上的图标说话即可，电脑会自动执行。"
Write-Host ""
