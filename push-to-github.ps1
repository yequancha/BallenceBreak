# 一键把「久坐休息提醒」上传到 GitHub（HTTPS + Git Credential Manager）
#
# 用法（在项目目录里执行任意一种）：
#   1) 右键本文件 ->「使用 PowerShell 运行」
#   2) powershell -ExecutionPolicy Bypass -File .\push-to-github.ps1
#   3) 带参数免交互：
#      powershell -ExecutionPolicy Bypass -File .\push-to-github.ps1 `
#        -RepoUrl https://github.com/你的用户名/BallenceBreak.git `
#        -UserName "你的昵称" -UserEmail "你的邮箱"
#
# 前期准备：先在 GitHub 网页建一个「空仓库」（不要勾 Add README / .gitignore / license）。
# 安全性：凭据由 Windows 凭据管理器（GCM）弹出登录框收集，本脚本不保存、不回显任何 Token。

param(
    [string]$RepoUrl   = "",
    [string]$UserName  = "",
    [string]$UserEmail = "",
    [string]$Message   = "久坐休息提醒：首版，含窗口自适应（DPI/分辨率）",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }

Say "=== 上传 BallenceBreak 到 GitHub ==="

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "未找到 git，请先安装 Git for Windows 并加入 PATH。"
}

# 1) 本地仓库
if (-not (Test-Path '.git')) {
    git init -b main | Out-Null
    Say "已初始化本地仓库（分支 main）"
}

# 2) 仓库地址
if (-not $RepoUrl) {
    $RepoUrl = (Read-Host "请输入仓库地址（如 https://github.com/yourname/BallenceBreak.git）").Trim()
}
if ($RepoUrl -notmatch '^https://') {
    throw "本脚本只支持 HTTPS 地址，例如 https://github.com/yourname/BallenceBreak.git"
}

# 3) 提交身份
if (-not $UserName)  { $UserName  = (Read-Host "提交者昵称 user.name").Trim() }
if (-not $UserEmail) { $UserEmail = (Read-Host "提交者邮箱 user.email（可用 数字ID+用户名@users.noreply.github.com）").Trim() }
if (-not $UserName -or -not $UserEmail) { throw "user.name / user.email 不能为空。" }
git config --local user.name  $UserName
git config --local user.email $UserEmail
Say "提交身份：$UserName <$UserEmail>"

# 4) 让 GCM 负责登录（只影响本仓库，不写全局配置）
git config --local credential.helper manager

# 5) 提交
git add -A
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Say "工作区无改动，跳过提交"
} else {
    git commit -m $Message | Out-Null
    Say ("已创建提交：" + (git log -1 --pretty=format:"%h %s"))
}

# 6) 远程
if ((git remote) -contains 'origin') {
    git remote set-url origin $RepoUrl
    Say "已更新远程 origin -> $RepoUrl"
} else {
    git remote add origin $RepoUrl
    Say "已添加远程 origin -> $RepoUrl"
}

if ($DryRun) {
    Warn "[DryRun] 跳过推送。正式执行：git push -u origin main"
    exit 0
}

# 7) 推送（首次会弹出 GCM 登录窗口，用 GitHub 账号授权即可）
Warn "即将推送：首次会弹出 GitHub 登录/授权窗口，按提示登录即可（无需手打 Token）。"
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    throw "推送失败。请检查：仓库地址是否正确、仓库是否为「空仓库」（若建仓库时勾了 README 需先 git pull --rebase origin main）。"
}

$web = $RepoUrl -replace '\.git$', ''
Say "完成！仓库地址：$web"
