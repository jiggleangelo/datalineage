import json
import re
from functools import lru_cache
from pathlib import Path

_ORDER = {"unknown": -1, "permissive": 0, "attribution": 1, "unlicensed": 2, "sharealike": 3, "restricted": 4, "noncommercial": 5}
_DATA = Path(__file__).resolve().parents[1] / "data" / "licences.json"
_GENERIC_ALIASES = {"other", "unknown", "custom"}


@lru_cache(maxsize=1)
def _table():
    value = json.loads(_DATA.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=1)
def _alias_patterns():
    return tuple(
        (alias, re.compile(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", re.I))
        for alias in sorted(_table(), key=len, reverse=True)
        if alias.lower() not in _GENERIC_ALIASES
    )


def normalise(raw, source="card_metadata"):
    from .resolver import LicenceInfo
    if isinstance(raw, (list, tuple)):
        values = [value for value in raw if isinstance(value, (str, int, float)) and str(value).strip()]
        if not values:
            return LicenceInfo(None, None, "unlicensed", "none")
        infos = [normalise(value, source) for value in values]
        selected = strictest(infos)
        if len(infos) == 1:
            return infos[0]
        return LicenceInfo(", ".join(str(value).strip() for value in values), selected.id, selected.category, source)
    if raw is None or not isinstance(raw, (str, int, float)) or not str(raw).strip():
        return LicenceInfo(raw if isinstance(raw, str) else None, None, "unlicensed", "none")
    value = str(raw).strip()
    item = _table().get(value.lower())
    if isinstance(item, dict) and item.get("category") in _ORDER:
        return LicenceInfo(value, item.get("id"), item["category"], source)
    if value.lower() in {"other", "custom"}:
        return LicenceInfo(value, "custom", "restricted", source)
    return LicenceInfo(value, None, "unknown", source)


def declared(data, source="card_metadata"):
    if not isinstance(data, dict):
        return normalise(None, source)
    raw = data.get("license")
    if raw is None:
        raw = data.get("licence")
    if isinstance(raw, str) and raw.strip().lower() == "other":
        name = data.get("license_name") or data.get("licence_name")
        link = data.get("license_link") or data.get("licence_link")
        details = [str(value).strip() for value in (name, link) if isinstance(value, str) and value.strip()]
        if details:
            from .resolver import LicenceInfo
            return LicenceInfo(" — ".join(details), "custom", "restricted", source)
    return normalise(raw, source)


def _negated(text, start):
    prefix = text[max(0, start - 160):start]
    return bool(re.search(
        r"(?:not|isn't|is not|without being|does not|doesn't|do not|don't|exclude(?:s|d)?|excluding|avoid(?:s|ed)?|not licensed under|do not restrict|does not restrict|no restriction on|not limited to)\s+(?:(?:use|content|data)\s+(?:to|as)\s+)?(?:restricted\s+to\s+)?(?:any\s+)?$",
        prefix,
        re.I,
    ))


def _licence_context(text, match):
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    if end < 0:
        end = len(text)
    line = text[start:end]
    return bool(re.search(r"\blicen[cs]e\b|\blicen[cs]ed\b|\bterms\b|\bSPDX\b|\breleased under\b|\bdistributed under\b|\busage restrictions?\b", line, re.I))


def detect(text):
    if not isinstance(text, str):
        return []
    text = text[:1_000_000]
    found = []
    identities = set()
    for alias, pattern in _alias_patterns():
        for match in pattern.finditer(text):
            if _negated(text, match.start()) or not _licence_context(text, match):
                continue
            info = normalise(alias, "card_text")
            identity = (info.id, info.category)
            if identity not in identities:
                found.append(info)
                identities.add(identity)
            break
    from .resolver import LicenceInfo
    phrase_patterns = (
        (re.compile(r"\bnon-commercial\b", re.I), "custom", "noncommercial"),
        (re.compile(r"\bnot for commercial use\b", re.I), "custom", "noncommercial"),
        (re.compile(r"\bresearch purposes only\b", re.I), "custom", "restricted"),
        (re.compile(r"\bshare alike\b", re.I), "custom", "sharealike"),
    )
    phrase_count = sum(bool(pattern.search(text)) for pattern, _, _ in phrase_patterns)
    for pattern, ident, category in phrase_patterns:
        for match in pattern.finditer(text):
            if _negated(text, match.start()) or (not _licence_context(text, match) and phrase_count < 2):
                continue
            identity = (ident, category)
            if identity not in identities:
                found.append(LicenceInfo(match.group(0), ident, category, "card_text"))
                identities.add(identity)
            break
    return found


def strictest(items):
    values = [item for item in items if item is not None]
    known = [item for item in values if item.category != "unknown"]
    candidates = known or values
    return max(candidates, key=lambda item: _ORDER.get(item.category, -1)) if candidates else None


def more_permissive(left, right):
    if left == "unknown" or right == "unknown":
        return False
    if left not in _ORDER or right not in _ORDER:
        return False
    return _ORDER[left] < _ORDER[right]
