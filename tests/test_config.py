"""설정 파싱 단위 테스트."""

import pytest

from db_migration.config import ConfigError, parse_config


def _base(**overrides):
    data = {
        "source": {"dsn": "postgresql://s", "schema": "src"},
        "destination": {"dsn": "postgresql://d"},
        "tables": ["users"],
    }
    data.update(overrides)
    return data


def test_minimal_config():
    cfg = parse_config(_base())
    assert cfg.source.schema == "src"
    assert cfg.destination.schema == "public"
    assert cfg.mode == "append"
    assert cfg.tables[0].name == "users"
    assert cfg.reset_sequences is True


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("SRC_DSN", "postgresql://from-env")
    cfg = parse_config(_base(source={"dsn": "${SRC_DSN}"}))
    assert cfg.source.dsn == "postgresql://from-env"


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NOPE_DSN", raising=False)
    with pytest.raises(ConfigError, match="NOPE_DSN"):
        parse_config(_base(source={"dsn": "${NOPE_DSN}"}))


def test_table_spec_with_where_and_mode():
    cfg = parse_config(
        _base(mode="truncate", tables=[{"name": "orders", "where": " id > 10 ", "mode": "upsert"}, "users"])
    )
    assert cfg.effective_mode("orders") == "upsert"
    assert cfg.effective_mode("users") == "truncate"
    assert cfg.table_spec("orders").where == "id > 10"
    assert cfg.table_spec("unknown").where is None


def test_invalid_mode():
    with pytest.raises(ConfigError, match="mode"):
        parse_config(_base(mode="replace"))


def test_copy_all_false_requires_tables():
    with pytest.raises(ConfigError, match="tables"):
        parse_config(_base(tables=None))


def test_copy_all_true_without_tables_is_ok():
    cfg = parse_config(_base(copy_all=True, tables=None, exclude=["audit"]))
    assert cfg.copy_all and cfg.exclude == ("audit",)


def test_duplicate_table_rejected():
    with pytest.raises(ConfigError, match="중복"):
        parse_config(_base(tables=["users", {"name": "users"}]))


def test_duplicate_order_rejected():
    with pytest.raises(ConfigError, match="order"):
        parse_config(_base(order=["a", "a"]))
