import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import intent, semantic

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prompts.json")
with open(DATA, encoding="utf-8") as _handle:
    PROMPTS = json.load(_handle)

EQUIVALENT = {"battery": {"playbook:battery_status"}, "wifi_info": {"playbook:wifi_status"},
              "storage_clean": {"playbook:free_space"},
              "run": {"playbook:check_storage", "playbook:update_packages"},
              "pkg_ensure": {"playbook:install_package"}}


@pytest.fixture(autouse=True, scope="module")
def clean_home():
    home = tempfile.mkdtemp(prefix="intent-home-")
    old = safety.HOME
    safety.HOME = home
    semantic.invalidate()
    yield
    safety.HOME = old
    semantic.invalidate()
    shutil.rmtree(home, ignore_errors=True)


def test_the_set_has_200_prompts_with_both_kinds():
    assert len(PROMPTS) == 200
    assert sum(p["label"] == "ai" for p in PROMPTS) >= 40
    assert len({p["text"] for p in PROMPTS}) == 200


@pytest.mark.parametrize("p", PROMPTS, ids=[p["text"][:40] for p in PROMPTS])
def test_accuracy(p):
    r = intent.route(p["text"])
    top = r["candidates"][0] if r["candidates"] else None
    if p["label"] == "ai":
        assert r["route"] != "local", (p["text"], r)
        return
    assert top is not None and r["route"] in ("local", "choose"), (p["text"], r)
    assert top["tool"] == p["label"] or top["intent"] in EQUIVALENT.get(p["label"], set()), (p["text"], r)


@pytest.mark.parametrize("p", PROMPTS, ids=[p["text"][:40] for p in PROMPTS])
def test_golden(p):
    r = intent.route(p["text"])
    top = r["candidates"][0] if r["candidates"] else {}
    assert (r["route"], top.get("tool"), top.get("intent")) == (p["route"], p["tool"], p["intent"])


def test_most_tool_prompts_run_with_no_model():
    local = sum(1 for p in PROMPTS if p["label"] != "ai" and p["route"] == "local")
    tool_prompts = sum(1 for p in PROMPTS if p["label"] != "ai")
    assert local / tool_prompts >= 0.85
