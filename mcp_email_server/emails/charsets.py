"""Decode legacy Chinese charset labels without regressing text that already decodes.

Mail labelled ``gb2312`` routinely contains GBK-only characters (for example
``镕`` or ``啰``), ``gbk`` mail occasionally contains GB18030 four-byte
characters, and ``big5`` mail contains HKSCS characters. Python decodes each
label strictly, so one such character makes a whole MIME part or RFC 2047
encoded word undecodable and the parser falls back to replacement characters.

Two mechanisms, chosen per family so that nothing that decodes today changes:

* GB2312 labels resolve to GB18030, as the WHATWG Encoding Standard maps them.
  Every two-byte GB2312 sequence decodes identically except A1A4 and A1AA,
  where GB18030 yields the WHATWG code points (U+00B7, U+2014). The label alias
  is what lets Python's header parser decode encoded words, so this is process
  wide; the ``gbk`` codec and the Windows code page names that resolve to it
  (``cp936`` and friends, and the ``gbk`` encoding Python reports for piped
  standard streams) are left strict.
* For GBK and Big5, body decoding tries the declared codec first and the
  superset (GB18030, Big5-HKSCS) only when strict decoding fails. Big5-HKSCS is
  not a superset of Python's ``big5`` (249 sequences in C6A1-C7FC differ), so a
  label alias would regress kana and Cyrillic text; encoded words in headers
  therefore keep strict Big5 decoding.
"""

from __future__ import annotations

import codecs
import encodings
import encodings.aliases

_GB18030 = "gb18030"

# https://encoding.spec.whatwg.org/#names-and-labels: the GBK labels that Python
# otherwise resolves to its strict gb2312 codec.
_WHATWG_GB2312_LABELS = (
    "chinese",
    "csgb2312",
    "csiso58gb231280",
    "gb2312",
    "gb_2312",
    "gb_2312-80",
    "iso-ir-58",
)
# WHATWG labels that Python does not know at all; they gain the strict codec.
_WHATWG_UNKNOWN_LABELS = {"x-gbk": "gbk", "cn-big5": "big5", "x-x-big5": "big5"}

# Tried only after the declared codec has rejected the bytes.
_SUPERSET_BY_CODEC = {"gbk": _GB18030, "big5": "big5hkscs"}


def _normalized(label: str) -> str:
    return encodings.normalize_encoding(label.lower())


def _gb2312_aliases() -> dict[str, str]:
    aliases = {alias: _GB18030 for alias, target in encodings.aliases.aliases.items() if target == "gb2312"}
    aliases.update({_normalized(label): _GB18030 for label in _WHATWG_GB2312_LABELS})
    return aliases


def _clear_lookup_caches() -> None:
    # encodings.search_function memoizes resolved codecs in a module-level dict
    # that typeshed does not declare, and the codec registry caches lookups on top
    # of it; unregistering any search function clears the registry (3.10+).
    cache = vars(encodings).get("_cache")
    if isinstance(cache, dict):
        cache.clear()

    def _probe(_name: str) -> None:
        return None

    codecs.register(_probe)
    codecs.unregister(_probe)


def install_gb2312_superset_aliases() -> None:
    """Resolve GB2312 labels to GB18030 for every decoder in the process. Idempotent."""
    aliases = encodings.aliases.aliases
    aliases.update(_gb2312_aliases())
    for label, codec in _WHATWG_UNKNOWN_LABELS.items():
        aliases.setdefault(_normalized(label), codec)
    _clear_lookup_caches()


def decode_text(payload: bytes, label: str) -> str:
    """Decode with the declared label, then its CJK superset, then UTF-8 with replacement.

    The superset is keyed on the resolved codec name, so ``gbk``/``cp936`` fall
    back to GB18030 and ``big5`` to Big5-HKSCS, while ``cp950`` stays strict.
    """
    try:
        return payload.decode(label)
    except LookupError:
        return payload.decode("utf-8", errors="replace")
    except UnicodeError:
        pass
    superset = _SUPERSET_BY_CODEC.get(codecs.lookup(label).name)
    if superset is not None:
        try:
            return payload.decode(superset)
        except UnicodeError:
            pass
    return payload.decode("utf-8", errors="replace")
