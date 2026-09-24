import json
import os
import shutil
import sys
import tempfile
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import playbook as pb
from termux_mcp.handlers.doctor import render_harvest
from termux_mcp.mcp_bridge import VirtualHandler, decode_virtual, route_callable


class TestParameterising:

    def _draft(self, steps, **over):
        return pb.harvest(steps=steps, **over)

    def test_a_github_url_becomes_a_repo(self):
        draft = self._draft(["git clone https://github.com/bhai4you/termux-mcp.git ~/termux-mcp"],
                            title="Clone my bot repo", playbook_dir=tempfile.mkdtemp())
        assert draft["draft"]["steps"][0]["run"] == \
            "git clone https://{host}/{repo}.git {path}"
        assert sorted(draft["slots"]) == ["host", "path", "repo"]

    def test_another_host_stays_a_url(self):
        draft = self._draft(["git clone https://gitlab.com/team/thing.git /sdcard/thing"],
                            title="Clone from gitlab",
                            playbook_dir=tempfile.mkdtemp())
        assert draft["draft"]["steps"][0]["run"] == "git clone {url} {path}"

    def test_a_path_is_not_mistaken_for_a_repo(self):
        draft = self._draft(["echo hello > /sdcard/notes/today.txt"],
                            title="Write a note",
                            playbook_dir=tempfile.mkdtemp())
        assert draft["draft"]["steps"][0]["run"] == "echo hello > {path}"

    def test_flags_survive_a_package_slot(self):
        draft = self._draft(["pkg install -y nodejs", "npm install -g pm2"],
                            title="Set up pm2",
                            playbook_dir=tempfile.mkdtemp())
        runs = [step["run"] for step in draft["draft"]["steps"]]
        assert runs == ["pkg install -y {package}",
                        "npm install -g {package2}"]

    def test_values_can_be_given_explicitly(self):
        draft = self._draft(["deploy.sh myapp", "deploy.sh myapp --check"],
                            title="Deploy",
                            values={"name": "myapp"},
                            playbook_dir=tempfile.mkdtemp())
        runs = [step["run"] for step in draft["draft"]["steps"]]
        assert runs == ["deploy.sh {name}", "deploy.sh {name} --check"]
        assert draft["draft"]["match"]["name"]["type"] == "name"

    def test_nothing_to_learn(self):
        result = pb.harvest(steps=[])
        assert result["ok"] is False
        assert "Nothing to learn" in result["errors"][0]

    def test_a_title_makes_a_readable_id(self):
        draft = self._draft(["ls"], title="Show me the logs!",
                            playbook_dir=tempfile.mkdtemp())
        assert draft["playbook"] == "show_me_the_logs"

    def test_an_explicit_id_wins(self):
        draft = self._draft(["ls"], title="Anything", playbook_id="my_task",
                            playbook_dir=tempfile.mkdtemp())
        assert draft["playbook"] == "my_task"


