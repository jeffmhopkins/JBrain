"""The `flows` tier: validate every shipped automation definition — workflows/*.yaml
and actions/*.yaml — against the real engine's contract, so a malformed trigger, an
unparseable cron, an unknown action type, or a recipe step using an unknown primitive
fails CI instead of silently breaking automation at runtime.

Static/contract validation only (one end-to-end workflow run is covered by the e2e
suite). No DB, no LLM, no network — pure parse + registry checks against pipeline.py.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.flows

pytest.importorskip("yaml")
import yaml

from app.services import pipeline

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / "workflows").glob("*.yaml"))
ACTIONS = sorted((REPO / "actions").glob("*.yaml"))

# Trigger types the engine understands (see workflows.py dispatch_event / schedule_due).
VALID_TRIGGERS = {"event", "schedule"}


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict), f"{path.name}: top-level YAML must be a mapping"
    return doc


def test_there_are_workflows_and_actions_to_check():
    assert WORKFLOWS, "no workflows/*.yaml found"
    assert ACTIONS, "no actions/*.yaml found"


# --- actions/*.yaml ---------------------------------------------------------- #

def test_action_recipes_lint_clean():
    """Every shipped recipe's steps reference only known primitives and in-scope
    inputs (pipeline.validate_action_defs is the engine's own boot lint)."""
    pipeline.reload_action_defs()
    warnings = pipeline.validate_action_defs()
    assert warnings == [], "action recipe lint warnings:\n" + "\n".join(warnings)


@pytest.mark.parametrize("path", ACTIONS, ids=lambda p: p.name)
def test_action_yaml_shape(path: Path):
    doc = _load(path)
    assert doc.get("type"), f"{path.name}: missing 'type'"
    steps = doc.get("steps")
    assert isinstance(steps, list) and steps, f"{path.name}: needs a non-empty 'steps' list"
    assert all(isinstance(st, dict) for st in steps), f"{path.name}: each step must be a mapping"
    # Per-step correctness (known primitives, in-scope inputs, for_each/stop_when/when
    # control flow) is validated by the engine's own lint in
    # test_action_recipes_lint_clean — not re-implemented here.
    # config field defs, when present, must be a list of {key,...} descriptors.
    cfg = doc.get("config", [])
    assert isinstance(cfg, list), f"{path.name}: 'config' must be a list of field defs"
    for field in cfg:
        assert isinstance(field, dict) and field.get("key"), f"{path.name}: config field needs a 'key'"


def test_action_types_unique():
    types = [_load(p)["type"] for p in ACTIONS]
    dupes = {t for t in types if types.count(t) > 1}
    assert not dupes, f"duplicate action types across actions/*.yaml: {dupes}"


# --- workflows/*.yaml -------------------------------------------------------- #

@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_yaml_contract(path: Path):
    doc = _load(path)
    assert doc.get("key"), f"{path.name}: missing 'key'"

    trig = doc.get("trigger", {}) or {}
    ttype = trig.get("type", "event")
    assert ttype in VALID_TRIGGERS, f"{path.name}: bad trigger type {ttype!r}"

    if ttype == "schedule":
        cron = trig.get("cron")
        # interval_seconds must be a non-negative int (0 is the intentional "manual
        # only" sentinel — schedule_due() returns False, so it runs via Run-now only).
        interval = trig.get("interval_seconds", 0)
        assert isinstance(interval, int) and interval >= 0, (
            f"{path.name}: interval_seconds must be a non-negative int")
        assert cron or interval is not None, f"{path.name}: schedule needs cron or interval_seconds"
        if cron:
            from croniter import croniter
            assert croniter.is_valid(cron), f"{path.name}: invalid cron {cron!r}"

    act = doc.get("action", {}) or {}
    atype = act.get("type", "")
    assert atype, f"{path.name}: missing action.type"
    pipeline.reload_action_defs()
    assert pipeline.get_action_def(atype) is not None, (
        f"{path.name}: action.type {atype!r} resolves to no recipe in actions/")


def test_workflow_keys_unique():
    keys = [_load(p)["key"] for p in WORKFLOWS]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate workflow keys: {dupes}"
