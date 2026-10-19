# 把插件仓库准备好并打出与 manifest 一致的 tag。
#
# 用法：
#   pwsh ./publish.ps1 -Owner your-github-name
#   pwsh ./publish.ps1 -Owner your-name -Name "你的昵称" -Email "you@example.com"
#
# 只做本地操作：改 manifest 身份字段、git init/commit/tag，不会推送。

param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [string]$Repo = "maibot-codex-subscription",
    [string]$Name = "",
    [string]$Email = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$manifestPath = Join-Path $root "_manifest.json"
$manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest.author.name = if ($Name) { $Name } else { $Owner }
$manifest.author.url = "https://github.com/$Owner"
$manifest.urls.repository = "https://github.com/$Owner/$Repo"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 20) + "`n", $utf8)
Write-Host "manifest 已更新：author=$($manifest.author.name) repository=$($manifest.urls.repository)"

if (-not (Test-Path (Join-Path $root ".git"))) {
    git init -b main | Out-Null
    Write-Host "已执行 git init"
}
$gitName = if ($Name) { $Name } else { $Owner }
git config user.name $gitName | Out-Null
if ($Email) { git config user.email $Email | Out-Null }

git add -A
try {
    git commit -m "release: v$($manifest.version)" | Out-Null
} catch {
    Write-Host "没有新的改动需要提交（或 commit 失败），继续打 tag"
}

$tag = "v$($manifest.version)"
if (git tag --list $tag) {
    Write-Host "tag $tag 已存在，跳过"
} else {
    git tag $tag
    Write-Host "已打 tag：$tag"
}

Write-Host ""
Write-Host "接下来在 GitHub 新建公开空仓库 $Repo，然后执行："
Write-Host "  git remote add origin https://github.com/$Owner/$Repo.git"
Write-Host "  git push -u origin main"
Write-Host "  git push origin $tag"
Write-Host ""
Write-Host "然后去 https://github.com/Mai-with-u/plugin-repo/issues/new/choose 提交「Add Plugin」，"
Write-Host "插件 ID 填 $($manifest.id)，仓库地址填 $($manifest.urls.repository)"
