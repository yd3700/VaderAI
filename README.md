# VaderAI

**중간에 끊겨도, 다시 돌려도, 중복은 없습니다.**
*Interrupted or re-run, nothing is entered twice.*

VaderAI는 Windows 데스크톱의 **업무대행 RPA**입니다. 엑셀·CSV·PDF와 오래된 사무 프로그램에 사람이 반복하던 입력을,
사람이 확인하고 승인한 **업무 패키지**가 대신합니다.

- **이어하기** — 중간에 끊기면 끊긴 자리부터 다시 합니다.
- **중복 방지** — 같은 입력을 다시 넣어도 같은 건을 두 번 처리하지 않도록 기록합니다(업무 패키지가 체크포인트를 쓰는 범위에서).
- **연습 먼저** — 실제로 넣기 전에 연습 실행으로 결과를 봅니다. 실사용 승인은 사람만 합니다.
- **허용한 창만** — 화면 자동화는 사람이 허용한 앱 창에서만, 조작은 승인 카드로 한 번씩.
- **로컬 실행** — 파일과 화면 처리는 이 PC에서 합니다. 데스크톱 앱의 코드는 이 저장소에 공개되어 있습니다.

업무 패키지는 평소 쓰는 AI(Claude Code·Codex 등)로 만들 수 있습니다 — 앱에 포함된 MCP 도구가 만들기·시험·포장을 돕고,
설치와 실사용 승인은 VaderAI 화면에서 사람이 합니다.

## 설치

[Releases](../../releases)에서 `VaderAI-win-Setup.exe`를 받아 실행합니다. 설치 뒤에는 앱이 스스로 업데이트를 받습니다.
제거는 Windows **설정 → 앱 → 설치된 앱 → VaderAI → 제거**에서 합니다.
코드 서명은 [CODE_SIGNING.md](CODE_SIGNING.md)를 보세요.

## 직접 빌드

Windows 10/11, Python 3.11+, Rust(rustup), .NET SDK(`dotnet tool install -g vpk`)가 필요합니다.

```powershell
python -m venv .venv-desktop; .venv-desktop\Scripts\pip install -e .[desktop] pyinstaller velopack
.venv-desktop\Scripts\python -m pytest
powershell -File packaging\build.ps1 -Version 0.1.0
```

## 온라인 기능과 개인정보

계정 로그인, 메신저(종단간 암호화), 공유 목록, 업데이트는 VaderAI 웹 서비스와 통신합니다 — 이 서비스의 코드는 이 저장소에 없습니다(오픈코어).
무엇을 보내는지는 [개인정보 처리방침](https://vaderai-web.vercel.app/ko/privacy/)에 있습니다.

## 라이선스

데스크톱 앱 코드는 [MIT](LICENSE)입니다. 이름과 로고는 [TRADEMARKS.md](TRADEMARKS.md)를 따릅니다.
함께 실리는 제3자 구성 요소는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있습니다.

보안 문제는 [SECURITY.md](SECURITY.md)로, 기여는 [CONTRIBUTING.md](CONTRIBUTING.md)를 보세요.
