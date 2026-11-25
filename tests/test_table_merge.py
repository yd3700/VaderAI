"""기본 룬 '표 합치기·중복 정리' — 실린 샘플 3개로 돌려 본다 (2026-11-19)."""
from pathlib import Path

from vader.core import profile, tables
from vader.core.installer import install
from vader.sdk import Input, UserActionResponse

SOURCE = Path(__file__).resolve().parents[1] / "rune-packages" / "table_merge"


class Ctx:
    """표 룬이 쓰는 ctx 만 — 묻는 말에는 정해 둔 답을 순서대로."""

    def __init__(self, folder, params, answers):
        self.folder, self.params, self.answers, self.asked, self.saved = folder, params, list(answers), [], None
        self.dry_run = True

    def read_table(self, source, sheet=None):
        return tables.read_table(source.path, sheet)

    def write_table(self, name, rows):
        return tables.write_table(self.folder, name, rows)

    def request_user_action(self, request):
        self.asked.append(request)
        return self.answers.pop(0)

    def load_checkpoint(self):
        return self.saved

    def save_checkpoint(self, data):
        self.saved = data

    def clear_checkpoint(self):
        self.saved = None

    def check_cancelled(self): pass
    def report_progress(self, *a): pass
    def log(self, *a, **k): pass


def run(tmp_path, params, answers):
    from table_merge.rune import TableMerge   # rune-packages 가 sys.path 에 없으면 아래에서 넣는다

    ctx = Ctx(tmp_path, params, answers)
    samples = sorted((SOURCE / "samples").iterdir())
    return TableMerge().run(ctx, [Input(path=p) for p in samples]), ctx


def setup_module():
    import sys
    sys.path.insert(0, str(SOURCE.parent))


def test_samples_merge_by_phone_and_ask_only_the_unsure_one(tmp_path):
    result, ctx = run(tmp_path, {"기준열": "전화"}, [UserActionResponse("ok", "앞의 것 유지")])
    assert result.ok, result.failures
    assert len(ctx.asked) == 1 and "이메일" in ctx.asked[0].prompt   # 이바다 — 이메일·지역이 달라 사람에게
    assert result.summary.items() >= {"합친 파일": 3, "읽은 행": 14, "통합본 행": 8, "중복 제외": 5,
                                   "기준 칸 빈 행 제외": 1, "사람이 확인한 행": 1, "다른 파일 값으로 채운 칸": 3}.items()
    merged = tables.read_table(Path(result.artifacts[0]))
    assert merged[0] == ["이름", "전화", "이메일", "지역", "메모", "출처", "원래 행"]
    cloud = next(r for r in merged if r[0] == "박구름")
    assert cloud[2] == "cloud@example.com" and cloud[4] == "온라인 가입"   # 서울점의 빈칸을 온라인 값으로 채움
    assert ctx.saved is None   # 다 끝나면 체크포인트를 지운다


def test_without_a_setting_it_asks_the_key_and_both_keeps_the_unsure_row(tmp_path):
    result, ctx = run(tmp_path, {}, [UserActionResponse("ok", "열: 전화"), UserActionResponse("abort")])
    from table_merge.rune import WHOLE
    assert ctx.asked[0].options == ["열: 이름", "열: 전화", "열: 이메일", WHOLE]   # 모든 파일에 있는 칸만(종류를 글자로) + 전체
    assert not result.ok and "이어서" in result.failures[0].message
    again, ctx2 = run(tmp_path, {"기준열": "전화"}, [UserActionResponse("ok", "둘 다 남기기")])
    assert again.ok and again.summary["통합본 행"] == 9


def test_the_bundled_rune_passes_the_table_rules_and_installs(tmp_path):
    assert profile.declared(SOURCE) == "table" and profile.check(SOURCE) == []
    installed = install(SOURCE, tmp_path / "runes")
    assert installed.manifest.id == "table_merge"
    assert sorted(p.name for p in (installed.path / "samples").iterdir()) == sorted(p.name for p in (SOURCE / "samples").iterdir())


def table(folder, name, rows):
    return Input(path=Path(tables.write_table(folder, name, rows)))


def test_one_empty_key_part_keeps_the_row_and_only_phone_columns_drop_punctuation(tmp_path):
    from table_merge.rune import TableMerge

    src = tmp_path / "in"
    src.mkdir()
    rows = [["사번", "이름", "부서"],
            ["2026-0001", "가", ""], ["20260001", "나", ""],   # 사번은 전화가 아니다 — 기호가 다르면 다른 건
            ["", "다", "총무"], ["", "다", "총무"]]           # 복합 기준 중 하나만 비었다 — 빼지 않고 비교
    ctx = Ctx(tmp_path, {"기준열": "사번, 이름"}, [])
    result = TableMerge().run(ctx, [table(src, "a.csv", rows)])
    assert result.ok and not ctx.asked
    assert result.summary["통합본 행"] == 3 and result.summary["기준 칸 빈 행 제외"] == 0 and result.summary["중복 제외"] == 1


def test_a_column_named_like_the_all_choice_can_still_be_chosen_alone(tmp_path):
    """열은 '열: 이름', 전체는 '모든 열 비교' — '모든 칸' 이라는 열도 그 열 하나로 고른다 (codex1 영문판 A·4·5 3·5차)."""
    from table_merge.rune import ALL, WHOLE, TableMerge, column

    columns = ["모든 칸", "전화"]
    tables_ = [("a.xlsx", columns, []), ("b.xlsx", columns, [])]
    at = {column(c): i for i, c in enumerate(columns)}
    one = Ctx(tmp_path, {}, [UserActionResponse("ok", "열: 모든 칸")])
    assert TableMerge.key_columns(one, tables_, columns, at) == [0]          # 그 열 하나
    assert one.asked[0].options == ["열: 모든 칸", "열: 전화", WHOLE]
    every = Ctx(tmp_path, {}, [UserActionResponse("ok", WHOLE)])
    assert TableMerge.key_columns(every, tables_, columns, at) is None       # 전체 칸
    assert TableMerge.key_columns(Ctx(tmp_path, {"기준열": ALL}, []), tables_, columns, at) is None   # 설정의 '모든 칸' 은 그대로 전체


def test_a_column_literally_named_like_the_whole_choice_keeps_its_own_meaning(tmp_path):
    """열 이름이 '(모든 칸)' 이어도 — 그 열을 고르면 그 열로, 전체를 고르면 전체로 비교하고 요약도 다르게 보인다 (codex1 영문판 4·5차)."""
    from table_merge.rune import WHOLE, TableMerge

    src = tmp_path / "in"
    src.mkdir()
    a = tables.write_table(src, "a.xlsx", [["(모든 칸)", "other"], ["1", "A"]])
    b = tables.write_table(src, "b.xlsx", [["(모든 칸)", "other"], ["1", "B"]])
    def merge(answers):
        out = tmp_path / f"out{len(answers)}"
        out.mkdir()
        ctx = Ctx(out, {}, answers)
        return TableMerge().run(ctx, [Input(path=Path(a)), Input(path=Path(b))]), ctx
    by_column, ctx = merge([UserActionResponse("ok", "열: (모든 칸)"), UserActionResponse("ok", "앞의 것 유지")])
    assert ctx.asked[0].options == ["열: (모든 칸)", "열: other", WHOLE]
    assert by_column.summary["통합본 행"] == 1 and by_column.summary["기준"] == "열: (모든 칸)"
    by_all, _ = merge([UserActionResponse("ok", WHOLE)])
    assert by_all.summary["통합본 행"] == 2 and by_all.summary["기준"] == WHOLE
