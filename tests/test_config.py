"""설정 파싱 단위 테스트."""

import pytest

from psycopg.conninfo import conninfo_to_dict

from db_migration.config import ConfigError, parse_config


def _base(**overrides):
    data = {
        "source": {"host": "src-host", "user": "u", "password": "p", "port": 5433, "database": "srcdb", "schema": "src"},
        "destination": {"host": "dst-host", "database": "dstdb"},
        "tables": ["users"],
    }
    data.update(overrides)
    return data


def test_minimal_config():
    cfg = parse_config(_base())
    assert cfg.source.schema == "src"
    assert cfg.destination.schema == "public"
    assert conninfo_to_dict(cfg.source.dsn) == {
        "host": "src-host", "user": "u", "password": "p", "port": "5433", "dbname": "srcdb",
    }
    assert conninfo_to_dict(cfg.destination.dsn) == {"host": "dst-host", "dbname": "dstdb"}


def test_password_with_special_chars_is_quoted():
    cfg = parse_config(_base(source={"host": "h", "database": "d", "password": "p@ss 'w\\rd"}))
    assert conninfo_to_dict(cfg.source.dsn)["password"] == "p@ss 'w\\rd"


def test_database_required_without_dsn():
    with pytest.raises(ConfigError, match="database"):
        parse_config(_base(source={"host": "h"}))


def test_invalid_port():
    with pytest.raises(ConfigError, match="port"):
        parse_config(_base(source={"host": "h", "database": "d", "port": "abc"}))


def test_dsn_still_supported_and_overridden_by_parts():
    cfg = parse_config(_base(source={"dsn": "postgresql://u:p@h:5432/d", "database": "other"}))
    info = conninfo_to_dict(cfg.source.dsn)
    assert info["dbname"] == "other" and info["host"] == "h"
    assert cfg.mode == "append"
    assert cfg.tables[0].name == "users"
    assert cfg.reset_sequences is True


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("SRC_PASS", "from-env")
    cfg = parse_config(_base(source={"host": "h", "database": "d", "password": "${SRC_PASS}"}))
    assert conninfo_to_dict(cfg.source.dsn)["password"] == "from-env"


def test_env_default_used_when_missing_or_empty(monkeypatch):
    src = {"host": "${SRC_HOST:-localhost}", "port": "${SRC_PORT:-5432}", "database": "d"}

    monkeypatch.delenv("SRC_HOST", raising=False)
    monkeypatch.delenv("SRC_PORT", raising=False)
    info = conninfo_to_dict(parse_config(_base(source=src)).source.dsn)
    assert info["host"] == "localhost" and info["port"] == "5432"

    monkeypatch.setenv("SRC_HOST", "")
    info = conninfo_to_dict(parse_config(_base(source=src)).source.dsn)
    assert info["host"] == "localhost"

    monkeypatch.setenv("SRC_HOST", "real-host")
    monkeypatch.setenv("SRC_PORT", "6543")
    info = conninfo_to_dict(parse_config(_base(source=src)).source.dsn)
    assert info["host"] == "real-host" and info["port"] == "6543"


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NOPE_PASS", raising=False)
    with pytest.raises(ConfigError, match="NOPE_PASS"):
        parse_config(_base(source={"host": "h", "database": "d", "password": "${NOPE_PASS}"}))


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
