"""Stable, explicit mail mutation grants (never infer future grants)."""

from collections.abc import Sequence
from typing import Literal, cast

MutationClass = Literal["draft", "organize", "delete", "send", "append"]
DEFAULT_ALLOWED_MUTATIONS: tuple[MutationClass, ...] = ("draft", "organize", "delete", "send", "append")


def validate_mutations(values: Sequence[str]) -> None:
    if len(values) != len(set(values)) or any(value not in DEFAULT_ALLOWED_MUTATIONS for value in values):
        raise ValueError("allowed_mutations must contain unique draft/organize/delete/send/append grants")


def require_mutation(values: tuple[MutationClass, ...], grant: MutationClass) -> None:
    if grant not in values:
        raise PermissionError(f"Mutation class '{grant}' is not allowed for this account")


def require_append_permissions(values: tuple[MutationClass, ...], flags: Sequence[str] | None) -> None:
    """Authorize general APPEND without narrowing historical full-grant flags."""
    require_mutation(values, "append")
    if flags is not None and any(flag.casefold() == r"\deleted" for flag in flags):
        require_mutation(values, "delete")


def parse_mutations(values: Sequence[str]) -> tuple[MutationClass, ...]:
    validate_mutations(values)
    return cast(tuple[MutationClass, ...], tuple(values))
