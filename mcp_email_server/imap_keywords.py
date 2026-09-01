from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp_email_server.application.limits import APPLICATION_LIMITS, validate_controlled_string


def _is_imap_keyword(value: str) -> bool:
    """Return whether a value is one non-system IMAP keyword atom."""
    atom_specials = frozenset('(){%*]\\"')
    return bool(value) and all(0x21 <= ord(character) <= 0x7E and character not in atom_specials for character in value)


class ImapKeywordTag(BaseModel):
    """One semantic tag name mapped to one provider keyword."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    keyword: str
    description: str = ""
    writable: bool = Field(default=False, strict=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_controlled_string(
            value,
            field_name="tag name",
            maximum_bytes=APPLICATION_LIMITS.flag_bytes,
        )

    @field_validator("keyword")
    @classmethod
    def _validate_keyword(cls, value: str) -> str:
        validate_controlled_string(
            value,
            field_name="tag keyword",
            maximum_bytes=APPLICATION_LIMITS.flag_bytes,
        )
        if value.startswith("\\") or not _is_imap_keyword(value):
            raise ValueError("tag keyword must be a non-system IMAP keyword atom")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return validate_controlled_string(
            value,
            field_name="tag description",
            maximum_bytes=APPLICATION_LIMITS.account_description_bytes,
            allow_empty=True,
        )


class ImapKeywordAccount(BaseModel):
    """Bounded semantic tag configuration for one email account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tags: tuple[ImapKeywordTag, ...] = Field(default=(), max_length=APPLICATION_LIMITS.flags)

    @model_validator(mode="after")
    def _unique_mappings(self) -> ImapKeywordAccount:
        names = [tag.name.casefold() for tag in self.tags]
        keywords = [tag.keyword.casefold() for tag in self.tags]
        if len(names) != len(set(names)):
            raise ValueError("tag names must be unique within an account, ignoring case")
        if len(keywords) != len(set(keywords)):
            raise ValueError("tag keywords must be unique within an account, ignoring case")
        return self


@dataclass(frozen=True)
class ImapKeywordRegistry:
    """Immutable projection of one selected account's semantic tags."""

    tags: tuple[ImapKeywordTag, ...] = ()

    @classmethod
    def from_tags(cls, tags: tuple[ImapKeywordTag, ...] | list[ImapKeywordTag]) -> ImapKeywordRegistry:
        validated = ImapKeywordAccount(tags=tuple(tags))
        return cls(tags=validated.tags)

    def resolve(self, values: tuple[str, ...], *, require_writable: bool = False) -> tuple[str, ...]:
        """Resolve semantic names to provider keywords.

        Provider keywords are deliberately not accepted as public mutation or
        filter input: configured semantic names are the stable cross-provider
        interface.
        """
        by_name = {tag.name.casefold(): tag for tag in self.tags}
        resolved: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = by_name.get(value.casefold())
            if tag is None:
                raise ValueError(f"Unknown configured email tag: {value}")
            if require_writable and not tag.writable:
                raise PermissionError(f"Email tag is not writable: {value}")
            normalized = tag.keyword.casefold()
            if normalized not in seen:
                seen.add(normalized)
                resolved.append(tag.keyword)
        return tuple(resolved)

    def semantic_names(self, keywords: list[str] | tuple[str, ...]) -> list[str]:
        keyword_set = {keyword.casefold() for keyword in keywords}
        return [tag.name for tag in self.tags if tag.keyword.casefold() in keyword_set]
