$ErrorActionPreference = 'Stop'

$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'requirements.txt'

if (-not (Test-Path -LiteralPath $python)) {
    $candidates = @()
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($line in (& $pyLauncher.Source -0p 2>$null)) {
            if ($line -match '^\s*-V:\S+\s+(?:\*\s+)?(.+?)\s*$') {
                $candidate = $Matches[1].Trim()
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidates += $candidate }
            }
        }
    }
    $pathPython = Get-Command python -ErrorAction SilentlyContinue
    if ($pathPython) { $candidates += $pathPython.Source }

    $usable = @()
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            & $candidate -c 'import sys, venv, ensurepip' 2>$null
            if ($LASTEXITCODE -eq 0) { $usable += $candidate }
        } catch {
            # A launcher can list an interpreter whose runtime is unavailable; try the next one.
        }
    }
    if (-not $usable) { throw '未找到可运行且包含 venv/ensurepip 的 Python 3。' }
    & $usable[0] -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        throw '创建 run-analytics 本地 Python 环境失败。'
    }
}

if (-not (Test-Path -LiteralPath $python)) { throw "虚拟环境解释器不存在：$python" }
& $python -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw '安装 run-analytics 依赖失败。' }
Write-Host 'run-analytics 本地环境已就绪。'
