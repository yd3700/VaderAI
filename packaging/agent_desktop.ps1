# 화면 자동화 엔진(agent-desktop, vercel-labs, Apache-2.0)을 고정 커밋·고정 툴체인으로 빌드해 tools\agent-desktop\ 에 두고,
# 그 SHA-256 을 vader\core\screen_pin.py 에 적는다(앱 묶음에 들어가 실행 전 대조 — exe 옆 해시 파일은 믿지 않는다).
# 프로젝트 루트에서:  powershell -File packaging\agent_desktop.ps1     (build.ps1 이 PyInstaller 전에 부른다)
# 커밋·툴체인을 올릴 때는 아래 두 값과 vader\core\screen.py ENGINE_VERSION·ENVELOPE 를 같이, codex1 계약 검사부터.
$ErrorActionPreference = 'Stop'
$pin = '54855b6225aec00da4ac65e2d0362fd41cd9fcb3'
$rustc = 'rustc 1.99.1 (58cf8f9c3 2026-10-13)'
$root = Split-Path $PSScriptRoot -Parent
$src = Join-Path $root 'build\agent-desktop-src'
$target = Join-Path $root 'build\agent-desktop-target'   # 소스 밖 — 소스 폴더는 늘 깨끗해야 한다
$out = Join-Path $root 'tools\agent-desktop'
function Step-Git { git.exe @args; if ($LASTEXITCODE) { throw "git $($args -join ' ') 실패" } }

if (-not (Test-Path (Join-Path $src ".git"))) { Step-Git clone --quiet https://github.com/vercel-labs/agent-desktop.git $src }
Step-Git -C $src fetch --quiet origin $pin
Step-Git -C $src -c advice.detachedHead=false checkout --quiet --force $pin
Step-Git -C $src clean -xdfq
if ((git -C $src rev-parse HEAD) -ne $pin) { throw 'agent-desktop 커밋이 고정값과 다르다' }
if (git -C $src status --porcelain --ignored) { throw 'agent-desktop 소스 폴더가 깨끗하지 않다' }
# VaderAI 패치 — 입력칸(Edit) id 앞에 바로 위 부모 id 를 붙인다(이온엠 m_pTextBox 쌍둥이, 실PC 2026-11-16)
$patch = Join-Path $PSScriptRoot 'agent-desktop-vader.patch'
Step-Git -C $src apply --whitespace=nowarn $patch
$patchSha = (Get-FileHash $patch -Algorithm SHA256).Hash.ToLower()

$oldTc = $env:RUSTUP_TOOLCHAIN
try {
    # 저장소의 1.89 핀 대신 설치된 툴체인 — 아래에서 정확한 판을 확인. CI 는 VADER_RUST=1.99.1 로 그 판을 직접 고른다
    $env:RUSTUP_TOOLCHAIN = if ($env:VADER_RUST) { $env:VADER_RUST } else { 'stable' }
    if ((rustc --version) -ne $rustc) { throw "Rust 판이 다르다: $(rustc --version) (필요: $rustc)" }
    cargo build --release --locked -p agent-desktop --manifest-path (Join-Path $src 'Cargo.toml') --target-dir $target
    if ($LASTEXITCODE) { throw 'agent-desktop 빌드 실패' }
} finally { $env:RUSTUP_TOOLCHAIN = $oldTc }

New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $target 'release\agent-desktop.exe') $out -Force
Copy-Item (Join-Path $src 'LICENSE') (Join-Path $out 'LICENSE-agent-desktop.txt') -Force
$sha = (Get-FileHash (Join-Path $out 'agent-desktop.exe') -Algorithm SHA256).Hash.ToLower()
$pinText = @"
# 만든 곳: packaging/agent_desktop.ps1 — 손으로 고치지 않는다 (git 에 넣지 않는다)
COMMIT = "$pin"
PATCH = "$patchSha"
TOOLCHAIN = "$rustc"
SHA256 = "$sha"
"@
# BOM 없는 UTF-8 — 파이썬 소스 검사(ast)가 BOM 을 거절한다
[IO.File]::WriteAllText((Join-Path $root 'vader\core\screen_pin.py'), $pinText, (New-Object Text.UTF8Encoding $false))
Write-Host "agent-desktop $pin sha256 $sha"
