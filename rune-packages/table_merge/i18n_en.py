"""화면 문구의 영어 — 코어가 실행하지 않고 값만 읽는다 (vader/core/rune_text.py). 이 버전의 룬과 함께 다닌다."""
EN = {
    "text": {
        "표 합치기·중복 정리": "Table merge & dedupe",
        "같은 건으로 볼 칸 (예: 전화 · 여럿이면 쉼표 · 비우면 실행할 때 물어요)": "Columns that identify the same record (e.g. phone · separate several with commas · leave empty to be asked when running)",
        "합칠 엑셀·CSV 파일을 골라 주세요.": "Choose the Excel or CSV files to merge.",
        "{0}: 읽지 못했어요 ({1})": "{0}: couldn't read it ({1})",
        "{0}: 첫 줄(칸 이름) 아래에 행이 없어요.": "{0}: no rows below the first line (column names).",
        "행 비교": "Comparing rows",
        "같은 건으로 보이는데 {0} 값이 달라요. 어떻게 할까요? ({1}/{2})": "These look like the same record but {0} differ. What should I do? ({1}/{2})",
        "앞": "Earlier",
        "뒤": "Later",
        "확인을 멈췄어요. 다시 실행하면 답한 것은 건너뛰고 이어서 물어요.": "Checking stopped. Run again to skip what you answered and continue asking.",
        "어느 칸이 같으면 같은 건으로 볼까요? (전화·이메일처럼 건마다 다른 칸이 좋아요)": "Which column identifies the same record? (A column unique per record, like phone or email, works best)",
        "합칠 파일": "Files to merge",
        "기준 칸을 고르지 않아 멈췄어요.": "Stopped because no key column was chosen.",
        "'{0}' 칸이 없어요. 있는 칸: {1}": "No '{0}' column. Available columns: {1}",
        "합친 파일": "Files merged",
        "읽은 행": "Rows read",
        "통합본 행": "Rows in merged file",
        "중복 제외": "Duplicates removed",
        "기준 칸 빈 행 제외": "Rows removed for empty key",
        "사람이 확인한 행": "Rows checked by a person",
        "다른 파일 값으로 채운 칸": "Cells filled from another file",
        "기준": "Key",
        "기준열": "Key columns",
        "표 합치기 완료": "Table merge complete"
    },
    "choices": {
        "어느 칸이 같으면 같은 건으로 볼까요? (전화·이메일처럼 건마다 다른 칸이 좋아요)": {
            "모든 열 비교": "Compare all columns",
            "열: {0}": "Column: {0}"
        },
        "같은 건으로 보이는데 {0} 값이 달라요. 어떻게 할까요? ({1}/{2})": {
            "앞의 것 유지": "Keep the earlier one",
            "뒤의 것으로 바꾸기": "Replace with the later one",
            "둘 다 남기기": "Keep both",
            "남은 것도 모두 앞의 것 유지": "Keep the earlier one for all remaining"
        }
    },
    "values": {
        "기준": {
            "모든 열 비교": "Compare all columns",
            "열: {0}": "Column: {0}"
        }
    }
}
