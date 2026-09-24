import json
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import playbook as pb


def _playbook(**over):
    base = {
        "schema": 1,
        "id": "sample",
        "title": "A sample",
        "phrases": ["do the sample"],
        "match": {"thing": {"type": "text", "required": True}},
        "steps": [{"run": "echo {thing}"}],
    }
    base.update(over)
    return base


def _check(**over):
    base = {
        "schema": 1,
        "id": "sample_present",
        "title": "Something is present",
        "severity": "high",
        "probe": "command -v thing",
    }
    base.update(over)
    return base


def _problems(playbooks=None, checks=None):
    return pb.validate(playbooks or {}, checks or {})


class TestShippedLibrary:

    def test_the_library_loads_and_validates(self):
        library = pb.load_library()
        assert library["problems"] == []

    def test_it_has_playbooks_and_checks(self):
        library = pb.load_library()
        assert library["playbooks"]
        assert len(library["checks"]) >= 10

    def test_every_playbook_resolves_with_required_inputs_only(self):
        library = pb.load_library()
        for name, playbook in library["playbooks"].items():
            supplied = {slot: "x" for slot, spec in playbook["match"].items()
                        if isinstance(spec, dict) and spec.get("required")}
            result = pb.resolve(name, supplied, library["playbooks"])
            assert result["ok"], (name, result.get("errors"))

    def test_no_playbook_claims_another_ones_phrase(self):
        library = pb.load_library()
        assert pb._phrases(library["playbooks"]) == {}


class TestLoader:

    def test_yaml_loads_when_pyyaml_is_there(self, tmp_path):
        path = tmp_path / "sample.json"
        path.write_text(json.dumps(_playbook()), encoding="utf-8")
        body, error = pb._read(str(path))
        assert error is None
        assert body["id"] == "sample"

    def test_a_broken_file_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        body, error = pb._read(str(path))
        assert body is None
        assert "broken.json" in error

    def test_a_duplicate_id_is_reported(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(_playbook(id="same")),
                                         encoding="utf-8")
        (tmp_path / "b.json").write_text(json.dumps(_playbook(id="same")),
                                         encoding="utf-8")
        found, errors = pb._load_dir(str(tmp_path))
        assert len(found) == 1
        assert any("duplicate id" in e for e in errors)

    def test_yaml_without_pyyaml_says_so(self, tmp_path):
        path = tmp_path / "sample.yaml"
        path.write_text("id: sample\n", encoding="utf-8")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            body, error = pb._read(str(path))
        assert body is None
        assert "PyYAML" in error


