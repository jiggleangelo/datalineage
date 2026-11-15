import json
from pathlib import Path

UNKNOWN = "UNKNOWN — provider must supply"
_MAX_MANIFEST_FIELD = 500
_DEFAULT_SECTIONS = [
    {"id": "1", "title": "General information", "fields": ["provider", "model_name", "base_model", "modalities", "training_data_size"]},
    {"id": "2.1", "title": "Publicly available datasets", "fields": ["datasets"]},
    {"id": "2.2", "title": "Private third-party datasets", "fields": ["details"]},
    {"id": "2.3", "title": "Crawled or scraped data", "fields": ["crawler", "period", "domains"]},
    {"id": "2.4-2.6", "title": "User, synthetic and other data", "fields": ["details"]},
    {"id": "3", "title": "Data processing", "fields": ["rights_reservation_compliance", "illegal_content_removal"]},
]


def _field(value=None, basis="provider must supply", status=None):
    present = value not in (None, "")
    return {
        "value": value if present else UNKNOWN,
        "status": (status or "inferred") if present else "unknown",
        "basis": basis if present else "provider must supply",
    }


def _model(manifest):
    if manifest is None:
        return {}
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    model = manifest.get("model", {})
    if model is None:
        return {}
    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    output = {}
    for key in ("provider", "name", "base_model"):
        value = model.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > _MAX_MANIFEST_FIELD:
                raise ValueError("invalid model field")
            output[key] = value.strip()
    return output


def _section_definitions(spec):
    supplied = spec.get("sections") if isinstance(spec, dict) else None
    supplied = supplied if isinstance(supplied, list) else []
    by_id = {str(item.get("id")): item for item in supplied if isinstance(item, dict)}
    output = []
    for default in _DEFAULT_SECTIONS:
        output.append(by_id.pop(default["id"], default))
    output.extend(by_id.values())
    return output


def _field_names(section):
    fields = section.get("fields", []) if isinstance(section, dict) else []
    if isinstance(fields, dict):
        return list(fields)
    if isinstance(fields, list):
        return [item.get("id") if isinstance(item, dict) else item for item in fields]
    return []


def _load_spec(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {
        "template_version": "unavailable",
        "source_url": None,
        "verified_against_official_text": False,
        "sections": _DEFAULT_SECTIONS,
    }


def build_draft(report, manifest=None, template_path=None):
    path = Path(template_path) if template_path else Path(__file__).resolve().parents[1] / "data/commission_template.json"
    spec = _load_spec(path)
    model = _model(manifest)
    public = []
    for node in report.nodes.values():
        if node.id.startswith("hf:"):
            public.append({
                "name": _field(node.title, node.id, "inferred"),
                "link": _field(node.url, node.id, "inferred"),
                "licence": _field(node.declared.id or node.declared.raw, node.declared.source, "inferred"),
            })
    common_node = report.nodes.get("ext:common-crawl")
    common = "Common Crawl–derived source inferred from resolved graph" if common_node else None
    values = {
        "provider": _field(model.get("provider"), "manifest", "provided"),
        "model_name": _field(model.get("name"), "manifest", "provided"),
        "base_model": _field(model.get("base_model"), "manifest", "provided"),
        "modalities": _field("text", "resolved graph", "inferred"),
        "datasets": {"value": public, "status": "inferred", "basis": "resolved graph"},
        "crawler": _field(common, "resolved graph" if common else "provider must supply", "inferred" if common else None),
    }
    sections = []
    for definition in _section_definitions(spec):
        fields = {name: values.get(name, _field()) for name in _field_names(definition) if isinstance(name, str)}
        sections.append({"id": str(definition.get("id", "")), "title": str(definition.get("title", "")), "fields": fields})
    banner = None if spec.get("verified_against_official_text") else "DRAFT — template structure not yet verified against official Commission text."
    return {"template_version": spec.get("template_version", "unknown"), "source_url": spec.get("source_url"), "verified_against_official_text": bool(spec.get("verified_against_official_text", False)), "banner": banner, "sections": sections}
