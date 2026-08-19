<#
.SYNOPSIS
    ARIA 를 시작하고 브라우저를 엽니다.

.DESCRIPTION
    localhost 전용 로컬 웹 프로그램입니다. 외부 네트워크에 공개되지 않습니다.

.PARAMETER Port
    바인딩할 포트. 기본 8765.

.PARAMETER NoBrowser
    브라우저를 자동으로 열지 않습니다.

.PARAMETER Setup
    의존성을 설치하고 프론트엔드를 빌드한 뒤 시작합니다. 최초 1회 필요합니다.

.PARAMETER Rebuild
    프론트엔드만 다시 빌드합니다.

.EXAMPLE
    .\start-aria.ps1 -Setup
    .\start-aria.ps1
    .\start-aria.ps1 -Port 9000 -NoBrowser
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Setup,
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Warn($message) { Write-Host "!!  $message" -ForegroundColor Yellow }
function Write-Err($message)  { Write-Host "!!  $message" -ForegroundColor Red }

# --------------------------------------------------------------- 사전 점검

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Err 'python 을 찾을 수 없습니다. Python 3.11 이상을 설치하십시오.'
    exit 1
}

if ($Setup -or $Rebuild) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Err 'npm 을 찾을 수 없습니다. Node.js 18 이상을 설치하십시오.'
        exit 1
    }
}

# ------------------------------------------------------------------- Setup

if ($Setup) {
    if (-not (Test-Path $venvPython)) {
        Write-Step '가상환경을 만듭니다'
        python -m venv (Join-Path $backend '.venv')
    }
    Write-Step '백엔드 의존성을 설치합니다'
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -r (Join-Path $backend 'requirements.txt') --quiet

    Write-Step '프론트엔드 의존성을 설치합니다'
    Push-Location $frontend
    try {
        npm install
        Write-Step '프론트엔드를 빌드합니다'
        npm run build
    } finally {
        Pop-Location
    }
}
elseif ($Rebuild) {
    Write-Step '프론트엔드를 다시 빌드합니다'
    Push-Location $frontend
    try { npm run build } finally { Pop-Location }
}

if (-not (Test-Path $venvPython)) {
    Write-Err "가상환경이 없습니다. 먼저 다음을 실행하십시오:  .\start-aria.ps1 -Setup"
    exit 1
}

if (-not (Test-Path (Join-Path $frontend 'dist\index.html'))) {
    Write-Warn '프론트엔드가 빌드되지 않았습니다. API 만 동작합니다.'
    Write-Warn "빌드하려면:  .\start-aria.ps1 -Rebuild"
}

# --------------------------------------------------------------- 포트 확인

function Test-PortInUse([int]$candidate) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $candidate -ErrorAction SilentlyContinue
    return $null -ne $conns
}

if (Test-PortInUse $Port) {
    Write-Warn "포트 $Port 가 이미 사용 중입니다."
    $found = $false
    foreach ($candidate in ($Port + 1)..($Port + 20)) {
        if (-not (Test-PortInUse $candidate)) {
            $Port = $candidate
            $found = $true
            Write-Step "대신 포트 $Port 을(를) 사용합니다."
            break
        }
    }
    if (-not $found) {
        Write-Err '사용 가능한 포트를 찾지 못했습니다. -Port 로 직접 지정하십시오.'
        exit 1
    }
}

# ------------------------------------------------------------------- 실행

$url = "http://127.0.0.1:$Port"

Write-Host ''
Write-Host '  ARIA' -ForegroundColor White
Write-Host "  $url" -ForegroundColor Green
Write-Host '  데이터 폴더: ' -NoNewline
Write-Host (Join-Path $env:LOCALAPPDATA 'ARIA') -ForegroundColor Gray
Write-Host '  중지하려면 Ctrl+C' -ForegroundColor Gray
Write-Host ''

if (-not $NoBrowser) {
    # 서버가 뜬 뒤에 열리도록 별도 작업으로 지연시킨다.
    $null = Start-Job -ScriptBlock {
        param($target)
        for ($i = 0; $i -lt 40; $i++) {
            try {
                Invoke-WebRequest -Uri "$target/api/health" -UseBasicParsing -TimeoutSec 1 | Out-Null
                Start-Process $target
                return
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
    } -ArgumentList $url
}

$env:ARIA_PORT = "$Port"
# 백틱 줄바꿈 대신 인수 배열을 쓴다. 인수가 셸을 거치지 않는다.
$uvicornArgs = @(
    '-m', 'uvicorn', 'app.main:app',
    '--app-dir', $backend,
    '--host', '127.0.0.1',
    '--port', "$Port",
    '--log-level', 'info'
)

try {
    & $venvPython @uvicornArgs
} finally {
    Get-Job | Where-Object { $_.State -ne 'Running' } | Remove-Job -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Step 'ARIA 를 종료했습니다.'
}
