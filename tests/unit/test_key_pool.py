"""Tests for KeyPool round-robin key rotation."""

import pytest
from pydantic import SecretStr

from course_supporter.key_pool import KeyPool


class TestKeyPoolParsing:
    """Test key parsing from raw strings."""

    def test_single_key(self) -> None:
        pool = KeyPool("my-key")
        assert len(pool) == 1
        assert pool.next_key().get_secret_value() == "my-key"

    def test_comma_separated(self) -> None:
        pool = KeyPool("key1,key2,key3")
        assert len(pool) == 3

    def test_comma_with_spaces(self) -> None:
        pool = KeyPool("key1, key2 , key3")
        assert len(pool) == 3
        assert pool.next_key().get_secret_value() == "key1"

    def test_whitespace_separated(self) -> None:
        pool = KeyPool("key1  key2\tkey3")
        assert len(pool) == 3

    def test_mixed_separators(self) -> None:
        pool = KeyPool("key1, key2  key3,key4")
        assert len(pool) == 4

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            KeyPool("")

    def test_only_whitespace_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            KeyPool("  , , ")

    def test_trailing_comma_ignored(self) -> None:
        pool = KeyPool("key1, key2,")
        assert len(pool) == 2


class TestKeyPoolRotation:
    """Test round-robin rotation."""

    def test_single_key_always_returns_same(self) -> None:
        pool = KeyPool("only-key")
        for _ in range(5):
            assert pool.next_key().get_secret_value() == "only-key"

    def test_round_robin_order(self) -> None:
        pool = KeyPool("a, b, c")
        results = [pool.next_key().get_secret_value() for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_two_keys_alternate(self) -> None:
        pool = KeyPool("first, second")
        assert pool.next_key().get_secret_value() == "first"
        assert pool.next_key().get_secret_value() == "second"
        assert pool.next_key().get_secret_value() == "first"


class TestKeyPoolAllKeys:
    """Test all_keys() accessor."""

    def test_returns_tuple_of_secret_str(self) -> None:
        pool = KeyPool("a, b")
        keys = pool.all_keys()
        assert isinstance(keys, tuple)
        assert all(isinstance(k, SecretStr) for k in keys)
        assert len(keys) == 2

    def test_values_match(self) -> None:
        pool = KeyPool("x, y, z")
        values = [k.get_secret_value() for k in pool.all_keys()]
        assert values == ["x", "y", "z"]


class TestKeyPoolRepr:
    """Test repr doesn't leak secrets."""

    def test_repr_shows_size_not_keys(self) -> None:
        pool = KeyPool("secret1, secret2")
        r = repr(pool)
        assert "secret1" not in r
        assert "secret2" not in r
        assert "size=2" in r