class TestValidator:

    def test_a_good_playbook_has_no_problems(self):
        assert _problems({"sample": _playbook()}) == []

    def test_an_unknown_slot_in_a_step(self):
        found = _problems({"sample": _playbook(
            steps=[{"run": "echo {missing}"}])})
        assert any("{missing}" in p for p in found)

    def test_an_unknown_slot_in_the_success_line(self):
        found = _problems({"sample": _playbook(success="done {nope}")})
        assert any("{nope}" in p for p in found)

    def test_a_required_slot_with_a_default(self):
        found = _problems({"sample": _playbook(
            match={"thing": {"type": "text", "required": True,
                             "default": "x"}})})
        assert any("required and also has a default" in p for p in found)

    def test_an_unknown_slot_type(self):
        found = _problems({"sample": _playbook(
            match={"thing": {"type": "sandwich"}})})
        assert any("unknown type" in p for p in found)

    def test_a_derive_from_an_undeclared_slot(self):
        found = _problems({"sample": _playbook(
            match={"thing": {"type": "name", "from": "nowhere",
                             "op": "basename"}})})
        assert any("derives from undeclared" in p for p in found)

    def test_a_derive_with_an_unknown_op(self):
        found = _problems({"sample": _playbook(
            match={"thing": {"type": "text"},
                   "other": {"type": "name", "from": "thing",
                             "op": "teleport"}})})
        assert any("unknown op" in p for p in found)

    def test_a_step_with_both_run_and_tool(self):
        found = _problems({"sample": _playbook(
            steps=[{"run": "echo hi", "tool": "ls"}])})
        assert any("exactly one of run or tool" in p for p in found)

    def test_a_step_with_neither(self):
        found = _problems({"sample": _playbook(steps=[{"verify": "true"}])})
        assert any("exactly one of run or tool" in p for p in found)

    def test_no_steps(self):
        assert any("no steps" in p for p in _problems(
            {"sample": _playbook(steps=[])}))
        assert any("no steps" in p for p in _problems(
            {"sample": _playbook(steps=None)}))

    def test_no_title(self):
        assert any("no title" in p for p in _problems(
            {"sample": _playbook(title="  ")}))

    def test_a_wrong_schema_version(self):
        assert any("schema" in p for p in _problems(
            {"sample": _playbook(schema=99)}))

    def test_an_unknown_risk(self):
        assert any("risk" in p for p in _problems(
            {"sample": _playbook(risk="spicy")}))

    def test_no_phrases(self):
        assert any("no phrases" in p for p in _problems(
            {"sample": _playbook(phrases=[])}))

    def test_a_requirement_naming_an_unknown_check(self):
        found = _problems({"sample": _playbook(
            requires=[{"check": "ghost"}])})
        assert any("unknown check ghost" in p for p in found)

    def test_a_requirement_that_is_not_a_reference(self):
        found = _problems({"sample": _playbook(requires=["git_present"])})
        assert any("not a check reference" in p for p in found)

    def test_the_same_phrase_in_two_playbooks(self):
        found = _problems({
            "sample": _playbook(),
            "other": _playbook(id="other", phrases=["Do   The Sample"]),
        })
        assert any("claimed by" in p for p in found)

    def test_a_check_with_no_probe(self):
        assert any("no probe" in p for p in _problems(
            checks={"sample_present": _check(probe="")}))

    def test_a_check_with_an_unknown_severity(self):
        assert any("severity" in p for p in _problems(
            checks={"sample_present": _check(severity="catastrophic")}))

    def test_a_check_with_an_uncompilable_pattern(self):
        assert any("does not compile" in p for p in _problems(
            checks={"sample_present": _check(
                expect={"stdout_matches": "([unclosed"})}))

    def test_a_check_with_an_unknown_expect_key(self):
        assert any("unknown expect key" in p for p in _problems(
            checks={"sample_present": _check(expect={"stderr": "x"})}))

    def test_a_fix_naming_an_unknown_playbook(self):
        found = _problems({"sample": _playbook()},
                          {"sample_present": _check(
                              fix={"playbook": "nowhere"})})
        assert any("unknown playbook nowhere" in p for p in found)

    def test_a_fix_that_does_not_supply_a_required_slot(self):
        found = _problems({"sample": _playbook()},
                          {"sample_present": _check(
                              fix={"playbook": "sample"})})
        assert any("does not supply thing" in p for p in found)

    def test_a_fix_that_does_supply_it_is_fine(self):
        found = _problems({"sample": _playbook()},
                          {"sample_present": _check(
                              fix={"playbook": "sample",
                                   "inputs": {"thing": "x"}})})
        assert found == []

    def test_a_repair_cycle_is_caught(self):
        playbooks = {
            "sample": _playbook(requires=[{"check": "sample_present"}]),
        }
        checks = {"sample_present": _check(fix={"playbook": "sample",
                                                "inputs": {"thing": "x"}})}
        found = _problems(playbooks, checks)
        assert any("repair cycle" in p for p in found)


