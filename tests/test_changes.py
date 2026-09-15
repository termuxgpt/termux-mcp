import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import changes, safety


class TestJournal:

    def setup_method(self):
        self.root = tempfile.mkdtemp(prefix="mcp-changes-")
        self.home = tempfile.mkdtemp(prefix="mcp-home-")

    def teardown_method(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.home, name)

    def write(self, name, text):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read_back(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_records_and_reads_newest_first(self):
        changes.record(self.root, changes.MODIFY, "/a", tool="write")
        changes.record(self.root, changes.DELETE, "/b", tool="delete")
        entries = changes.read(self.root)
        assert [e["path"] for e in entries] == ["/b", "/a"]
        assert entries[0]["kind"] == changes.DELETE
        assert entries[0]["tool"] == "delete"

    def test_since_is_inclusive_of_the_moment_given(self):
        changes.record(self.root, changes.MODIFY, "/old")
        changes.record(self.root, changes.MODIFY, "/new")
        with open(changes.journal_path(self.root), encoding="utf-8") as handle:
            newest = json.loads(handle.readlines()[-1])["ts"]
        assert [e["path"] for e in changes.read(self.root, since=newest)] == ["/new"]

    def test_a_half_written_line_is_skipped(self):
        changes.record(self.root, changes.MODIFY, "/a")
        with open(changes.journal_path(self.root), "a", encoding="utf-8") as handle:
            handle.write('{"ts": "20')
        assert [e["path"] for e in changes.read(self.root)] == ["/a"]

    def test_reverting_a_modification_restores_the_old_contents(self):
        live = self.write("notes.txt", "after")
        snap = self.write("snap.txt", "before")
        changes.record(self.root, changes.MODIFY, live, snapshot=snap)
        done = changes.revert(changes.read(self.root))
        assert done == [(live, "restored")]
        assert self.read_back(live) == "before"

    def test_reverting_a_creation_removes_the_file(self):
        live = self.write("new.txt", "hello")
        changes.record(self.root, changes.CREATE, live)
        changes.revert(changes.read(self.root))
        assert not os.path.exists(live)

    def test_reverting_a_deletion_brings_it_back(self):
        trashed = self.write("trash/gone.txt", "precious")
        live = self.path("gone.txt")
        changes.record(self.root, changes.DELETE, live, trash=trashed)
        changes.revert(changes.read(self.root))
        assert self.read_back(live) == "precious"

    def test_a_created_then_edited_file_is_gone_after_reverting_both(self):
        created = self.write("made.txt", "v1")
        snapshot = self.write("snap.txt", "v2")
        changes.record(self.root, changes.CREATE, created)
        changes.record(self.root, changes.MODIFY, created, snapshot=snapshot)
        changes.revert(changes.read(self.root))
        assert not os.path.exists(created)

    def test_reverting_only_the_newer_change_keeps_the_file(self):
        live = self.write("made.txt", "v2")
        snapshot = self.write("snap.txt", "v1")
        changes.record(self.root, changes.CREATE, live)
        changes.record(self.root, changes.MODIFY, live, snapshot=snapshot)
        changes.revert([changes.read(self.root)[0]])
        assert self.read_back(live) == "v1"

    def test_an_entry_whose_snapshot_is_gone_is_not_revertable(self):
        entry = {"kind": changes.MODIFY, "path": "/x", "snapshot": "/nope"}
        assert changes.revertable(entry) is False
        assert changes.revertable({"kind": changes.CREATE, "path": "/x"}) is True

    def test_reverting_reports_what_it_could_not_do(self):
        gone = {"kind": changes.MODIFY, "path": "/x", "snapshot": "/nope"}
        assert changes.revert([gone]) == [("/x", "nothing to undo")]

    def test_the_revert_itself_is_snapshotted_when_the_caller_can(self):
        live = self.write("notes.txt", "after")
        snap = self.write("snap.txt", "before")
        changes.record(self.root, changes.MODIFY, live, snapshot=snap)
        seen = []
        changes.revert(changes.read(self.root),
                       snapshot_before=lambda path: seen.append(path))
        assert seen == [live]

    def test_summary_names_each_change_and_flags_stale_ones(self):
        changes.record(self.root, changes.MODIFY, "/gone.txt",
                       tool="write", snapshot="/nope", cmd="sed -i s/a/b/ f")
        text = changes.summarise(changes.read(self.root))
        assert "/gone.txt" in text
        assert "(write)" in text
        assert "no longer revertable" in text
        assert "via: sed -i" in text

    def test_summary_of_nothing_says_so(self):
        assert changes.summarise([]) == "Nothing has changed yet."

    def test_the_journal_is_trimmed_and_stays_readable(self):
        original = changes.JOURNAL_MAX_BYTES
        changes.JOURNAL_MAX_BYTES = 200
        try:
            for i in range(60):
                changes.record(self.root, changes.MODIFY, f"/f{i}")
            entries = changes.read(self.root, limit=0)
            assert len(entries) <= changes.JOURNAL_KEEP
            assert entries[0]["path"] == "/f59"
        finally:
            changes.JOURNAL_MAX_BYTES = original


class FakeHandler:

    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key.lower()] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    @property
    def wfile(self):
        return self

    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.text())


