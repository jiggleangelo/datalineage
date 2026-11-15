import hashlib
import re
from functools import lru_cache

_SEGMENT = r"[A-Za-z0-9][\w.-]{0,95}"
_ID = _SEGMENT + r"(?:/[\w.-]{1,96})?"
_ID_RE = re.compile(r"^" + _ID + r"$")
_HF_LINK_RE = re.compile(r"https?://huggingface\.co/datasets/(" + _ID + r")(?![\w./-])")
_ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arXiv:\s*)(\d{4}\.\d{4,5})", re.I)
_FRONT_START_RE = re.compile(r"\A---[ \t]*\r?\n")
_FRONT_END_RE = re.compile(r"^---[ \t]*$", re.M)
_MAX_CARD_SIZE = 1_000_000
_MAX_ARXIV_LOOKUPS = 3


@lru_cache(maxsize=1024)
def _alias_pattern(alias):
    return re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.I)


def _valid_target(value):
    if not isinstance(value, str):
        return False
    if value.startswith("hf:"):
        return bool(_ID_RE.fullmatch(value[3:]))
    return bool(re.fullmatch(r"(?:ext|unresolved):[A-Za-z0-9][\w.-]{0,127}", value))


def _strip_comment(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _split_inline(value):
    inner = value[1:-1].strip()
    if not inner:
        return []
    parts = []
    quote = None
    start = 0
    for index, char in enumerate(inner):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ",":
            parts.append(_clean(inner[start:index]))
            start = index + 1
    parts.append(_clean(inner[start:]))
    return [part for part in parts if part != ""]


def parse_front_matter(text):
    if not isinstance(text, str):
        return {}
    text = text[:_MAX_CARD_SIZE]
    start = _FRONT_START_RE.match(text)
    if not start:
        return {}
    end = _FRONT_END_RE.search(text, start.end())
    if not end:
        return {}
    result, current = {}, None
    for line in text[start.end():end.start()].splitlines():
        line = _strip_comment(line)
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line) and current:
            existing = result.get(current)
            if not isinstance(existing, list):
                existing = []
                result[current] = existing
            existing.append(_clean(re.sub(r"^\s*-\s+", "", line)))
        elif re.match(r"^[\w-]+\s*:", line):
            key, value = line.split(":", 1)
            current = key.strip()
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                result[current] = _split_inline(value)
            else:
                result[current] = _clean(value) if value else []
    return result


def _clean(value):
    value = _strip_comment(value.strip())
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _source_target(value):
    value = str(value).strip()
    if value.lower().startswith("extended|"):
        value = value.split("|", 1)[1].strip()
    return value if _ID_RE.fullmatch(value) else None


def _unresolved_source(value):
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]
    return "unresolved:source-" + digest


def upstream(card_text, card_data, aliases, paper_client=None, limits=None):
    card_text = card_text[:_MAX_CARD_SIZE] if isinstance(card_text, str) else ""
    card_data = card_data if isinstance(card_data, dict) else {}
    aliases = aliases if isinstance(aliases, dict) else {}
    hits = []
    present = "source_datasets" in card_data
    sources = card_data.get("source_datasets", [])
    if isinstance(sources, str):
        sources = [sources]
    malformed_sources = False
    if isinstance(sources, list):
        for value in sources:
            raw = str(value).strip()
            if raw.lower() == "original":
                continue
            target = _source_target(raw)
            if target:
                hits.append(("hf:" + target, "source_datasets", "high"))
            elif raw:
                malformed_sources = True
                hits.append((_unresolved_source(raw), "invalid_source_datasets", "low"))
    elif present:
        malformed_sources = True
        hits.append((_unresolved_source(sources), "invalid_source_datasets", "low"))
    if present and malformed_sources and isinstance(limits, list) and "INVALID_SOURCE_DATASETS" not in limits:
        limits.append("INVALID_SOURCE_DATASETS")
    for value in _HF_LINK_RE.findall(card_text):
        hits.append(("hf:" + value, "card_link", "medium"))
    for alias, target in aliases.items():
        target_id = target.get("id") if isinstance(target, dict) else target
        if isinstance(alias, str) and _valid_target(target_id) and _alias_pattern(alias).search(card_text):
            hits.append((target_id, "alias_mention", "low"))
    if paper_client:
        paper_ids = list(dict.fromkeys(_ARXIV_RE.findall(card_text)))
        if len(paper_ids) > _MAX_ARXIV_LOOKUPS and isinstance(limits, list) and "ARXIV_LIMIT" not in limits:
            limits.append("ARXIV_LIMIT")
        for arxiv_id in paper_ids[:_MAX_ARXIV_LOOKUPS]:
            abstract = (paper_client.get_abstract(arxiv_id) or "")[:_MAX_CARD_SIZE]
            for alias, target in aliases.items():
                target_id = target.get("id") if isinstance(target, dict) else target
                if isinstance(alias, str) and _valid_target(target_id) and _alias_pattern(alias).search(abstract):
                    hits.append((target_id, "paper_abstract", "low"))
    seen = set()
    return [item for item in hits if not (item in seen or seen.add(item))]
