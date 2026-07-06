"""Deterministic canonical-zip writer for the normalizer (KD18 P1).

Produces a byte-reproducible zip of a snapshot's INCLUDED entries. The
zip is a storage carrier (the S3 ``snapshot_key``), never a comparison
key: echo-match and dedup always go through ``aggregate_hash`` over the
raw file bytes, so the compressed stream is never hashed or byte-compared
across environments.

``ZIP_STORED`` (no DEFLATE) is used so reproducibility is
*unconditional* -- byte-identical across zlib versions and platforms,
not merely within a single process. Compression is unnecessary here: the
snapshot is already denylist-pruned source bounded by the level-2 cap.
Switching to DEFLATE with a fixed ``compresslevel`` remains a safe future
option -- it would not break echo-match, precisely because zip bytes are
never compared.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

# ZIP DOS epoch (minimum legal date_time). Pins every entry's timestamp
# so the archive never depends on wall-clock time.
_ZIP_EPOCH: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)


def write_canonical_zip(entries: Sequence[tuple[str, bytes]]) -> bytes:
    """Serialize ``(path, raw_bytes)`` entries into a byte-reproducible zip.

    Entries are sorted (by ``path``, then bytes -- ``path`` is assumed
    already canonical and unique) and each is written as a fully-pinned
    :class:`zipfile.ZipInfo`: fixed ``date_time``, per-entry
    ``ZIP_STORED`` (not inherited from the archive default),
    ``external_attr`` and ``create_system`` zeroed, no extra field or
    comment. The same set of entries in any input order yields identical
    bytes -- in this process and across zlib versions / platforms.

    Only INCLUDED entries belong here; excluded content (denylist,
    magic-mismatch, nested archives) never enters the snapshot zip.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for path, content in sorted(entries):
            info = zipfile.ZipInfo(filename=path, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, content)
        # ``writestr`` stamps ``external_attr = 0o600 << 16`` and leaves
        # ``create_system`` at the platform default (3 on POSIX), even for
        # a passed ZipInfo. Both live only in the central directory, which
        # is written on context-exit, so zero them on the retained infos
        # to keep the archive byte-canonical across platforms.
        for info in zf.filelist:
            info.external_attr = 0
            info.create_system = 0
    return buf.getvalue()