class TestRedaction:
    """The journal is read back into the app, the model and the receipt.

    A command can carry a credential inline, and this file outlives the task —
    so what gets written is the command with its secrets taken out, still
    recognisable, never usable.
    """

    def test_bearer_tokens_go(self):
        out = changes.redact('curl -H "Authorization: Bearer sk-abc123" https://x/y')
        assert "sk-abc123" not in out
        assert "curl" in out and "https://x/y" in out

    def test_key_value_secrets_go(self):
        for raw in ["curl https://x?token=abc123",
                    "export API_KEY=abc123",
                    "mysql --password hunter2",
                    "deploy --token abc123"]:
            out = changes.redact(raw)
            assert "abc123" not in out, raw
            assert "hunter2" not in out, raw

    def test_a_password_flag_goes_where_it_can_only_be_one(self):
        out = changes.redact("sshpass -p hunter2 ssh root@host")
        assert "hunter2" not in out
        assert "ssh root@host" in out
        out = changes.redact("curl -u alice:s3cret https://x/y")
        assert "s3cret" not in out
        assert "https://x/y" in out

    def test_a_flag_that_is_not_a_secret_is_left_where_it_is(self):
        # `-p` is a path for mkdir, a port for ssh, and only a password for
        # sshpass. Redacting it everywhere would ruin the ordinary command.
        for raw in ["mkdir -p ~/a/b", "ssh -p 2222 root@host", "ls -la"]:
            assert changes.redact(raw) == raw, raw

    def test_credentials_in_a_url_go(self):
        out = changes.redact("git clone https://user:ghp_secret@github.com/a/b")
        assert "ghp_secret" not in out
        assert "github.com/a/b" in out

    def test_an_honest_command_is_left_alone(self):
        for raw in ["sed -i s/a/b/ notes.txt", "ls -la ~/projects",
                    "pkg install python -y"]:
            assert changes.redact(raw) == raw

    def test_the_journal_stores_the_redacted_form(self):
        root = tempfile.mkdtemp(prefix="mcp-redact-")
        try:
            changes.record(root, changes.MODIFY, "/x",
                           cmd='sshpass -p hunter2 ssh root@host')
            stored = changes.read(root)[0]["cmd"]
            assert "hunter2" not in stored
            assert "ssh root@host" in stored
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestRestoreActions:

    def setup_method(self):
        self.home = tempfile.mkdtemp(prefix="mcp-restore-")
        self.previous = safety.HOME
        safety.HOME = self.home
        self.root = os.path.join(self.home, "termuxGPT")

    def teardown_method(self):
        safety.HOME = self.previous
        shutil.rmtree(self.home, ignore_errors=True)

    def change(self, name, before, after):
        path = os.path.join(self.home, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(before)
        safety.snapshot_before_write(path, tool="write")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(after)
        return path

    def restore(self, data):
        from termux_mcp.handlers.terminal import handle_restore
        handler = FakeHandler()
        handle_restore(handler, data)
        return handler

    def test_list_as_text_names_the_files(self):
        path = self.change("a.txt", "v1", "v2")
        assert path in self.restore({"action": "list"}).text()

    def test_list_as_json_carries_the_structure(self):
        path = self.change("a.txt", "v1", "v2")
        payload = self.restore({"action": "list", "format": "json"}).json()
        entry = payload["changes"][0]
        assert entry["path"] == path
        assert entry["kind"] == changes.MODIFY
        assert entry["tool"] == "write"
        assert entry["revertable"] is True

    def test_json_marks_an_entry_whose_snapshot_is_gone(self):
        path = self.change("a.txt", "v1", "v2")
        for entry in changes.read(self.root):
            os.remove(entry["snapshot"])
        payload = self.restore({"action": "list", "format": "json"}).json()
        assert payload["changes"][0]["revertable"] is False
        assert path in payload["changes"][0]["path"]

    def test_revert_asks_for_confirmation_before_touching_anything(self):
        path = self.change("a.txt", "v1", "v2")
        response = self.restore({"action": "revert"})
        assert response.json()["requires_confirmation"] is True
        assert open(path, encoding="utf-8").read() == "v2"

    def test_revert_puts_the_file_back_once_confirmed(self):
        path = self.change("a.txt", "v1", "v2")
        response = self.restore({"action": "revert", "confirmed": True})
        assert open(path, encoding="utf-8").read() == "v1"
        assert "restored" in response.text()

    def test_revert_can_name_one_file(self):
        first = self.change("a.txt", "a1", "a2")
        second = self.change("b.txt", "b1", "b2")
        self.restore({"action": "revert", "path": first, "confirmed": True})
        assert open(first, encoding="utf-8").read() == "a1"
        assert open(second, encoding="utf-8").read() == "b2"

    def test_reverting_nothing_says_so(self):
        assert "Nothing to revert" in self.restore({"action": "revert"}).text()

    def test_the_backup_restore_still_needs_its_file(self):
        response = self.restore({"target": "home"})
        assert response.status == 400


class TestSafetyWiring:

    def setup_method(self):
        self.home = tempfile.mkdtemp(prefix="mcp-safety-")
        self.previous = safety.HOME
        safety.HOME = self.home
        self.root = os.path.join(self.home, "termuxGPT")

    def teardown_method(self):
        safety.HOME = self.previous
        shutil.rmtree(self.home, ignore_errors=True)

    def target(self, name, text=None):
        path = os.path.join(self.home, name)
        if text is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return path

    def test_a_new_file_is_journalled_as_a_creation(self):
        path = self.target("new.txt")
        assert safety.snapshot_before_write(path, tool="write") is None
        entry = changes.read(self.root)[0]
        assert entry["kind"] == changes.CREATE
        assert entry["path"] == path
        assert entry["tool"] == "write"

    def test_an_existing_file_is_journalled_with_its_snapshot(self):
        path = self.target("old.txt", "v1")
        snap = safety.snapshot_before_write(path, tool="write")
        entry = changes.read(self.root)[0]
        assert entry["kind"] == changes.MODIFY
        assert entry["snapshot"] == snap
        assert os.path.exists(snap)

    def test_a_deletion_is_journalled_with_its_trash_path(self):
        path = self.target("gone.txt", "x")
        dest = safety.trash_path(path, tool="delete")
        entry = changes.read(self.root)[0]
        assert entry["kind"] == changes.DELETE
        assert entry["trash"] == dest
        assert not os.path.exists(path)

    def test_shell_writes_are_journalled_with_the_command(self):
        path = self.target("script.sh", "echo hi\n")
        safety.snapshot_targets_from_command(f"sed -i s/hi/bye/ {path}")
        entry = changes.read(self.root)[0]
        assert entry["path"] == path
        assert entry["tool"] == "run"
        assert entry["cmd"].startswith("sed -i")

    def test_the_safety_area_itself_is_never_journalled(self):
        path = self.target("termuxGPT/snapshots/x/y", "no")
        safety.snapshot_before_write(path)
        assert changes.read(self.root) == []

    def test_the_safety_area_guard_survives_any_spelling(self):
        inside = self.target("termuxGPT/notes.txt", "no")
        spellings = [
            inside,
            inside.replace(os.sep, "/"),
            os.path.join(self.home, "termuxGPT", "snapshots", "..", "notes.txt"),
        ]
        for spelling in spellings:
            assert safety.inside_safety_area(spelling), spelling
            assert safety.snapshot_before_write(spelling) is None, spelling
        assert changes.read(self.root) == []

    def test_a_file_beside_the_safety_area_is_not_inside_it(self):
        outside = self.target("plain.txt", "x")
        assert safety.inside_safety_area(outside) is False
        assert safety.snapshot_before_write(outside, tool="write") is not None
        assert changes.read(self.root)[0]["path"] == outside

    def test_the_revert_recorded_itself_so_it_can_be_undone(self):
        path = self.target("notes.txt", "v1")
        safety.snapshot_before_write(path, tool="write")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("v2")
        changes.revert(changes.read(self.root),
                       snapshot_before=safety.snapshot_before_write)
        assert open(path, encoding="utf-8").read() == "v1"
        changes.revert([changes.read(self.root)[0]], snapshot_before=None)
        assert open(path, encoding="utf-8").read() == "v2"
