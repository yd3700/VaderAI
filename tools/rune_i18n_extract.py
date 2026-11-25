"""룬이 화면에 내보내는 한국어 문구를 뽑는다 — 룬 패키지 안 i18n_en.py 를 채울 때 (2026-11-24 영문판 5단계 후속).

    python tools/rune_i18n_extract.py rune-packages/<룬>          i18n_en.py 에 없는 문구만 JSON 으로
    python tools/rune_i18n_extract.py rune-packages/<룬> --all

뽑는 곳: 매니페스트 name·description·RuneField label, 질문(prompt·options), Failure(…) 사유, report_progress 의 글,
ctx.log 의 글, RuneResult summary 의 키, 사용자에게 가는 키워드(message·reason·text·label·prompt).
f-string·'…'+값 은 값 자리를 {0},{1} 로 바꾼 틀로. 화면은 같은 틀로 바꾸고 끼인 값은 그대로 둔다.
"""
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_core_extract import template  # noqa: E402

HANGUL = re.compile(r"[가-힣]")
KEYWORDS = {"name", "description", "label", "prompt", "message", "reason", "text", "note"}


def strings(tree) -> set[str]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        found += [template(k.value) for k in node.keywords if k.arg in KEYWORDS]
        if name == "Failure" and len(node.args) > 1:
            found.append(template(node.args[1]))
        elif name == "report_progress" and len(node.args) > 2:
            found.append(template(node.args[2]))
        elif name == "log" and len(node.args) > 1:
            found.append(template(node.args[1]))
        elif name in ("RuneField",) and node.args:
            found.append(template(node.args[0]))
        for k in node.keywords:
            if k.arg == "options" and isinstance(k.value, (ast.List, ast.Tuple)):
                found += [template(e) for e in k.value.elts]
            if k.arg == "summary" and isinstance(k.value, ast.Dict):
                found += [template(e) for e in k.value.keys if e is not None]
    for node in ast.walk(tree):   # summary = {...} 를 먼저 만들고 넘기는 꼴
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "summary" for t in node.targets) \
                and isinstance(node.value, ast.Dict):
            found += [template(e) for e in node.value.keys if e is not None]
    return {s.strip() for s in found if s and HANGUL.search(s)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path(sys.argv[1])
    found = set().union(*(strings(ast.parse(p.read_text(encoding="utf-8"))) for p in folder.rglob("*.py")
                          if p.name != "i18n_en.py" and "__pycache__" not in p.parts))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from vader.core import rune_text
    have = (rune_text.load(folder) or {}).get("text", {})
    todo = sorted(found if "--all" in sys.argv else found - set(have))
    print(json.dumps(todo, ensure_ascii=False, indent=0))
    print(f"# {len(todo)}개 (전체 {len(found)}개)", file=sys.stderr)
