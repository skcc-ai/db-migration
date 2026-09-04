"""위상 정렬 단위 테스트."""

import pytest

from db_migration.ordering import OrderingError, resolve_order


def test_fk_chain_orders_parents_first():
    tables = {"order_items", "orders", "users", "products"}
    deps = [("orders", "users"), ("order_items", "orders"), ("order_items", "products")]
    order = resolve_order(tables, deps)
    assert order.index("users") < order.index("orders") < order.index("order_items")
    assert order.index("products") < order.index("order_items")


def test_no_deps_is_alphabetical():
    assert resolve_order({"c", "a", "b"}, []) == ["a", "b", "c"]


def test_deps_outside_selection_ignored():
    order = resolve_order({"orders"}, [("orders", "users")])
    assert order == ["orders"]


def test_cycle_raises_with_table_names():
    with pytest.raises(OrderingError) as exc:
        resolve_order({"a", "b", "c"}, [("a", "b"), ("b", "a")])
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_manual_order_breaks_cycle():
    order = resolve_order({"a", "b", "c"}, [("a", "b"), ("b", "a"), ("c", "a")], manual_order=("b", "a"))
    assert order == ["b", "a", "c"]


def test_manual_order_comes_first_and_ignores_unknown():
    order = resolve_order({"x", "y", "z"}, [("z", "y")], manual_order=("z", "nope"))
    assert order == ["z", "x", "y"]
