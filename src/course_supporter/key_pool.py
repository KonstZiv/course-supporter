"""Round-robin API key pool for multi-key rotation.

Supports comma- and whitespace-separated keys from a single
environment variable. Safe in single-threaded asyncio context.
"""

import itertools
from collections.abc import Iterator

from pydantic import SecretStr


class KeyPool:
    """Round-robin pool of API keys.

    Parses a raw string of comma/whitespace-separated API keys
    and cycles through them on each call to ``next_key()``.

    Example::

        >>> pool = KeyPool("key_a, key_b, key_c")
        >>> pool.next_key().get_secret_value()
        'key_a'
        >>> pool.next_key().get_secret_value()
        'key_b'
        >>> len(pool)
        3
    """

    __slots__ = ("_index_cycle", "_keys")

    def __init__(self, raw: str) -> None:
        keys = [k.strip() for k in raw.replace(",", " ").split() if k.strip()]
        if not keys:
            msg = "KeyPool requires at least one non-empty key"
            raise ValueError(msg)
        self._keys: tuple[SecretStr, ...] = tuple(SecretStr(k) for k in keys)
        self._index_cycle: Iterator[int] = itertools.cycle(range(len(self._keys)))

    def next_key(self) -> SecretStr:
        """Return the next key in round-robin order."""
        return self._keys[next(self._index_cycle)]

    def all_keys(self) -> tuple[SecretStr, ...]:
        """Return all keys (e.g. for pre-creating SDK clients)."""
        return self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return f"KeyPool(size={len(self._keys)})"
