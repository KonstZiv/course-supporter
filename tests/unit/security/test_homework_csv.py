"""A .csv in homework: read as text, bounded like text (operator, 2026-11-05)."""

import io
import zipfile

import pytest

from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.stage1 import run_stage1

_ROW = b"date,region,amount\n2026-01-01,north,100\n"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_a_csv_is_read_as_text() -> None:
    result = run_stage1(filename="sales.csv", content=_ROW * 10, context="homework")
    assert "region" in (result.nfc_text or "")


def test_a_large_csv_under_the_file_cap_is_not_refused() -> None:
    content = _ROW * (600_000 // len(_ROW))
    result = run_stage1(filename="sales.csv", content=content, context="homework")
    assert "region" in (result.nfc_text or "")


def test_a_csv_over_the_file_cap_is_refused_like_txt() -> None:
    content = _ROW * (1_100_000 // len(_ROW))
    with pytest.raises(SecurityRejectedError):
        run_stage1(filename="sales.csv", content=content, context="homework")
    with pytest.raises(SecurityRejectedError):
        run_stage1(filename="notes.txt", content=content, context="homework")


def test_a_csv_in_an_archive_is_opened_not_set_aside() -> None:
    content = _zip({"word_stats.py": b"print('hi')\n", "sales.csv": _ROW * 10})
    result = run_stage1(filename="work.zip", content=content, context="homework")
    assert result.archive_entries is not None
    assert any("sales.csv" in repr(entry) for entry in result.archive_entries)
    assert not any("sales.csv" in repr(entry) for entry in result.not_opened)
