"""Unit tests for pyecsdwan.candidate: the on-disk candidate changeset store."""

from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ref

HOST = "orch.example.com"
REF = Ref(kind="bio", name="corp")


def test_set_path_builds_nested_intent_and_persists(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.set_path(REF, ["topology", "hubs", "primary"], "edge1")
    item = store.items[REF.key()]
    assert item.mode == "merge"
    assert item.intent == {"topology": {"hubs": {"primary": "edge1"}}}

    # A second store handle over the same root sees the persisted state.
    reloaded = CandidateStore(HOST, root=tmp_path)
    assert len(reloaded) == 1
    again = reloaded.items[REF.key()]
    assert again.mode == "merge"
    assert again.intent == {"topology": {"hubs": {"primary": "edge1"}}}
    assert again.ref == REF


def test_desired_for_merges_intent_over_current(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.set_path(REF, ["a", "b"], 9)
    current = {"a": {"b": 1, "keep": 2}, "top": 3}
    desired = store.desired_for(store.items[REF.key()], current)
    assert desired == {"a": {"b": 9, "keep": 2}, "top": 3}
    # merge never mutates the server-side canonical state
    assert current == {"a": {"b": 1, "keep": 2}, "top": 3}


def test_delete_whole_resource_yields_none(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.set_path(REF, ["a"], 1)
    store.delete(REF)
    item = store.items[REF.key()]
    assert item.mode == "delete"
    assert store.desired_for(item, {"a": 1, "b": 2}) is None


def test_delete_subtree_prunes_desired(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.delete(REF, ["a", "b"])
    item = store.items[REF.key()]
    assert item.mode == "merge"
    desired = store.desired_for(item, {"a": {"b": 1, "c": 2}, "top": 3})
    assert desired == {"a": {"c": 2}, "top": 3}


def test_set_after_delete_resurrects_as_replace(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.delete(REF)
    store.set_path(REF, ["name"], "corp-v2")
    item = store.items[REF.key()]
    assert item.mode == "replace"
    assert item.intent == {"name": "corp-v2"}
    # replace ignores current state entirely
    assert store.desired_for(item, {"name": "old", "extra": 1}) == {"name": "corp-v2"}


def test_later_set_on_same_path_cancels_delete(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.delete(REF, ["a", "b"])
    store.set_path(REF, ["a", "b"], 7)
    item = store.items[REF.key()]
    assert item.delete_paths == []
    assert store.desired_for(item, {"a": {"b": 1}}) == {"a": {"b": 7}}


def test_clear_empties_store_and_disk(tmp_path):
    store = CandidateStore(HOST, root=tmp_path)
    store.set_path(REF, ["x"], 1)
    store.set_path(Ref(kind="bio", name="other"), ["y"], 2)
    assert len(store) == 2
    store.clear()
    assert len(store) == 0
    assert len(CandidateStore(HOST, root=tmp_path)) == 0
