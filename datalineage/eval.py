import json
import sys
from pathlib import Path
from .clients import FakeHubClient, FakePaperClient
from .resolver import Resolver


def evaluate(labels, hub, papers):
    totals = {}
    coverages = []
    for row in labels:
        report = Resolver(hub, papers).resolve([row["id"]])
        actual, expected = {f.code for f in report.flags}, set(row.get("flags", []))
        coverages.append(report.coverage["pct"])
        for code in actual | expected:
            score = totals.setdefault(code, {"tp": 0, "fp": 0, "fn": 0})
            score["tp"] += code in actual and code in expected
            score["fp"] += code in actual and code not in expected
            score["fn"] += code not in actual and code in expected
    for score in totals.values():
        score["precision"] = score["tp"] / max(1, score["tp"] + score["fp"])
        score["recall"] = score["tp"] / max(1, score["tp"] + score["fn"])
    return {"flags": totals, "coverage": sum(coverages) / max(1, len(coverages))}


def main():
    root = Path(__file__).resolve().parents[1]
    labels = json.loads(Path(sys.argv[1]).read_text())
    result = evaluate(labels, FakeHubClient(root / "fixtures/hub_cards.json"), FakePaperClient(root / "fixtures/papers.json"))
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
