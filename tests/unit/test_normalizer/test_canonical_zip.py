"""Tests for the deterministic canonical-zip writer (KD18 P1).

ZIP_STORED makes byte-reproducibility unconditional: the same entries in
any order produce identical bytes, in this process and across zlib
versions / platforms. Also verifies the pinned ZipInfo fields and a
round-trip through stdlib zipfile.
"""

import io
import zipfile

from course_supporter.normalizer.canonical_zip import write_canonical_zip


def _infos(data: bytes) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.infolist()


class TestByteReproducibility:
    def test_order_invariant_bytes(self) -> None:
        a = ("src/a.py", b"print('a')\n")
        b = ("src/b.py", b"print('b')\n")
        c = ("README.md", b"# title\n")
        assert write_canonical_zip([a, b, c]) == write_canonical_zip([c, b, a])

    def test_independent_calls_identical(self) -> None:
        # No wall-clock timestamp -> two independent builds match. With
        # STORED there is no zlib in play, so this holds cross-version.
        entries = [("m.py", b"x = 1\n"), ("d/n.txt", b"hello")]
        assert write_canonical_zip(entries) == write_canonical_zip(list(entries))

    def test_empty_is_deterministic(self) -> None:
        assert write_canonical_zip([]) == write_canonical_zip([])


class TestPinnedZipInfo:
    def test_all_entries_stored_not_deflated(self) -> None:
        data = write_canonical_zip([("a.py", b"x"), ("b.py", b"yy")])
        for info in _infos(data):
            assert info.compress_type == zipfile.ZIP_STORED

    def test_fixed_date_time_and_attrs(self) -> None:
        data = write_canonical_zip([("a.py", b"x")])
        info = _infos(data)[0]
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.external_attr == 0
        assert info.create_system == 0
        assert info.extra == b""
        assert info.comment == b""

    def test_entries_sorted_by_path(self) -> None:
        data = write_canonical_zip([("z.py", b"1"), ("a.py", b"2"), ("m.py", b"3")])
        assert [i.filename for i in _infos(data)] == ["a.py", "m.py", "z.py"]


class TestRoundTrip:
    def test_unzip_recovers_paths_and_bytes(self) -> None:
        entries = [
            ("src/main.py", b"print('hi')\n"),
            ("data/x.json", b'{"k": 1}'),
            ("README.md", b"# title\n"),
        ]
        data = write_canonical_zip(entries)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            recovered = {name: zf.read(name) for name in zf.namelist()}
        assert recovered == dict(entries)

    def test_unicode_path_round_trip(self) -> None:
        entries = [("тека/файл.txt", "вміст".encode())]
        data = write_canonical_zip(entries)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.read("тека/файл.txt") == "вміст".encode()
