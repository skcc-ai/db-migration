"""실제 PostgreSQL 을 사용하는 통합 테스트.

환경변수 DB_MIGRATION_TEST_DSN 으로 접속 정보를 지정한다. 기본값은 로컬 테스트 컨테이너.
연결할 수 없으면 전체 스킵된다. 테스트마다 src / dst 스키마를 새로 만든다.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from db_migration.config import parse_config
from db_migration.models import Event
from db_migration.runner import MigrationError, run_migration

DSN = os.environ.get("DB_MIGRATION_TEST_DSN", "postgresql://test:test@localhost:15432/test")


def _can_connect() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_connect(), reason="테스트용 PostgreSQL 에 연결할 수 없음")

SCHEMA_DDL = """
CREATE TABLE {s}.users (
    id serial PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE {s}.products (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku text UNIQUE NOT NULL,
    price numeric(10,2) NOT NULL,
    price_with_tax numeric(10,2) GENERATED ALWAYS AS (price * 1.1) STORED
);
CREATE TABLE {s}.orders (
    id serial PRIMARY KEY,
    user_id int NOT NULL REFERENCES {s}.users(id),
    created_at date NOT NULL
);
CREATE TABLE {s}.order_items (
    order_id int NOT NULL REFERENCES {s}.orders(id),
    product_id bigint NOT NULL REFERENCES {s}.products(id),
    qty int NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
CREATE TABLE {s}.no_pk (
    v text
);
"""

SEED_SQL = """
INSERT INTO {s}.users (name) VALUES ('alice'), ('bob'), ('carol');
INSERT INTO {s}.products (sku, price) VALUES ('A', 10), ('B', 20);
INSERT INTO {s}.orders (user_id, created_at) VALUES
    (1, '2023-12-31'), (2, '2024-01-01'), (3, '2024-06-01');
INSERT INTO {s}.order_items VALUES (1, 1, 1), (2, 1, 2), (2, 2, 3), (3, 2, 4);
INSERT INTO {s}.no_pk VALUES ('x'), ('y');
"""


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS src CASCADE; DROP SCHEMA IF EXISTS dst CASCADE")
        c.execute("CREATE SCHEMA src; CREATE SCHEMA dst")
        c.execute(SCHEMA_DDL.format(s="src"))
        c.execute(SCHEMA_DDL.format(s="dst"))
        c.execute(SEED_SQL.format(s="src"))
        yield c
        c.execute("DROP SCHEMA IF EXISTS src CASCADE; DROP SCHEMA IF EXISTS dst CASCADE")


def _config(**overrides):
    data = {
        "source": {"dsn": DSN, "schema": "src"},
        "destination": {"dsn": DSN, "schema": "dst"},
        "copy_all": True,
        "mode": "append",
    }
    data.update(overrides)
    return parse_config(data)


def _count(conn, table, where="true"):
    return conn.execute(f"SELECT count(*) FROM dst.{table} WHERE {where}").fetchone()[0]


def _statuses(result):
    return {r.name: r.status for r in result.results}


def test_copy_all_orders_by_fk_and_copies_everything(conn):
    events: list[Event] = []
    plan, result = run_migration(_config(), on_event=events.append)

    names = [t.name for t in plan.tables]
    assert names.index("users") < names.index("orders") < names.index("order_items")
    assert names.index("products") < names.index("order_items")
    assert result.succeeded
    assert all(s == "success" for s in _statuses(result).values())
    assert _count(conn, "users") == 3
    assert _count(conn, "order_items") == 4
    assert _count(conn, "no_pk") == 2
    # generated 컬럼은 복사 대상에서 빠지고 대상에서 다시 계산된다
    assert conn.execute("SELECT price_with_tax FROM dst.products WHERE sku = 'A'").fetchone()[0] == 11
    rows = {r.name: r.rows for r in result.results}
    assert rows["users"] == 3 and rows["order_items"] == 4


def test_sequences_are_reset_after_copy(conn):
    run_migration(_config())
    # serial 과 identity 모두 다음 값이 MAX+1 이어야 한다
    new_user = conn.execute("INSERT INTO dst.users (name) VALUES ('dave') RETURNING id").fetchone()[0]
    new_prod = conn.execute("INSERT INTO dst.products (sku, price) VALUES ('C', 1) RETURNING id").fetchone()[0]
    assert new_user == 4
    assert new_prod == 3


def test_exclude_and_explicit_tables(conn):
    _, result = run_migration(_config(exclude=["no_pk", "order_items"]))
    assert set(_statuses(result)) == {"users", "products", "orders"}

    conn.execute("TRUNCATE dst.users CASCADE")
    _, result = run_migration(_config(copy_all=False, tables=["users"]))
    assert list(_statuses(result)) == ["users"]
    assert _count(conn, "users") == 3


def test_where_condition_filters_source_rows(conn):
    cfg = _config(
        copy_all=False,
        tables=[
            "users",
            {"name": "orders", "where": "created_at >= '2024-01-01'"},
        ],
    )
    _, result = run_migration(cfg)
    assert result.succeeded
    assert _count(conn, "orders") == 2
    assert _count(conn, "orders", "created_at < '2024-01-01'") == 0


def test_truncate_mode_clears_destination_first(conn):
    conn.execute("INSERT INTO dst.users (name) VALUES ('stale')")
    conn.execute("INSERT INTO dst.no_pk VALUES ('stale')")
    _, result = run_migration(_config(mode="truncate"))
    assert result.succeeded
    assert _count(conn, "users") == 3
    assert _count(conn, "users", "name = 'stale'") == 0
    assert _count(conn, "no_pk") == 2


def test_truncate_with_external_referencer_fails_without_cascade(conn):
    # order_items 는 복사 대상이 아닌데 orders 를 참조 -> TRUNCATE 불가
    cfg = _config(mode="truncate", exclude=["order_items"])
    with pytest.raises(MigrationError, match="order_items"):
        run_migration(cfg)
    # 아무것도 쓰이지 않아야 한다
    assert _count(conn, "users") == 0


def test_truncate_cascade_clears_external_referencer(conn):
    conn.execute("INSERT INTO dst.users (name) VALUES ('u')")
    conn.execute("INSERT INTO dst.products (sku, price) VALUES ('Z', 1)")
    conn.execute("INSERT INTO dst.orders (user_id, created_at) VALUES (1, '2020-01-01')")
    conn.execute("INSERT INTO dst.order_items VALUES (1, 1, 1)")
    cfg = _config(mode="truncate", exclude=["order_items"], truncate_cascade=True)
    events: list[Event] = []
    _, result = run_migration(cfg, on_event=events.append)
    assert result.succeeded
    assert _count(conn, "order_items") == 0
    assert any("CASCADE" in e.message for e in events if e.kind == "warning")


def test_upsert_updates_existing_rows_and_inserts_new(conn):
    conn.execute("INSERT INTO dst.users (id, name) VALUES (1, 'old-alice'), (99, 'keep-me')")
    conn.execute("INSERT INTO dst.products (id, sku, price) OVERRIDING SYSTEM VALUE VALUES (1, 'A', 999)")
    cfg = _config(mode="upsert", exclude=["no_pk"])
    _, result = run_migration(cfg)
    assert result.succeeded, _statuses(result)
    assert conn.execute("SELECT name FROM dst.users WHERE id = 1").fetchone()[0] == "alice"
    assert _count(conn, "users") == 4  # 3 복사 + keep-me
    assert conn.execute("SELECT price FROM dst.products WHERE id = 1").fetchone()[0] == 10
    # 두 번 실행해도 중복이 생기지 않는다
    _, result = run_migration(cfg)
    assert result.succeeded
    assert _count(conn, "order_items") == 4


def test_upsert_without_pk_fails_that_table_only(conn):
    _, result = run_migration(_config(mode="upsert"))
    statuses = _statuses(result)
    assert statuses["no_pk"] == "failed"
    assert statuses["users"] == "success"
    assert not result.succeeded
    failed = next(r for r in result.results if r.name == "no_pk")
    assert "PK" in failed.message


def test_per_table_mode_override(conn):
    conn.execute("INSERT INTO dst.no_pk VALUES ('stale')")
    cfg = _config(mode="upsert", tables=[{"name": "no_pk", "mode": "truncate"}])
    _, result = run_migration(cfg)
    assert result.succeeded
    assert _count(conn, "no_pk") == 2


def test_missing_dest_table_is_skipped_with_warning_in_copy_all(conn):
    conn.execute("DROP TABLE dst.no_pk")
    _, result = run_migration(_config())
    statuses = _statuses(result)
    assert statuses["no_pk"] == "skipped"
    assert statuses["users"] == "success"
    assert result.succeeded
    assert any("no_pk" in w for w in result.warnings)


def test_missing_dest_table_fails_when_explicit(conn):
    conn.execute("DROP TABLE dst.no_pk")
    _, result = run_migration(_config(copy_all=False, tables=["no_pk"]))
    assert _statuses(result)["no_pk"] == "failed"


def test_missing_source_table_aborts(conn):
    with pytest.raises(MigrationError, match="nope"):
        run_migration(_config(copy_all=False, tables=["nope"]))


def test_column_mismatch_fails_table_and_skips_dependents(conn):
    conn.execute("ALTER TABLE dst.users DROP COLUMN name")
    _, result = run_migration(_config())
    statuses = _statuses(result)
    assert statuses["users"] == "failed"
    assert statuses["orders"] == "skipped"
    assert statuses["order_items"] == "skipped"
    assert statuses["products"] == "success"
    assert statuses["no_pk"] == "success"
    skipped = next(r for r in result.results if r.name == "orders")
    assert "users" in skipped.message


def test_runtime_failure_isolated_per_table(conn):
    # 대상에 소스에는 없는 CHECK 제약을 추가해 복사 시 실패하게 만든다
    conn.execute("ALTER TABLE dst.orders ADD CONSTRAINT chk CHECK (created_at >= '2024-01-01')")
    _, result = run_migration(_config())
    statuses = _statuses(result)
    assert statuses["users"] == "success"
    assert statuses["orders"] == "failed"
    assert statuses["order_items"] == "skipped"
    assert statuses["products"] == "success"
    assert _count(conn, "orders") == 0  # 롤백되어 부분 데이터가 남지 않는다
    assert _count(conn, "users") == 3


def test_extra_dest_column_with_default_is_fine(conn):
    conn.execute("ALTER TABLE dst.users ADD COLUMN migrated_at timestamptz DEFAULT now()")
    _, result = run_migration(_config(copy_all=False, tables=["users"]))
    assert result.succeeded
    assert _count(conn, "users", "migrated_at IS NOT NULL") == 3


def test_manual_order_is_respected(conn):
    cfg = _config(copy_all=False, tables=["users", "products", "no_pk"], order=["no_pk", "products"])
    plan, _ = run_migration(cfg, dry_run=True)
    assert [t.name for t in plan.tables] == ["no_pk", "products", "users"]


def test_dry_run_writes_nothing_and_estimates(conn):
    conn.execute("ANALYZE src.users")
    cfg = _config(tables=[{"name": "orders", "where": "created_at >= '2024-01-01'"}])
    plan, result = run_migration(cfg, dry_run=True)
    assert result is None
    assert _count(conn, "users") == 0
    by_name = {t.name: t for t in plan.tables}
    assert by_name["orders"].estimated_rows == 2
    assert by_name["users"].estimated_rows == 3


def test_disable_triggers_option(conn):
    conn.execute(
        """
        CREATE TABLE dst.trigger_log (n int);
        CREATE FUNCTION dst.log_it() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO dst.trigger_log VALUES (1); RETURN NEW; END $$;
        CREATE TRIGGER t AFTER INSERT ON dst.users FOR EACH ROW EXECUTE FUNCTION dst.log_it();
        """
    )
    run_migration(_config(copy_all=False, tables=["users"]))
    assert _count(conn, "trigger_log") == 3
    conn.execute("TRUNCATE dst.users CASCADE; TRUNCATE dst.trigger_log")
    run_migration(_config(copy_all=False, tables=["users"], disable_triggers=True))
    assert _count(conn, "trigger_log") == 0
    assert _count(conn, "users") == 3


def test_where_subquery_resolves_unqualified_table_in_source_schema(conn):
    # search_path 가 소스 스키마로 고정되어 있어 where 절에서 스키마 없이 테이블을 쓸 수 있다
    cfg = _config(
        copy_all=False,
        tables=[
            "users",
            "products",
            {"name": "orders", "where": "created_at >= '2024-01-01'"},
            {"name": "order_items", "where": "order_id IN (SELECT id FROM orders WHERE created_at >= '2024-01-01')"},
        ],
    )
    _, result = run_migration(cfg)
    assert result.succeeded, _statuses(result)
    assert _count(conn, "order_items") == 3


def test_bad_where_marks_table_failed_in_dry_run(conn):
    cfg = _config(tables=[{"name": "orders", "where": "no_such_column = 1"}])
    plan, _ = run_migration(cfg, dry_run=True)
    orders = next(t for t in plan.tables if t.name == "orders")
    assert orders.precheck_status == "failed"
    assert "no_such_column" in orders.precheck_message


def test_progress_events_are_emitted(conn):
    conn.execute("INSERT INTO src.no_pk SELECT 'row' || g FROM generate_series(1, 5000) g")
    events: list[Event] = []
    cfg = _config(copy_all=False, tables=["no_pk", "users"], progress_interval=0.0001)
    _, result = run_migration(cfg, on_event=events.append)
    assert result.succeeded
    progress = [e for e in events if e.kind == "table_progress" and e.table == "no_pk" and e.rows is not None]
    assert progress, "진행 이벤트가 없음"
    assert progress[-1].rows <= 5002 and progress[-1].bytes > 0
    # 시퀀스가 있는 users 는 단계 메시지도 나온다
    assert any(e.kind == "table_progress" and e.table == "users" and "시퀀스" in e.message for e in events)


def test_upsert_emits_merge_phase(conn):
    events: list[Event] = []
    _, result = run_migration(_config(mode="upsert", copy_all=False, tables=["users"]), on_event=events.append)
    assert result.succeeded
    assert any(e.kind == "table_progress" and "병합" in e.message for e in events)


def test_progress_disabled_when_interval_zero(conn):
    conn.execute("INSERT INTO src.no_pk SELECT 'row' || g FROM generate_series(1, 5000) g")
    events: list[Event] = []
    run_migration(_config(copy_all=False, tables=["no_pk"], progress_interval=0), on_event=events.append)
    assert not [e for e in events if e.kind == "table_progress" and e.rows is not None]
