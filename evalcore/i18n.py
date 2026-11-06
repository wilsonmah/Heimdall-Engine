"""Canadian English / French text selection.

Any human-facing string in a template may be a plain string or a localized
object. Keys may be regional ("en-CA", "fr-CA") or bare ("en", "fr"):

    {"en-CA": "Network security", "fr-CA": "Sécurité du réseau"}

Callers pick the locale from Accept-Language (or ?lang=). Lookup falls back
from the exact tag to the bare language, then to English.
"""
SUPPORTED = ("en-CA", "fr-CA")
DEFAULT = "en-CA"
_BY_BASE = {"en": "en-CA", "fr": "fr-CA"}


def normalize(tag):
    """'fr', 'fr-ca', 'fr_FR' -> 'fr-CA'; unknown -> None."""
    if not tag:
        return None
    return _BY_BASE.get(tag.replace("_", "-").split("-")[0].strip().lower())


def pick_lang(accept_language=None, override=None):
    if normalize(override):
        return normalize(override)
    for part in (accept_language or "").split(","):
        loc = normalize(part.split(";")[0])
        if loc:
            return loc
    return DEFAULT


def _candidates(lang):
    loc = normalize(lang) or DEFAULT
    base = loc.split("-")[0]
    seen = []
    for k in (lang, loc, base, DEFAULT, "en"):
        if k and k not in seen:
            seen.append(k)
    return seen


def text(value, lang=DEFAULT):
    if isinstance(value, dict):
        for k in _candidates(lang):
            if value.get(k):
                return value[k]
        return next(iter(value.values()), "")
    return value


def has_lang(value, lang):
    """True if `value` explicitly provides `lang` (either regional or bare key)."""
    if not isinstance(value, dict):
        return False
    base = (normalize(lang) or lang).split("-")[0]
    return any(v and k.replace("_", "-").split("-")[0].lower() == base for k, v in value.items())
