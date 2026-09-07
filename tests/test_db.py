"""연결 파라미터 조합 단위 테스트 (실제 접속 없음)."""

from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict

from db_migration import db


def _captured_conninfo(dsn: str) -> dict:
    with patch("db_migration.db.psycopg.connect") as connect:
        db.connect(dsn, "public")
        return conninfo_to_dict(connect.call_args.args[0])


def test_keepalive_defaults_are_added():
    info = _captured_conninfo("host=h dbname=d")
    assert info["keepalives"] == "1"
    assert info["keepalives_idle"] == "30"
    assert info["connect_timeout"] == "15"
    assert info["application_name"] == "db-migration"
    assert info["host"] == "h" and info["dbname"] == "d"


def test_user_values_override_defaults():
    info = _captured_conninfo("host=h dbname=d keepalives_idle=60 application_name=custom")
    assert info["keepalives_idle"] == "60"
    assert info["application_name"] == "custom"
    assert info["keepalives"] == "1"
