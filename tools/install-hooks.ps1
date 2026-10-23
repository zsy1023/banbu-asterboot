# Install a git post-commit hook that pushes every commit to origin.
#
#   powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1 -Uninstall
#
# Kept ASCII-only on purpose: Windows PowerShell 5.1 reads BOM-less UTF-8
# files as ANSI and would mangle non-ASCII literals.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$hooksDir = Join-Path (Join-Path $repoRoot ".git") "hooks"
$source = Join-Path (Join-Path $PSScriptRoot "git-hooks") "post-commit"
$target = Join-Path $hooksDir "post-commit"

if (-not (Test-Path $hooksDir)) {
    throw "Not a git repository: $hooksDir not found"
}

if ($Uninstall) {
    if (Test-Path $target) {
        Remove-Item $target -Force
        Write-Output "[ok] post-commit hook removed"
    }
    else {
        Write-Output "[skip] no post-commit hook installed"
    }
    exit 0
}

if (-not (Test-Path $source)) {
    throw "Hook source missing: $source"
}

Copy-Item $source $target -Force
Write-Output "[ok] installed: $target"

$proxy = git -C $repoRoot config --get http.proxy
if (-not $proxy) {
    Write-Output "[note] no http.proxy configured; if pushes need a proxy run:"
    Write-Output "       git config http.proxy http://127.0.0.1:7897"
}
else {
    Write-Output "[info] proxy: $proxy"
}

$remote = git -C $repoRoot remote get-url origin 2>$null
if ($remote) {
    Write-Output "[info] remote: $remote"
}
else {
    Write-Output "[note] no origin remote yet; auto-push will be skipped"
}

Write-Output ""
Write-Output "Every commit will now be pushed to origin automatically."
Write-Output "To skip once: set BAMBU_NO_AUTOPUSH=1"