class TestResolver:

    def test_substitution_and_defaults(self):
        result = pb.resolve("clone_repo", {"repo": "bhai4you/termux-mcp"})
        assert result["ok"]
        assert result["inputs"]["dir"] == "~"
        assert result["inputs"]["name"] == "termux-mcp"
        assert "git clone https://github.com/bhai4you/termux-mcp.git" in \
            result["steps"][0]["run"]
        assert result["success"] == "Cloned bhai4you/termux-mcp into ~/termux-mcp."

    def test_a_missing_required_input(self):
        result = pb.resolve("clone_repo", {})
        assert not result["ok"]
        assert "repo" in result["errors"][0]

    def test_an_unknown_playbook(self):
        result = pb.resolve("no_such_playbook", {})
        assert not result["ok"]

    def test_extra_inputs_are_reported_not_used(self):
        result = pb.resolve("clone_repo", {"repo": "a/b", "colour": "blue"})
        assert result["ok"]
        assert result["unused"] == ["colour"]

    def test_a_shell_variable_is_not_a_slot(self):
        playbook = _playbook(steps=[{"run": "echo ${HOME} {thing}"}])
        assert pb.validate({"sample": playbook}, {}) == []
        result = pb.resolve("sample", {"thing": "x"},
                            {"sample": playbook})
        assert result["steps"][0]["run"] == "echo ${HOME} x"

    def test_a_shell_variable_survives_validation_unsubstituted(self):
        playbook = _playbook(steps=[{"run": "test -w $PREFIX && ls"}])
        assert pb.validate({"sample": playbook}, {}) == []

    def test_tool_parameters_are_substituted_too(self):
        playbook = _playbook(steps=[{"tool": "ask", "with": {"title": "{thing}"}}])
        result = pb.resolve("sample", {"thing": "Which one?"},
                            {"sample": playbook})
        assert result["steps"][0]["with"]["title"] == "Which one?"

    def test_a_defaulted_slot_can_be_overridden(self):
        result = pb.resolve("clone_repo", {"repo": "a/b", "dir": "/tmp"})
        assert "/tmp/b" in result["steps"][0]["run"]


