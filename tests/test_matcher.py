import json
import os
import sys
from typing import Any, cast
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import mcp_core as core
from termux_mcp import playbook as pb
from termux_mcp.handlers.doctor import render_do
from termux_mcp.mcp_bridge import VirtualHandler, decode_virtual, route_callable


def _top(text):
    found = pb.match_text(text)
    return found[0] if found else None


class TestMatching:

    def test_a_phrase_wins_over_extra_words(self):
        assert _top("clone this repo: bhai4you/termux-mcp")["score"] == 1.0

    def test_the_title_is_a_weaker_signal(self):
        top = _top("git repository")
        assert top["playbook"] == "clone_repo"
        assert top["score"] < 1.0

    def test_something_unknown_matches_nothing(self):
        assert pb.match_text("order me a pizza") == []
        assert pb.match_text("book a taxi") == []

    def test_a_half_match_is_offered_not_run(self):
        result = pb.do_text("turn off the wifi")
        assert result["ok"] is False
        assert result["reason"] == "unsure"
        assert result["candidates"][0]["playbook"] == "wifi_status"

    def test_the_everyday_requests_land(self):
        for text, expected in (
                ("update termux", "update_packages"),
                ("update my packages", "update_packages"),
                ("install git", "install_package"),
                ("install python", "install_package"),
                ("how much space is left", "check_storage"),
                ("what is taking up space", "find_large_files"),
                ("free up space", "free_space"),
                ("how is my battery", "battery_status"),
                ("what wifi am i on", "wifi_status"),
                ("create an ssh key", "ssh_key_setup"),
                ("give termux storage access", "termux_storage_setup"),
                ("clone this repo: bhai4you/termux-mcp", "clone_repo")):
            top = _top(text)
            assert top is not None, text
            assert top["playbook"] == expected, (text, top["playbook"])
            assert top["score"] >= pb.MIN_CONFIDENCE, text

    def test_the_strongest_match_is_first(self):
        found = pb.match_text("install git")
        assert found[0]["playbook"] == "install_package"
        scores = [c["score"] for c in found]
        assert scores == sorted(scores, reverse=True)


class TestExtraction:

    def test_a_repo_slug(self):
        assert pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone bhai4you/termux-mcp")[0]["repo"] == "bhai4you/termux-mcp"

    def test_a_github_url(self):
        assert pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone https://github.com/termux/termux-app")[0]["repo"] == \
            "termux/termux-app"

    def test_a_url_does_not_become_a_directory(self):
        inputs, _ = pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone the repo https://github.com/termux/termux-app")
        assert "dir" not in inputs

    def test_a_repo_slug_does_not_become_a_directory(self):
        inputs, _ = pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone this repo: bhai4you/termux-mcp")
        assert "dir" not in inputs

    def test_a_real_directory_is_picked_up(self):
        inputs, _ = pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone bhai4you/termux-mcp into /sdcard/projects")
        assert inputs["dir"] == "/sdcard/projects"

    def test_a_tilde_directory_is_picked_up(self):
        inputs, _ = pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"],
            "clone termux/termux-app into ~/src")
        assert inputs["dir"] == "~/src"

    def test_a_package_name(self):
        for text in ("install git", "install the package curl",
                     "pkg install termux-api", "apt install nano"):
            assert pb.extract_inputs(
                pb.load_playbooks()[0]["install_package"], text)[0][
                    "package"] == text.split()[-1]

    def test_the_package_can_come_before_or_after_the_verb(self):
        install = pb.load_playbooks()[0]["install_package"]
        for text in ("install php", "php install", "check and php install",
                     "node install", "pkg install git", "apt install curl",
                     "npm install -g pm2"):
            want = {"install php": "php", "php install": "php",
                    "check and php install": "php", "node install": "node",
                    "pkg install git": "git", "apt install curl": "curl",
                    "npm install -g pm2": "pm2"}[text]
            assert pb.extract_inputs(install, text)[0]["package"] == want, text

    def test_the_verb_alone_is_not_a_package(self):
        install = pb.load_playbooks()[0]["install_package"]
        assert "package" not in pb.extract_inputs(install, "pkg install")[0]
        assert "package" not in pb.extract_inputs(install, "apt install")[0]

    def test_a_missing_required_input_is_reported(self):
        _, missing = pb.extract_inputs(
            pb.load_playbooks()[0]["clone_repo"], "clone a repo")
        assert missing == ["repo"]


