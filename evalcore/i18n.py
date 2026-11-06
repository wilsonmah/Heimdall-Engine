"""English / French text selection.

Any human-facing string in a template may be either a plain string or a
localized object: {"en": "...", "fr": "..."}. Callers pick the language from
the HTTP Accept-Language header (or ?lang=), defaulting to English.
"""
SUPPORTED = ("en", "fr")
DEFAULT = "en"


def pick_lang(accept_language=None, override=None):
    if override in SUPPORTED:
        return override
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in SUPPORTED:
            return code
    return DEFAULT


def text(value, lang=DEFAULT):
    if isinstance(value, dict):
        return value.get(lang) or value.get(DEFAULT) or next(iter(value.values()), "")
    return value


def has_lang(value, lang):
    """True if `value` provides `lang` explicitly (plain strings do not)."""
    return isinstance(value, dict) and bool(value.get(lang))
