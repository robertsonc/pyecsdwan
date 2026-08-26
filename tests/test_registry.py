"""Unit tests for pyecsdwan.registry: plugin lookup and dependency ordering."""

import pytest

from pyecsdwan.contract import Ref, Resource
from pyecsdwan.registry import Registry, UnknownKind


class KindA(Resource):
    kind = "a"


class KindB(Resource):
    kind = "b"
    dependencies = ("a",)


class KindC(Resource):
    kind = "c"
    dependencies = ("b",)


@pytest.fixture
def registry():
    reg = Registry()
    reg.register(KindA())
    reg.register(KindB())
    reg.register(KindC())
    return reg


def test_register_and_get(registry):
    assert isinstance(registry.get("a"), KindA)
    assert "b" in registry
    assert "zzz" not in registry
    assert registry.kinds() == ["a", "b", "c"]


def test_unknown_kind_message_lists_known_kinds(registry):
    with pytest.raises(UnknownKind) as excinfo:
        registry.get("nope")
    message = str(excinfo.value)
    assert "nope" in message
    assert "a, b, c" in message
    assert excinfo.value.kind == "nope"


def test_duplicate_kind_raises(registry):
    with pytest.raises(ValueError, match="duplicate resource kind 'a'"):
        registry.register(KindA())


def test_order_refs_dependencies_first_deletes_last_reversed(registry):
    refs = [
        Ref(kind="c", name="c-up"),
        Ref(kind="a", name="a-up"),
        Ref(kind="b", name="b-del"),
        Ref(kind="b", name="b-up"),
        Ref(kind="a", name="a-del"),
        Ref(kind="c", name="c-del"),
    ]
    deletes = {"a:a-del", "b:b-del", "c:c-del"}
    ordered = registry.order_refs(refs, deletes)
    assert [r.kind for r in ordered] == ["a", "b", "c", "c", "b", "a"]
    # upserts first, in dependency order
    assert [r.name for r in ordered[:3]] == ["a-up", "b-up", "c-up"]
    # deletes last, in reverse dependency order (association before its target)
    assert [r.name for r in ordered[3:]] == ["c-del", "b-del", "a-del"]


def test_order_refs_cycle_raises_value_error():
    class KindX(Resource):
        kind = "x"
        dependencies = ("y",)

    class KindY(Resource):
        kind = "y"
        dependencies = ("x",)

    reg = Registry()
    reg.register(KindX())
    reg.register(KindY())
    refs = [Ref(kind="x", name="one"), Ref(kind="y", name="two")]
    with pytest.raises(ValueError, match="dependency cycle"):
        reg.order_refs(refs)
