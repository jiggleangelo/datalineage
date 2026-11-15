import json
import uuid
from dataclasses import asdict
from .pdf import make_pdf

_SPDX_IDS = {
    "0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0-1.0",
    "CC-BY-1.0", "CC-BY-2.0", "CC-BY-2.5", "CC-BY-3.0", "CC-BY-4.0",
    "CC-BY-NC-1.0", "CC-BY-NC-2.0", "CC-BY-NC-2.5", "CC-BY-NC-3.0", "CC-BY-NC-4.0",
    "CC-BY-NC-SA-1.0", "CC-BY-NC-SA-2.0", "CC-BY-NC-SA-2.5", "CC-BY-NC-SA-3.0", "CC-BY-NC-SA-4.0",
    "CC-BY-SA-1.0", "CC-BY-SA-2.0", "CC-BY-SA-2.5", "CC-BY-SA-3.0", "CC-BY-SA-4.0",
    "CDLA-Permissive-1.0", "CDLA-Permissive-2.0", "CDLA-Sharing-1.0", "ISC", "MIT", "MPL-2.0", "ODbL-1.0", "PDDL-1.0", "Unlicense"
}


def _spdx(value):
    return isinstance(value, str) and value in _SPDX_IDS


def json_export(report, draft):
    value = asdict(report)
    value.pop("roles", None)
    for edge in value.get("edges", []):
        if isinstance(edge, dict):
            edge.pop("traversed", None)
    value.update({"schema": "datalineage.report/0.1", "template_draft": draft})
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def cyclonedx(report):
    components = []
    for node in report.nodes.values():
        component = {
            "type": "data",
            "bom-ref": node.id,
            "name": node.title,
            "data": [{"type": "dataset", "name": node.title}],
        }
        licence = node.declared
        if _spdx(licence.id):
            component["licenses"] = [{"license": {"id": licence.id}}]
        elif licence.raw or licence.id:
            component["licenses"] = [{"license": {"name": str(licence.raw or licence.id)[:1024]}}]
        properties = [{"name": "datalineage:flag", "value": flag.code} for flag in report.flags if flag.node == node.id]
        if properties:
            component["properties"] = properties
        components.append(component)
    node_ids = set(report.nodes)
    dependencies = []
    for ident in report.nodes:
        children = []
        for edge in report.edges:
            if edge.parent == ident and edge.traversed and edge.child in node_ids and edge.child not in children:
                children.append(edge.child)
        dependencies.append({"ref": ident, "dependsOn": children})
    value = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:" + str(uuid.UUID(report.report_id)),
        "version": 1,
        "metadata": {
            "tools": {"components": [{"type": "application", "name": "DataLineage", "version": report.tool_version}]},
            "properties": [
                {"name": "datalineage:disclaimer", "value": report.disclaimer},
                {"name": "datalineage:coverage", "value": str(report.coverage["pct"])},
            ],
        },
        "components": components,
        "dependencies": dependencies,
    }
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def pdf_export(report, draft):
    lines = ["DataLineage provenance report", report.disclaimer, "", f"Coverage: {report.coverage['pct']}%", f"Flags: {len(report.flags)}", "", "Flags"]
    lines += [f"[{flag.severity}] {flag.code}: {flag.message} | {' -> '.join(flag.path)}" for flag in report.flags]
    lines += ["", "Nodes"] + [f"{node.id} | {node.modality} | {node.declared.id or node.declared.raw or 'unknown'}" for node in report.nodes.values()]
    lines += ["", "Commission template draft", draft.get("banner") or "Verified template structure"]
    for section in draft.get("sections", []):
        lines.append(str(section.get("id", "")) + " " + str(section.get("title", "")))
        for name, field in section.get("fields", {}).items():
            value = field.get("value")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"  {name}: {value} [{field.get('status')}; {field.get('basis')}]")
    return make_pdf(lines)
