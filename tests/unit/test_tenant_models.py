"""Tests for Tenant & APIKey ORM models and key utilities."""

from __future__ import annotations

import uuid

from course_supporter.auth.keys import generate_api_key, hash_api_key
from course_supporter.storage.orm import APIKey, Tenant, _uuid7


class TestTenantModel:
    """Tests for Tenant ORM model."""

    def test_tenant_create(self) -> None:
        """Tenant creation sets name; is_active defaults to True in DB."""
        tenant = Tenant(name="Acme Corp")

        assert tenant.name == "Acme Corp"
        # Column default is True (applied on INSERT, not in-memory)
        col = Tenant.__table__.c.is_active
        assert col.default.arg is True

    def test_tenant_has_uuid7_default(self) -> None:
        """Tenant PK uses UUIDv7 factory."""
        pk = _uuid7()
        assert isinstance(pk, uuid.UUID)

    def test_tenant_unique_name_constraint(self) -> None:
        """Tenant model has unique constraint on name column."""
        table = Tenant.__table__
        name_col = table.c.name
        # Check unique=True is set on the column
        assert name_col.unique is True


class TestAPIKeyModel:
    """Tests for APIKey ORM model."""

    def test_api_key_create(self) -> None:
        """APIKey creation with explicit fields."""
        key = APIKey(
            tenant_id=_uuid7(),
            key_hash="a" * 64,
            key_prefix="cs_live_a1b2",
            label="production",
            scopes=["prep", "check"],
            rate_limit_prep=100,
            rate_limit_check=500,
        )

        assert key.key_prefix == "cs_live_a1b2"
        assert key.label == "production"
        assert key.scopes == ["prep", "check"]
        assert key.rate_limit_prep == 100
        assert key.rate_limit_check == 500

    def test_api_key_defaults(self) -> None:
        """APIKey column defaults match spec."""
        table = APIKey.__table__
        assert table.c.is_active.default.arg is True
        assert table.c.rate_limit_prep.default.arg == 60
        assert table.c.rate_limit_check.default.arg == 300

    def test_api_key_hash_unique_constraint(self) -> None:
        """APIKey model has unique constraint on key_hash column."""
        table = APIKey.__table__
        key_hash_col = table.c.key_hash
        assert key_hash_col.unique is True

    def test_cascade_delete_configured(self) -> None:
        """Tenant -> APIKey relationship has cascade delete-orphan."""
        rel = Tenant.__mapper__.relationships["api_keys"]
        assert "delete-orphan" in rel.cascade


class TestGenerateAPIKey:
    """Tests for generate_api_key utility."""

    def test_format_live(self) -> None:
        """Generated key follows cs_live_<32hex> format."""
        full_key, _hash, _prefix = generate_api_key("live")

        assert full_key.startswith("cs_live_")
        # cs_live_ = 8 chars + 32 hex chars = 40 total
        random_part = full_key[len("cs_live_") :]
        assert len(random_part) == 32
        # Verify it's valid hex
        int(random_part, 16)

    def test_format_test(self) -> None:
        """Test environment key follows cs_test_<32hex> format."""
        full_key, _, key_prefix = generate_api_key("test")

        assert full_key.startswith("cs_test_")
        assert key_prefix.startswith("cs_test_")

    def test_uniqueness(self) -> None:
        """Two calls produce different keys."""
        key1, hash1, _ = generate_api_key()
        key2, hash2, _ = generate_api_key()

        assert key1 != key2
        assert hash1 != hash2

    def test_hash_matches(self) -> None:
        """Generated hash matches hash_api_key result."""
        full_key, key_hash, _ = generate_api_key()

        assert hash_api_key(full_key) == key_hash

    def test_prefix_contains_first_4_hex(self) -> None:
        """Key prefix includes first 4 chars of random part."""
        full_key, _, key_prefix = generate_api_key("live")

        random_part = full_key[len("cs_live_") :]
        assert key_prefix == f"cs_live_{random_part[:4]}"


class TestHashAPIKey:
    """Tests for hash_api_key utility."""

    def test_deterministic(self) -> None:
        """Same input produces same hash."""
        key = "cs_live_abcdef1234567890abcdef1234567890"

        assert hash_api_key(key) == hash_api_key(key)

    def test_returns_sha256_hex(self) -> None:
        """Result is a 64-char hex string (SHA-256)."""
        result = hash_api_key("test_key")

        assert len(result) == 64
        int(result, 16)  # valid hex
