"""Unit tests for pyecsdwan.diffing: structural diff and Junos-style rendering."""

from pyecsdwan.contract import Diff, DiffEntry, DiffOp, Ref
from pyecsdwan.diffing import render_diff_lines, structural_diff

REF = Ref(kind="interface-labels", name="global")


def test_equal_dicts_produce_empty_diff():
    state = {"a": 1, "b": {"c": [1, 2, {"d": True}]}, "e": None}
    other = {"a": 1, "b": {"c": [1, 2, {"d": True}]}, "e": None}
    assert structural_diff(state, other) == []


def test_none_to_dict_is_single_add_at_root():
    entries = structural_diff(None, {"a": 1})
    assert entries == [DiffEntry(op=DiffOp.ADD, path=(), new={"a": 1})]


def test_dict_to_none_is_single_remove_at_root():
    entries = structural_diff({"a": 1}, None)
    assert entries == [DiffEntry(op=DiffOp.REMOVE, path=(), old={"a": 1})]


def test_nested_replace_has_full_path():
    current = {"wan": {"1": {"name": "MPLS", "topology": 0}}}
    desired = {"wan": {"1": {"name": "MPLS", "topology": 2}}}
    entries = structural_diff(current, desired)
    assert entries == [
        DiffEntry(op=DiffOp.REPLACE, path=("wan", "1", "topology"), old=0, new=2)
    ]


def test_list_growth_adds_positional_entry():
    entries = structural_diff({"l": [1, 2]}, {"l": [1, 2, 3]})
    assert entries == [DiffEntry(op=DiffOp.ADD, path=("l", "2"), new=3)]


def test_list_shrink_removes_positional_entry():
    entries = structural_diff({"l": [1, 2]}, {"l": [1]})
    assert entries == [DiffEntry(op=DiffOp.REMOVE, path=("l", "1"), old=2)]


def test_scalar_type_change_is_replace():
    entries = structural_diff({"v": 1}, {"v": "1"})
    assert entries == [DiffEntry(op=DiffOp.REPLACE, path=("v",), old=1, new="1")]


def test_render_markers_and_replace_pair():
    entries = [
        DiffEntry(op=DiffOp.ADD, path=("wan", "9"), new={"name": "LTE"}),
        DiffEntry(op=DiffOp.REMOVE, path=("lan", "3"), old={"name": "old"}),
        DiffEntry(op=DiffOp.REPLACE, path=("v",), old=1, new="1"),
    ]
    lines = render_diff_lines(Diff(ref=REF, entries=entries))
    # replace renders as a '-' line followed by a '+' line (Junos flavor)
    assert [marker for marker, _ in lines] == ["+", "-", "-", "+"]
    assert lines[0] == ("+", 'wan.9: {"name":"LTE"}')
    assert lines[1] == ("-", 'lan.3: {"name":"old"}')
    assert lines[2] == ("-", "v: 1")
    assert lines[3] == ("+", "v: '1'")


def test_render_root_path_label():
    entries = structural_diff(None, {"a": 1})
    lines = render_diff_lines(Diff(ref=REF, entries=entries))
    assert lines == [("+", '(root): {"a":1}')]
