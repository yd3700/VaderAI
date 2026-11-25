# VaderAI

English · [한국어](README.ko.md)

**Interrupted or re-run, nothing is entered twice.**

VaderAI is **work-delegation RPA** for the Windows desktop. Repetitive data entry that people used to do by hand —
from Excel, CSV and PDF files into older office programs — is handled by **work packages** that a person has checked and approved.

- **Resume** — if a run is interrupted, it picks up where it stopped.
- **No duplicates** — it records what it processed so the same item is not handled twice when the same input comes in again (within the range a work package checkpoints).
- **Practice first** — a practice run shows the result before anything is entered for real. Only a person approves real use.
- **Only allowed windows** — screen automation works only in app windows a person has allowed, one approval card per action.
- **Runs locally** — files and screens are processed on your PC. The desktop app's code is public in this repository.

Work packages can be built with the AI you already use (Claude Code, Codex and others): MCP tools bundled with the app help
build, test and pack them, and a person installs and approves them for real use in the VaderAI window.

> **Language:** the app's screens are in Korean today; English is in preparation. The website is available in
> [English](https://vaderai-web.vercel.app/en/) and Korean.

## Install

Download `VaderAI-win-Setup.exe` from [Releases](../../releases) and run it. After installation the app updates itself.
To uninstall, go to Windows **Settings → Apps → Installed apps → VaderAI → Uninstall**.
See the [Code signing policy](CODE_SIGNING.md).

## Build it yourself

You need Windows 10/11, Python 3.11+, Rust (rustup) and the .NET SDK (`dotnet tool install -g vpk`).

```powershell
python -m venv .venv-desktop; .venv-desktop\Scripts\pip install -e .[desktop] pyinstaller velopack
.venv-desktop\Scripts\python -m pytest
powershell -File packaging\build.ps1 -Version 0.1.0
```

## Online features and privacy

Account sign-in, the messenger (end-to-end encrypted) and the shared package list talk to the VaderAI web service; automatic
updates talk to this repository's GitHub Releases. The web service's code is not in this repository (open core).
What is sent is described in the [privacy policy](https://vaderai-web.vercel.app/en/privacy/). The first-run screen
lets you turn off automatic online checks, and you can change it later under **Automatic online checks** (자동 온라인 확인) in the app's Account window.

## License

The desktop app code is [MIT](LICENSE). The name and logo follow [TRADEMARKS.md](TRADEMARKS.md).
Bundled third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Report security issues via [SECURITY.md](SECURITY.md); see [CONTRIBUTING.md](CONTRIBUTING.md) to contribute.