class TestRepair:

    def _checks(self, **over):
        check = _check(**over)
        return {check["id"]: check}

    def test_a_missing_fix_is_reported(self):
        result = pb.apply_fix(_check())
        assert result["applied"] is False
        assert "no fix" in result["reason"]

    def test_a_fix_that_cannot_be_resolved(self):
        result = pb.apply_fix(_check(fix={"playbook": "ghost"}))
        assert result["applied"] is False
        assert "No playbook named ghost" in result["reason"]

    def test_a_fix_runs_its_steps(self):
        playbook = _playbook(steps=[{"run": "echo {thing}"}])
        check = _check(fix={"playbook": "sample", "inputs": {"thing": "hi"}})
        with mock.patch("termux_mcp.shell.run_captured",
                        return_value={"ok": True, "text": "hi",
                                      "reason": ""}) as run:
            result = pb.apply_fix(check, playbooks={"sample": playbook})
        assert result["applied"] is True
        assert run.call_args[0][0] == "echo hi"

    def test_a_failing_step_stops_the_rest(self):
        playbook = _playbook(steps=[{"run": "one"}, {"run": "two"}])
        check = _check(fix={"playbook": "sample", "inputs": {"thing": "x"}})
        with mock.patch("termux_mcp.shell.run_captured",
                        return_value={"ok": False, "reason": "failed",
                                      "text": "boom"}) as run:
            result = pb.apply_fix(check, playbooks={"sample": playbook})
        assert result["applied"] is False
        assert run.call_count == 1
        assert result["runs"][0]["text"] == "boom"

    def test_a_tool_step_is_not_run_yet(self):
        playbook = _playbook(steps=[{"tool": "run", "with": {"cmd": "x"}}])
        check = _check(fix={"playbook": "sample", "inputs": {"thing": "x"}})
        with mock.patch("termux_mcp.shell.run_captured") as run:
            result = pb.apply_fix(check, playbooks={"sample": playbook})
        assert run.call_count == 0
        assert "cannot run yet" in result["reason"]

    def test_a_confirmation_that_is_refused_is_not_a_repair(self):
        check = _check(fix={"playbook": "sample", "inputs": {"thing": "x"}})
        playbook = _playbook(steps=[{"run": "echo {thing}"}])
        with mock.patch("termux_mcp.shell.run_captured",
                        return_value={"ok": False,
                                      "reason": "confirmation_required",
                                      "text": "needs confirming"}):
            result = pb.apply_fix(check, playbooks={"sample": playbook})
        assert result["fixed"] is False
        assert result["reason"] == "confirmation_required"

    def test_repair_reports_an_unknown_check(self):
        result = pb.repair("ghost")
        assert result["fixed"] is False
        assert "no check named ghost" in result["reason"]

    def test_a_passing_check_is_left_alone(self):
        with mock.patch("termux_mcp.playbook.run_check",
                        return_value={"ok": True, "id": "git_present"}):
            with mock.patch("termux_mcp.playbook.apply_fix") as fix:
                result = pb.repair("git_present")
        assert result["already_ok"] is True
        assert fix.call_count == 0

    def _world(self, healed: bool):
        state = {"git_present": False, "network_reachable": True}

        def probe(check, timeout=pb.PROBE_TIMEOUT):
            return {"ok": state.get(check["id"], False), "id": check["id"]}

        def apply(check, confirmed=False, task_id="", playbooks=None):
            if healed:
                state["git_present"] = True
            return {"check": check["id"], "applied": healed, "runs": []}

        return state, probe, apply

    def test_a_repair_that_works_is_marked_fixed(self):
        state, probe, apply = self._world(healed=True)
        with mock.patch("termux_mcp.playbook.run_check", side_effect=probe):
            with mock.patch("termux_mcp.playbook.apply_fix", side_effect=apply):
                result = pb.repair("git_present")
        assert result["fixed"] is True

    def test_a_repair_that_fails_is_not_marked_fixed(self):
        state, probe, apply = self._world(healed=False)
        with mock.patch("termux_mcp.playbook.run_check", side_effect=probe):
            with mock.patch("termux_mcp.playbook.apply_fix", side_effect=apply):
                result = pb.repair("git_present")
        assert result["fixed"] is False
        assert result["applied"] is False

    def test_a_requirement_that_cannot_be_met_blocks_the_repair(self):
        state, probe, apply = self._world(healed=True)
        state["network_reachable"] = False
        with mock.patch("termux_mcp.playbook.run_check", side_effect=probe):
            with mock.patch("termux_mcp.playbook.apply_fix", side_effect=apply):
                result = pb.repair("git_present")
        assert result["fixed"] is False
        assert "requirement could not be met" in result["reason"]
        assert result["blocked_by"][0]["check"] == "network_reachable"

    def test_a_check_that_leads_back_to_itself_stops(self):
        playbooks = {"sample": _playbook(
            requires=[{"check": "sample_present"}])}
        checks = self._checks(fix={"playbook": "sample",
                                   "inputs": {"thing": "x"}})
        with mock.patch("termux_mcp.playbook.load_playbooks",
                        return_value=(playbooks, [])):
            with mock.patch("termux_mcp.playbook.load_checks",
                            return_value=(checks, [])):
                with mock.patch("termux_mcp.playbook.run_check",
                                return_value={"ok": False,
                                              "id": "sample_present"}):
                    with mock.patch("termux_mcp.playbook.apply_fix") as fix:
                        result = pb.repair("sample_present")
        assert result["fixed"] is False
        assert fix.call_count == 0
        assert "could not be met" in result["reason"]

    def test_the_shipped_clone_playbook_needs_git(self):
        library = pb.load_library()
        assert pb.requirement_ids(library["playbooks"]["clone_repo"]) == \
            ["git_present"]
        assert library["checks"]["git_present"]["fix"]["playbook"] == \
            "install_package"


