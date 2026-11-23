# VaderAI 설치본 만들기 (런칭 0단계). 프로젝트 루트에서:  powershell -File packaging\build.ps1
#
#   1) PyInstaller 로 dist\VaderAI\ (vaderai-worker.exe + vaderai-cli.exe), cargo 로 VaderAI.exe (Rust 껍데기)
#   2) Velopack(vpk) 으로 releases\ 에 설치 파일(Setup.exe)과 업데이트 피드
#
# 필요: .venv-desktop 에 pyinstaller·velopack, .NET 의 vpk (dotnet tool install -g vpk), Rust(rustup)·MSVC 빌드 도구
# 아직 코드 서명은 하지 않는다 — 서명 인증서가 생기면 vpk pack 에 --signParams 를 더한다.
param([string]$Version)   # 비우면 vader.__version__ — 업데이트 시험 때만 넘긴다
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv-desktop\Scripts\python.exe'
$version = if ($Version) { $Version } else { & $python -c "import vader; print(vader.__version__)" }

# 화면 자동화 엔진을 먼저 — 그 해시가 vader/core/screen_pin.py 로 앱 묶음에 들어간다
& (Join-Path $PSScriptRoot 'agent_desktop.ps1')

& $python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') `
    (Join-Path $PSScriptRoot 'vaderai.spec')
if ($LASTEXITCODE) { throw 'PyInstaller 실패' }

# Rust 껍데기 — 사람이 켜는 VaderAI.exe. 파이썬은 그 뒤의 vaderai-worker.exe (Rust 전환 2단계)
cargo build --release --manifest-path (Join-Path $root 'shell\Cargo.toml')
if ($LASTEXITCODE) { throw 'cargo build 실패' }
Copy-Item (Join-Path $root 'shell\target\release\VaderAI.exe') (Join-Path $root 'dist\VaderAI\VaderAI.exe') -Force

# 화면 자동화 엔진 — 위에서 빌드한 것을 앱 옆에, 라이선스 고지와 함께 (vader/core/screen.py 가 이 경로만 쓴다)
Copy-Item (Join-Path $root 'tools\agent-desktop\agent-desktop.exe') (Join-Path $root 'dist\VaderAI\agent-desktop.exe') -Force
Copy-Item (Join-Path $root 'tools\agent-desktop\LICENSE-agent-desktop.txt') (Join-Path $root 'dist\VaderAI\LICENSE-agent-desktop.txt') -Force
Copy-Item (Join-Path $root 'THIRD_PARTY_NOTICES.md') (Join-Path $root 'dist\VaderAI\THIRD_PARTY_NOTICES.md') -Force

vpk pack --packId VaderAI --packVersion $version --packTitle VaderAI `
    --packDir (Join-Path $root 'dist\VaderAI') --mainExe VaderAI.exe `
    --icon (Join-Path $root 'vader\desktop\assets\vaderai.ico') `
    --outputDir (Join-Path $root 'releases')
if ($LASTEXITCODE) { throw 'vpk pack 실패' }

Write-Host "완료: releases\ (버전 $version)"
