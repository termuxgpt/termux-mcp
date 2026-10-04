import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import changes, kernel, safety


@pytest.fixture
def root(monkeypatch):
    h = tempfile.mkdtemp(prefix="pkg-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    yield safety.safety_root("")
    shutil.rmtree(h, ignore_errors=True)


class TestParse:
    @pytest.mark.parametrize("cmd,expected", [
        ("pkg install -y nginx", [("pkg_install", "pkg", ["nginx"])]),
        ("apt-get remove vim", [("pkg_remove", "pkg", ["vim"])]),
        ("pip install requests==2.31 'flask>=3'", [("pkg_install", "pip", ["requests", "flask"])]),
        ("python3 -m pip uninstall -y rich", [("pkg_remove", "pip", ["rich"])]),
        ("npm i -g yarn", [("pkg_install", "npm", ["yarn"])]),
        ("cd ~ && pkg install git && git clone x", [("pkg_install", "pkg", ["git"])]),
        ("FOO=1 sudo pkg install curl", [("pkg_install", "pkg", ["curl"])]),
    ])
    def test_recognised(self, cmd, expected):
        assert changes.parse_package_ops(cmd) == expected

    @pytest.mark.parametrize("cmd", [
        "pip install -U pip",
        "pip install -r requirements.txt",
        "pip install ./local/pkg",
        "npm install lodash",
        "echo pkg install nginx",
        "ls -la",
    ])
    def test_ignored(self, cmd):
        assert changes.parse_package_ops(cmd) == []


class TestJournal:
    def test_only_newly_installed_pkg_names_are_recorded(self, root):
        changes.record_packages(root, "pkg install -y git nginx", "t1", installed=lambda names: {"git"})
        [entry] = changes.read(root)
        assert entry["kind"] == "pkg_install" and entry["packages"] == ["nginx"] and entry["task"] == "t1"
        assert changes.revertable(entry, root)
        assert changes.undo_command(entry) == "pkg uninstall -y nginx"

    def test_removing_something_not_installed_is_not_recorded(self, root):
        changes.record_packages(root, "pkg uninstall -y ghost", installed=lambda names: set())
        assert changes.read(root) == []

    def test_undo_runs_inverse_and_marks_done(self, root):
        changes.record_packages(root, "pip install rich")
        ran = []
        done = changes.revert(changes.read(root), root=root, runner=lambda c: (ran.append(c) or True, "ok"))
        assert ran == ["pip uninstall -y rich"]
        assert done == [("pip:rich", "uninstalled")]
        assert not changes.revertable(changes.read(root)[0], root)

    def test_failed_undo_stays_revertable(self, root):
        changes.record_packages(root, "npm i -g yarn")
        done = changes.revert(changes.read(root), root=root, runner=lambda c: (False, "E404"))
        assert done[0][1].startswith("failed")
        assert changes.revertable(changes.read(root)[0], root)

    def test_package_names_are_shell_quoted(self, root):
        entry = {"kind": "pkg_install", "manager": "pip", "packages": ["ok-name", "bad;rm -rf ~"]}
        assert changes.undo_command(entry) == "pip uninstall -y ok-name"

    def test_kernel_prepare_records(self, root, monkeypatch):
        monkeypatch.setattr(changes, "_dpkg_installed", lambda names: set())
        kernel.prepare("pkg install -y cowsay", task_id="task-9")
        [entry] = changes.read(root)
        assert entry["path"] == "pkg:cowsay" and entry["task"] == "task-9"