class TestSaving:

    def setup_method(self):
        self.root = tempfile.mkdtemp(prefix="mcp-saved-")

    def teardown_method(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_writes_a_valid_file(self):
        result = pb.harvest(steps=["pkg install git"], title="Install git",
                            playbook_dir=self.root)
        assert result["ok"] is True
        with open(result["path"], encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved["id"] == "install_git"
        assert saved["steps"][0]["run"] == "pkg install {package}"
        assert saved["schema"] == pb.SCHEMA_VERSION

    def test_the_draft_validates_against_the_whole_library(self):
        result = pb.harvest(steps=["pkg install git"], title="Install git",
                            playbook_dir=self.root)
        playbooks, _ = pb.load_playbooks()
        problems = pb.validate(dict(playbooks,
                                    **{result["playbook"]: result["draft"]}),
                               pb.load_checks()[0])
        assert problems == []

    def test_it_refuses_to_clobber_without_being_told(self):
        pb.harvest(steps=["ls"], title="Twice", playbook_dir=self.root)
        result = pb.harvest(steps=["ls"], title="Twice",
                            playbook_dir=self.root)
        assert result["ok"] is False
        assert "already exists" in result["errors"][0]

    def test_it_clobbers_when_told(self):
        pb.harvest(steps=["ls"], title="Twice", playbook_dir=self.root)
        result = pb.harvest(steps=["ls -la"], title="Twice", overwrite=True,
                            playbook_dir=self.root)
        assert result["ok"] is True

    def test_it_warns_that_there_is_no_rollback(self):
        result = pb.harvest(steps=["ls"], title="Listing",
                            playbook_dir=self.root)
        assert "No rollback" in result["note"]

    def test_a_saved_playbook_loads_and_resolves(self):
        pb.harvest(steps=["git clone https://github.com/a/b.git ~/b"],
                   title="Clone b", playbook_dir=self.root)
        found, errors = pb._load_dir(self.root)
        assert errors == []
        assert "clone_b" in found
        plan = pb.resolve("clone_b", {"repo": "x/y", "host": "github.com",
                                      "path": "~/y"}, found)
        assert plan["ok"] is True
        assert "https://github.com/x/y.git ~/y" in plan["steps"][0]["run"]


class TestUserLibrary:

    def setup_method(self):
        self.root = tempfile.mkdtemp(prefix="mcp-user-")

    def teardown_method(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_saved_playbooks_join_the_library(self):
        pb.harvest(steps=["pkg install git"], title="Install git",
                   playbook_dir=self.root)
        import unittest.mock as mock
        with mock.patch("termux_mcp.playbook.user_dir", return_value=self.root):
            playbooks, errors = pb.load_playbooks()
        assert "install_git" in playbooks
        assert "clone_repo" in playbooks
        assert errors == []

    def test_a_saved_playbook_can_take_a_shipped_name(self):
        broken = {"schema": 1, "id": "clone_repo", "title": "Mine",
                  "phrases": ["x"], "match": {}, "steps": [{"run": "ls"}]}
        with open(os.path.join(self.root, "clone_repo.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(broken, handle)
        import unittest.mock as mock
        with mock.patch("termux_mcp.playbook.user_dir", return_value=self.root):
            playbooks, errors = pb.load_playbooks()
        assert playbooks["clone_repo"]["title"] == "A git repository clone" \
            if False else playbooks["clone_repo"].get("title") != "Mine"
        assert any("shipped name" in e for e in errors)

    def test_the_matcher_finds_saved_playbooks(self):
        pb.harvest(steps=["pkg install git"], title="Install git",
                   playbook_dir=self.root,
                   phrases=["install git", "get git"])
        import unittest.mock as mock
        with mock.patch("termux_mcp.playbook.user_dir", return_value=self.root):
            found = pb.match_text("get git")
        assert found and found[0]["playbook"] == "install_git"


class TestRender:

    def test_a_saved_task_reads_back(self):
        result = {"ok": True, "playbook": "clone_b", "path": "/tmp/clone_b.json",
                  "slots": ["repo"],
                  "draft": {"title": "Clone b",
                            "steps": [{"run": "git clone {repo}"}],
                            "phrases": ["do clone b"]},
                  "note": "No rollback was declared."}
        text = render_harvest(result)
        assert "Saved as clone_b" in text
        assert "$ git clone {repo}" in text
        assert "takes: repo" in text
        assert "No rollback" in text

    def test_a_refusal_says_why(self):
        result = {"ok": False, "errors": ["/tmp/x.json already exists"],
                  "draft": {"steps": [{"run": "ls"}]}}
        text = render_harvest(result)
        assert text.startswith("Not saved.")
        assert "already exists" in text


class TestHandler:

    def _call(self, data):
        handler = cast(Any, VirtualHandler())
        route_callable("harvest")(handler, data)
        return decode_virtual(handler)

    def test_the_route_exists(self):
        assert route_callable("harvest") is not None

    def test_steps_can_come_as_lines(self):
        import unittest.mock as mock
        with mock.patch("termux_mcp.handlers.doctor.harvest",
                        return_value={"ok": True, "playbook": "x", "path": "p",
                                      "draft": {"title": "t", "steps": [],
                                                "phrases": []},
                                      "slots": [], "note": ""}) as call:
            self._call({"steps": "pkg install git\nls", "title": "t"})
        assert call.call_args[1]["steps"] == ["pkg install git", "ls"]

    def test_json_when_asked(self):
        with tempfile.TemporaryDirectory() as root:
            import unittest.mock as mock
            with mock.patch("termux_mcp.playbook.user_dir",
                            return_value=root):
                result = self._call({"steps": ["pkg install git"],
                                     "title": "Install git",
                                     "format": "json"})
        assert json.loads(result["text"])["ok"] is True
