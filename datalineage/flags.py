from collections import deque
from .licences import more_permissive, strictest
from .resolver import Flag


def _paths_from(report, root):
    graph = {}
    for edge in report.edges:
        if edge.traversed and edge.child in report.nodes:
            graph.setdefault(edge.parent, []).append(edge)
    output = {}
    queue = deque([(root, [root], False)])
    while queue:
        node, path, low = queue.popleft()
        score = (1 if low else 0, len(path))
        previous = output.get(node)
        if previous and previous[2] <= score:
            continue
        output[node] = (path, low, score)
        for edge in graph.get(node, []):
            if edge.child not in path:
                queue.append((edge.child, path + [edge.child], low or edge.confidence == "low"))
    return {node: (value[0], value[1]) for node, value in output.items()}


def build_flags(report, litigation=()):
    paths_by_root = {root: _paths_from(report, root) for root in report.roots}
    best_paths = {}
    for paths in paths_by_root.values():
        for node, value in paths.items():
            score = (1 if value[1] else 0, len(value[0]))
            old = best_paths.get(node)
            if old is None or score < (1 if old[1] else 0, len(old[0])):
                best_paths[node] = value
    flags, emitted = [], set()

    def add(code, severity, node, fact, path=None, root=None):
        key = (code, node, root)
        if key in emitted:
            return
        emitted.add(key)
        selected = path or best_paths.get(node, ([node], False))[0]
        flags.append(Flag(code, severity, node, fact + "; review recommended", selected))

    entries = litigation if isinstance(litigation, (list, tuple)) else ()
    for ident, node in report.nodes.items():
        effective = strictest([node.declared] + node.detected)
        low = best_paths.get(ident, ([], False))[1]
        if effective and effective.category == "noncommercial" and ident not in report.roots:
            add("NC_UPSTREAM_POSSIBLE" if low else "NC_UPSTREAM", "medium" if low else "high", ident, "Non-commercial terms detected upstream")
        if effective and effective.category == "unlicensed" and node.resolved:
            add("UNLICENSED", "medium", ident, "No declared licence was found")
        if not node.resolved:
            add("UNRESOLVED_UPSTREAM", "info", ident, "Referenced upstream source could not be fetched completely")
        if node.modality == "other":
            add("NON_TEXT_NOT_ANALYSED", "info", ident, "Non-text dataset was retained but not expanded")
        for detected in node.detected:
            if more_permissive(node.declared.category, detected.category):
                add("DECLARED_DETECTED_MISMATCH", "medium" if detected.source == "card_text" else "high", ident, "Declared licence is more permissive than card-text terms")
                break
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matches = entry.get("match", [])
            if not isinstance(matches, list):
                continue
            canonical_matches = {value if str(value).startswith(("hf:", "ext:")) else "hf:" + str(value) for value in matches}
            if entry.get("reviewed_by_counsel") and ident in canonical_matches:
                code = "REMOVED" if entry.get("kind") == "removed" else "LITIGATED"
                add(code, "high", ident, f"{entry.get('summary') or code} ({entry.get('source_url') or 'source unavailable'}, reviewed {entry.get('last_reviewed') or 'date unavailable'})")

    for root in report.roots:
        root_node = report.nodes.get(root)
        if not root_node:
            continue
        for ident, (path, low) in paths_by_root.get(root, {}).items():
            if ident == root or low:
                continue
            node = report.nodes.get(ident)
            if not node:
                continue
            upstream = strictest([node.declared] + node.detected)
            if upstream and upstream.category == "sharealike" and root_node.declared.category != "sharealike":
                add("SA_UPSTREAM", "medium", ident, "ShareAlike terms detected upstream while the root declares different terms", path, root)
            if upstream and more_permissive(root_node.declared.category, upstream.category):
                add("DECLARED_MORE_PERMISSIVE_THAN_UPSTREAM", "high", ident, "Root licence is more permissive than upstream terms", path, root)
    return flags
