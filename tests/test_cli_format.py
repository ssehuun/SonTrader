"""CLI 출력 정렬 (`cli.py`의 표시 폭 헬퍼).

분봉 수집은 종목당 약 980줄을 쏟아낸다. 한글 종목명이 섞인 열이 어긋나면
눈으로 훑을 수 없어서, 글자 수가 아니라 **터미널 표시 폭**으로 맞춘다.
"""

from __future__ import annotations

from sontrader.cli import _display_width, _pad


def test_ascii_width_is_character_count():
    assert _display_width("005930") == 6


def test_hangul_counts_as_two_columns():
    """한글은 터미널에서 두 칸이다. len()으로 맞추면 열이 밀린다."""
    assert _display_width("삼성전자") == 8
    assert len("삼성전자") == 4


def test_mixed_ascii_and_hangul():
    assert _display_width("005930 삼성전자") == 6 + 1 + 8


def test_pad_aligns_by_display_width_not_length():
    """핵심 검증 — 이름 길이가 달라도 뒤따르는 열의 시작 위치가 같아야 한다."""
    a = _pad("005930 삼성전자", 20)  # 표시 폭 15
    b = _pad("035720 카카오", 20)  # 표시 폭 13
    c = _pad("000660 SK하이닉스", 20)  # 표시 폭 17

    assert _display_width(a) == _display_width(b) == _display_width(c) == 20


def test_pad_does_not_truncate_when_too_long():
    """잘라내면 종목을 못 알아본다 — 열이 밀리는 것보다 나쁘다."""
    long_name = "005930 " + "가" * 20

    assert _pad(long_name, 20) == long_name


def test_pad_leaves_exact_width_untouched():
    assert _pad("005930", 6) == "005930"
