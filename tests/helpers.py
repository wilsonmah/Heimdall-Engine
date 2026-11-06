import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def universal():
    return copy.deepcopy(load("templates/tmpl_universal_v1.json"))


def vendor():
    return copy.deepcopy(load("templates/tmpl_vendor_scorecard.json"))


def payload(name):
    return copy.deepcopy(load(f"examples/payloads/{name}.json"))