class TestDoText:

    def test_a_known_request_matches(self):
        result = pb.do_text("clone this repo: bhai4you/termux-mcp",
                            dry_run=True)
        assert result["matched"] == "clone_repo"
        assert result["ok"] is True

    def test_an_unknown_request_runs_nothing(self):
        with mock.patch("termux_mcp.playbook.run_playbook") as run:
            result = pb.do_text("order me a pizza")
        assert run.call_count == 0
        assert result["reason"] == "unknown"
        assert result["candidates"] == []

    def test_a_weak_match_asks_instead_of_guessing(self):
        with mock.patch("termux_mcp.playbook.run_playbook") as run:
            with mock.patch("termux_mcp.playbook.match_text",
                            return_value=[{"playbook": "clone_repo",
                                           "score": 0.2, "missing": [],
                                           "inputs": {}, "phrases": [],
                                           "title": "t"}]):
                result = pb.do_text("something vague")
        assert run.call_count == 0
        assert result["reason"] == "unsure"

    def test_a_missing_value_runs_nothing(self):
        with mock.patch("termux_mcp.playbook.run_playbook") as run:
            result = pb.do_text("clone a repo over there")
        assert run.call_count == 0
        assert result["reason"] == "missing_input"
        assert "repo" in result["errors"][0]

    def test_an_empty_request(self):
        assert pb.do_text("  ")["reason"] == "empty"


class TestRender:

    def test_a_match_shows_what_ran(self):
        result = {"ok": True, "playbook": "clone_repo", "matched": "clone_repo",
                  "runs": [], "success": "Cloned a/b into ~/b.",
                  "task_id": "t-1"}
        text = render_do(result, "clone a/b")
        assert "matched: clone_repo" in text
        assert "Cloned a/b into ~/b." in text

    def test_a_missing_value_says_what_is_needed(self):
        result = {"ok": False, "reason": "missing_input",
                  "matched": "clone_repo", "candidates": [],
                  "errors": ["clone_repo needs: repo"]}
        text = render_do(result, "clone a repo somewhere")
        assert "matched: clone_repo" in text
        assert "nothing was run" in text
        assert "needs: repo" in text
        assert "?" not in text.split("\n")[0]

    def test_an_unknown_request_lists_what_is_known(self):
        text = render_do({"ok": False, "reason": "unknown",
                          "errors": ["Nothing in the library does that yet."],
                          "candidates": []}, "turn off the wifi")
        assert "nothing was run" in text
        assert "known: " in text
        assert "clone_repo" in text

    def test_candidates_are_shown_with_their_phrases(self):
        result = {"ok": False, "reason": "unsure", "errors": ["Not sure."],
                  "candidates": [{"playbook": "clone_repo", "score": 0.42,
                                  "missing": ["repo"],
                                  "phrases": ["clone {repo}"]}]}
        text = render_do(result, "something")
        assert "clone_repo (0.42)" in text
        assert "clone {repo}" in text
        assert "needs repo" in text


class TestDoHandler:

    def _call(self, data):
        handler = cast(Any, VirtualHandler())
        route_callable("do")(handler, data)
        return decode_virtual(handler)

    def test_the_route_exists(self):
        assert route_callable("do") is not None

    def test_it_answers_in_text(self):
        with mock.patch("termux_mcp.handlers.doctor.do_text",
                        return_value={"ok": True, "matched": "clone_repo",
                                      "runs": [], "success": "Cloned."}):
            result = self._call({"text": "clone a/b"})
        assert "matched: clone_repo" in result["text"]

    def test_it_answers_in_json_when_asked(self):
        with mock.patch("termux_mcp.handlers.doctor.do_text",
                        return_value={"ok": False, "reason": "unknown"}):
            result = self._call({"text": "x", "format": "json"})
        assert json.loads(result["text"])["reason"] == "unknown"

    def test_dry_run_reaches_the_matcher(self):
        with mock.patch("termux_mcp.handlers.doctor.do_text",
                        return_value={"ok": True}) as call:
            self._call({"text": "clone a/b", "dry_run": True})
        assert call.call_args[1]["dry_run"] is True


class TestResources:

    def test_the_capability_is_advertised(self):
        assert "resources" in core.server_info()["capabilities"]

    def test_the_index_is_readable(self):
        body = json.loads(core.resource_read(core.LIBRARY_URI)
                          ["contents"][0]["text"])
        assert body["problems"] == []
        assert any(p["id"] == "clone_repo" for p in body["playbooks"])
        assert body["checks"]

    def test_each_playbook_is_a_resource(self):
        listed = [r["uri"] for r in core.resource_list()["resources"]]
        assert f"{core.PLAYBOOK_URI}clone_repo" in listed

    def test_a_playbook_reads_in_full(self):
        uri = f"{core.PLAYBOOK_URI}clone_repo"
        body = json.loads(core.resource_read(uri)["contents"][0]["text"])
        assert body["id"] == "clone_repo"
        assert body["steps"]
        assert "_file" not in body

    def test_an_unknown_playbook_is_an_error(self):
        try:
            core.resource_read(f"{core.PLAYBOOK_URI}ghost")
        except core.RpcError as error:
            assert "ghost" in str(error)
        else:
            raise AssertionError("expected an error")

    def test_an_unknown_uri_is_an_error(self):
        try:
            core.resource_read("nonsense://x")
        except core.RpcError:
            pass
        else:
            raise AssertionError("expected an error")

    def test_the_dispatch_reaches_them(self):
        session = core.MCPSession("res", "test")
        assert core.dispatch(session, "resources/list", {})["resources"]
        assert core.dispatch(session, "resources/read",
                             {"uri": core.LIBRARY_URI})["contents"]
