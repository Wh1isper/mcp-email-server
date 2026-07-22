"""Structural regressions for placement-scoped IMAP expunge behavior."""

from __future__ import annotations

import ast
from pathlib import Path

_PRODUCT_ROOT = Path(__file__).resolve().parents[1] / "mcp_email_server"
_SCOPED_UID_EXPUNGE_MODULES = frozenset({Path("emails/classic.py")})


def _calls(tree: ast.AST) -> tuple[ast.Call, ...]:
    return tuple(node for node in ast.walk(tree) if isinstance(node, ast.Call))


def _literal_string(argument: ast.expr) -> str | None:
    return argument.value if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else None


def test_raw_imap_boundaries_never_call_mailbox_wide_expunge():
    direct_calls: list[str] = []
    for module in sorted(_PRODUCT_ROOT.rglob("*.py")):
        relative_module = module.relative_to(_PRODUCT_ROOT)
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for call in _calls(tree):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "expunge":
                direct_calls.append(str(relative_module))

    assert direct_calls == []


def test_product_uid_expunge_calls_remain_explicitly_scoped_and_reviewed():
    observed_modules: set[Path] = set()
    for module in sorted(_PRODUCT_ROOT.rglob("*.py")):
        relative_module = module.relative_to(_PRODUCT_ROOT)
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for call in _calls(tree):
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "uid" or not call.args:
                continue
            command = _literal_string(call.args[0])
            if command is None or command.casefold() != "expunge":
                continue
            assert command == "expunge"
            assert relative_module in _SCOPED_UID_EXPUNGE_MODULES
            assert len(call.args) >= 2
            observed_modules.add(relative_module)

    assert observed_modules == _SCOPED_UID_EXPUNGE_MODULES
