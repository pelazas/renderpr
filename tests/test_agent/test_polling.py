import subprocess

from src.agent.polling import ChangeSession, has_pending_edits, parse_command


class TestParseCommand:
    def test_no_at_renderpr(self):
        assert parse_command("hello world") is None

    def test_plain_review(self):
        assert parse_command("@renderpr review") == {"action": "review", "query": None}

    def test_empty_after_at(self):
        assert parse_command("@renderpr") == {"action": "review", "query": None}

    def test_help_falls_to_review(self):
        assert parse_command("@renderpr help") == {"action": "review", "query": None}

    def test_change_command(self):
        result = parse_command("@renderpr change the button to orange")
        assert result["action"] == "change"
        assert result["query"] == "the button to orange"

    def test_change_with_extra_whitespace(self):
        result = parse_command("  @renderpr change   make it blue  ")
        assert result["action"] == "change"
        assert result["query"] == "make it blue"

    def test_apply_command(self):
        assert parse_command("@renderpr apply") == {"action": "apply", "query": None}

    def test_reject_command(self):
        assert parse_command("@renderpr reject") == {"action": "reject", "query": None}

    def test_unknown_falls_back_to_review(self):
        assert parse_command("@renderpr something random") == {"action": "review", "query": None}

    def test_text_around_at_renderpr(self):
        result = parse_command("some text before @renderpr change the color")
        assert result["action"] == "change"
        assert result["query"] == "the color"


class TestHasPendingEdits:
    def test_clean_repo_returns_false(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        assert not has_pending_edits(str(tmp_path))

    def test_dirty_repo_returns_true(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("dirty")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("modified")
        assert has_pending_edits(str(tmp_path))


class TestChangeSession:
    def test_add_edit_deduplicates(self):
        session = ChangeSession()
        session.add_edit("src/page.tsx")
        session.add_edit("src/page.tsx")
        assert session.edited_files == ["src/page.tsx"]

    def test_add_multiple_files(self):
        session = ChangeSession()
        session.add_edit("src/a.tsx")
        session.add_edit("src/b.tsx")
        assert session.edited_files == ["src/a.tsx", "src/b.tsx"]

    def test_clear(self):
        session = ChangeSession()
        session.add_edit("src/page.tsx")
        session.clear()
        assert session.edited_files == []
