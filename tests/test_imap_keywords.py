from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_email_server.imap_keywords import ImapKeywordRegistry, ImapKeywordTag


def test_from_tags_builds_single_account_projection_with_safe_defaults() -> None:
    registry = ImapKeywordRegistry.from_tags((ImapKeywordTag(name="todo", keyword="$label4"),))

    assert registry.tags[0].model_dump() == {
        "name": "todo",
        "keyword": "$label4",
        "description": "",
        "writable": False,
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"name": "", "keyword": "$label4"}, "tag name must not be empty"),
        ({"name": "todo", "keyword": r"\Seen"}, "non-system IMAP keyword atom"),
        ({"name": "todo", "keyword": "not valid"}, "non-system IMAP keyword atom"),
        (
            {"name": "todo", "keyword": "$label4", "writable": "yes"},
            "valid boolean",
        ),
    ],
)
def test_tag_model_rejects_invalid_values(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ImapKeywordTag.model_validate(values)


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        (
            (
                ImapKeywordTag(name="Todo", keyword="$label4"),
                ImapKeywordTag(name="todo", keyword="$label5"),
            ),
            "tag names must be unique within an account, ignoring case",
        ),
        (
            (
                ImapKeywordTag(name="todo", keyword="$Label4"),
                ImapKeywordTag(name="important", keyword="$label4"),
            ),
            "tag keywords must be unique within an account, ignoring case",
        ),
    ],
)
def test_from_tags_enforces_case_insensitive_uniqueness(
    tags: tuple[ImapKeywordTag, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ImapKeywordRegistry.from_tags(tags)


def test_resolve_accepts_only_semantic_names() -> None:
    registry = ImapKeywordRegistry.from_tags((ImapKeywordTag(name="todo", keyword="$label4"),))

    assert registry.resolve(("TODO", "todo")) == ("$label4",)
    with pytest.raises(ValueError, match="Unknown configured email tag"):
        registry.resolve(("$label4",))


def test_resolve_enforces_writable_tags_when_requested() -> None:
    registry = ImapKeywordRegistry.from_tags((
        ImapKeywordTag(name="todo", keyword="$label4"),
        ImapKeywordTag(name="archive", keyword="$archive", writable=True),
    ))

    assert registry.resolve(("todo",)) == ("$label4",)
    assert registry.resolve(("archive",), require_writable=True) == ("$archive",)
    with pytest.raises(PermissionError, match="Email tag is not writable: todo"):
        registry.resolve(("todo",), require_writable=True)


def test_semantic_names_are_case_insensitive_and_ignore_unknown_keywords() -> None:
    registry = ImapKeywordRegistry.from_tags((
        ImapKeywordTag(name="todo", keyword="$label4"),
        ImapKeywordTag(name="important", keyword="$label1"),
    ))

    assert registry.semantic_names(["$LABEL1", "unknown", "$LABEL4"]) == [
        "todo",
        "important",
    ]
