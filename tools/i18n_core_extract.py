"""코어가 화면에 보내는 한국어 문장을 뽑는다 (2026-11-24 영문판 4단계).

    python tools/i18n_core_extract.py          앱 사전(vader/desktop/assets/i18n-en.js)에 없는 것만 JSON 으로
    python tools/i18n_core_extract.py --all    모두

화면에 닿는 모듈만 본다(AI·발행자 전용인 screen.py·publisher.py 등은 뺀다). 뽑는 곳: raise 의 인자,
'message'·'error'·'reason'·'text'·'note'·'state_text'·'key_help' 키의 값. f-string 은 값 자리를 {0},{1} 로 바꾼 틀로 — 화면의 i18n.js 가 같은 틀로 바꾼다.
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = [
    "vader/desktop/controller.py", "vader/desktop/host.py",
    "vader/core/catalog.py", "vader/core/catalog_sync.py", "vader/core/installer.py", "vader/core/workshop.py",
    "vader/core/todo.py", "vader/core/todo_ai.py", "vader/core/messenger.py", "vader/core/account.py",
    "vader/core/gate.py", "vader/core/ai_choice.py", "vader/core/update.py", "vader/core/phone.py",
    "vader/core/runner.py", "vader/core/job.py", "vader/core/clients.py", "vader/core/release.py",
    "vader/core/record_tool.py", "vader/core/seal.py", "vader/core/signing.py", "vader/core/conversation.py",
    "vader/core/chat.py", "vader/core/config.py", "vader/core/loader.py", "vader/core/permissions.py",
]
KEYS = {"message", "error", "reason", "text", "note", "state_text", "key_help"}
HANGUL = re.compile(r"[가-힣]")


def template(node) -> str | None:
    """문자열·f-string·'…'+값+'…' 을 값 자리가 {0},{1} 인 틀로. 문자열이 아니면 None."""
    if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) or (
            isinstance(node, ast.Constant) and not isinstance(node.value, str)):
        return None
    parts, n = [], 0

    def flat(x):
        nonlocal n
        if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Add):
            flat(x.left)
            flat(x.right)
        elif isinstance(x, ast.Constant) and isinstance(x.value, str):
            parts.append(x.value)
        elif isinstance(x, ast.JoinedStr):
            for v in x.values:
                flat(v) if isinstance(v, ast.Constant) else hole()
        else:
            hole()

    def hole():
        nonlocal n
        parts.append("{%d}" % n)
        n += 1

    flat(node)
    return "".join(parts)


def strings(tree) -> set[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            found += [template(a) for a in node.exc.args]
        elif isinstance(node, ast.Call):   # Failure(message=…) 처럼 사람에게 보이는 키워드 인자
            found += [template(k.value) for k in node.keywords if k.arg in KEYS]
        elif isinstance(node, ast.Dict):
            found += [template(v) for k, v in zip(node.keys, node.values)
                      if isinstance(k, ast.Constant) and k.value in KEYS]
        elif isinstance(node, ast.IfExp):
            found += [template(node.body), template(node.orelse)]
    out = set()
    for s in found:
        for line in (s or "").split("\n"):
            line = line.strip()
            if HANGUL.search(line):
                out.add(line)
    return out


def known() -> dict:
    body = (ROOT / "vader/desktop/assets/i18n-en.js").read_text(encoding="utf-8")
    return json.loads(body[body.index("{"): body.rindex("}") + 1])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    found = set().union(*(strings(ast.parse((ROOT / m).read_text(encoding="utf-8"))) for m in MODULES))
    have = known()
    todo = sorted(found if "--all" in sys.argv else found - set(have))
    print(json.dumps(todo, ensure_ascii=False, indent=0))
    print(f"# {len(todo)}개 (전체 {len(found)}개, 사전 {len(have)}개)", file=sys.stderr)