class TestDoctorFix:

    def test_fixing_reprobes_afterwards(self):
        calls = []

        def fake_check(check, timeout=pb.PROBE_TIMEOUT):
            calls.append(check["id"])
            return {"ok": False, "id": check["id"], "severity": "high",
                    "title": "t", "explain": "", "probe": "p", "fix": None,
                    "detail": "", "code": 1}

        with mock.patch("termux_mcp.playbook.run_check", side_effect=fake_check):
            with mock.patch("termux_mcp.playbook.repair",
                            return_value={"check": "git_present",
                                          "fixed": True}):
                report = pb.run_doctor(only=["git_present"], fix=True)
        assert calls == ["git_present", "git_present"]
        assert report["repairs"][0]["fixed"] is True

    def test_without_fix_nothing_is_repaired(self):
        with mock.patch("termux_mcp.playbook.run_check",
                        return_value={"ok": False, "id": "git_present"}):
            with mock.patch("termux_mcp.playbook.repair") as repair:
                report = pb.run_doctor(only=["git_present"])
        assert repair.call_count == 0
        assert report["repairs"] == []


class _Done:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class TestDoctor:

    def _run(self, check, done=None, error=None):
        target = "subprocess.run"
        with mock.patch(target) as run:
            if error:
                run.side_effect = error
            else:
                run.return_value = done
            return pb.run_check(check)

    def test_a_passing_probe(self):
        finding = self._run(_check(), _Done(0, "/usr/bin/thing\n"))
        assert finding["ok"] is True
        assert finding["detail"] == ""

    def test_a_failing_probe_keeps_its_output(self):
        finding = self._run(_check(), _Done(1, "", "thing: not found\n"))
        assert finding["ok"] is False
        assert "not found" in finding["detail"]

    def test_an_expected_nonzero_exit(self):
        check = _check(expect={"exit": 1})
        assert self._run(check, _Done(1))["ok"] is True
        assert self._run(check, _Done(0))["ok"] is False

    def test_stdout_must_match_when_asked(self):
        check = _check(expect={"exit": 0, "stdout_matches": "reachable"})
        assert self._run(check, _Done(0, "reachable\n"))["ok"] is True
        assert self._run(check, _Done(0, "offline\n"))["ok"] is False

    def test_stdout_must_not_match_when_asked(self):
        check = _check(expect={"stdout_missing": "LOCK"})
        assert self._run(check, _Done(0, "all clear\n"))["ok"] is True
        assert self._run(check, _Done(0, "LOCK held\n"))["ok"] is False

    def test_a_timeout_is_a_finding_not_a_crash(self):
        finding = self._run(_check(), error=subprocess.TimeoutExpired("x", 1))
        assert finding["ok"] is False
        assert "no answer" in finding["detail"]

    def test_a_missing_command_is_a_finding(self):
        finding = self._run(_check(), error=FileNotFoundError())
        assert finding["ok"] is False

    def test_high_severity_findings_come_first(self):
        report = pb.run_doctor()
        severities = [f["severity"] for f in report["findings"]]
        assert severities == sorted(severities,
                                    key=pb.SEVERITIES.index)

    def test_only_runs_what_was_named(self):
        report = pb.run_doctor(only=["git_present"])
        assert [f["id"] for f in report["findings"]] == ["git_present"]

    def test_an_unknown_check_is_reported_not_run(self):
        report = pb.run_doctor(only=["ghost"])
        assert report["findings"] == []
        assert report["unknown"] == ["ghost"]

    def test_the_report_counts_what_it_ran(self):
        report = pb.run_doctor()
        assert report["checked"] == len(report["findings"])
        assert report["failing"] == len([f for f in report["findings"]
                                         if not f["ok"]])

    def test_a_real_run_does_not_crash(self):
        report = pb.run_doctor(only=["git_present", "network_reachable"])
        assert report["checked"] == 2
        for finding in report["findings"]:
            assert isinstance(finding["ok"], bool)
            assert finding["title"]
