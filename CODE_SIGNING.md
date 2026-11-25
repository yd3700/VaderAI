# Code signing policy

> **Status:** not yet approved by SignPath Foundation — current releases are **unsigned**. Once approved, releases are
> signed under this policy and this notice is removed.
> *지금 상태: SignPath Foundation 신청 전입니다 — 현재 릴리스는 서명되지 않았습니다.*

(After approval) Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

- **What is signed:** only release artifacts (the installer and the app executables) built by GitHub Actions from the public
  source in this repository. Bundled third-party binaries belong to their own projects.
- **Approval:** every release signing is approved manually by a person.
- **Publisher:** after approval, Windows shows the publisher as **SignPath Foundation**.

## Team roles

| Role | Members |
|---|---|
| Committers and reviewers | [@yd3700](https://github.com/yd3700) |
| Approvers | [@yd3700](https://github.com/yd3700) |

## Privacy

This program talks to the VaderAI web service when you use sign-in, the messenger or the shared package list, and it
periodically receives the shared package list. Automatic updates check this repository's GitHub Releases, starting with
builds that carry the update signing key. On first launch after installation, the app shows a privacy notice and lets you
turn off automatic checks (updates and the shared list); you can change this later under **Automatic online checks** in
the app's Account window (자동 온라인 확인). Details: [privacy policy](https://vaderai-web.vercel.app/en/privacy/)
([한국어](https://vaderai-web.vercel.app/ko/privacy/)).

This program will not transfer any information to other networked systems unless specifically requested by the user or
the person installing or operating it, except as described in the privacy policy above.
