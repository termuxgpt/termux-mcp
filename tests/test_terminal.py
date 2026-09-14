import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp.terminal import (interactive_program, strip_ansi,
                                 trim_to_char_boundary)

ESC = "\x1b"


class TestStripAnsi:

    def test_plain_text_is_untouched(self):
        assert strip_ansi("hello world") == "hello world"

    def test_keeps_tabs_and_newlines(self):
        assert strip_ansi("a\tb\nc") == "a\tb\nc"

    def test_keeps_unicode(self):
        text = "héllo 世界 🎉 ─│┌┐"
        assert strip_ansi(text) == text

    def test_csi_colour(self):
        assert strip_ansi(f"{ESC}[0;32mgreen{ESC}[0m") == "green"

    def test_csi_truecolor(self):
        assert strip_ansi(f"{ESC}[38;2;255;100;20morange{ESC}[0m") == "orange"

    def test_csi_cursor_movement(self):
        assert strip_ansi(f"{ESC}[12;20Hx") == "x"

    def test_csi_erase(self):
        assert strip_ansi(f"{ESC}[2J{ESC}[Hclean") == "clean"

    def test_csi_private_mode(self):
        # ?25l hides the cursor, ?1049h switches to the alternate screen.
        assert strip_ansi(f"{ESC}[?25l{ESC}[?1049htext{ESC}[?25h") == "text"

    def test_charset_designation(self):
        # ESC ( B selects the ASCII charset. This is a three-byte sequence and
        # the pattern only matched two, so the "(B" survived and reached the
        # model — it is the "m\Gb(B (B" that came back from cmatrix.
        assert strip_ansi(f"{ESC}(Bscreen") == "screen"
        assert strip_ansi(f"{ESC})0line{ESC}(B") == "line"

    def test_other_esc_intermediates(self):
        assert strip_ansi(f"{ESC}#8fill") == "fill"      # DECALN
        assert strip_ansi(f"{ESC}%Gutf8") == "utf8"      # UTF-8 select

    def test_single_byte_escapes(self):
        assert strip_ansi(f"{ESC}M{ESC}7{ESC}8{ESC}=x") == "x"

    def test_osc_with_bel(self):
        assert strip_ansi(f"{ESC}]0;window title\x07body") == "body"

    def test_osc_with_st(self):
        assert strip_ansi(f"{ESC}]0;window title{ESC}\\body") == "body"

    def test_dcs(self):
        assert strip_ansi(f"{ESC}P1;2;3qpayload{ESC}\\after") == "after"

    def test_c0_controls(self):
        assert strip_ansi("a\x00b\x07c\x7fd") == "abcd"

    def test_a_realistic_full_screen_frame(self):
        # What a curses program writes for one redraw.
        frame = (
            f"{ESC}[?1049h{ESC}[2J{ESC}[H"
            f"{ESC}[1;32m  MENU  {ESC}[0m\n"
            f"{ESC}[1;37m  1) Website lookup{ESC}[0m\n"
            f"{ESC}[12;1H{ESC}[?25l"
        )
        out = strip_ansi(frame)
        assert "MENU" in out
        assert "1) Website lookup" in out
        assert ESC not in out

    def test_no_escape_survives_anywhere(self):
        # A blind sweep: every sequence above, concatenated.
        blob = (
            f"{ESC}[0m{ESC}[?25l{ESC}(B{ESC})0{ESC}#8{ESC}%G{ESC}M"
            f"{ESC}]0;t\x07{ESC}Pq\x1b\\{ESC}[38;2;1;2;3m"
        )
        out = strip_ansi(blob)
        assert ESC not in out, f"escape survived: {out!r}"


class TestTrimToCharBoundary:

    def test_ascii_untouched(self):
        assert trim_to_char_boundary(b"abc") == b"abc"

    def test_drops_leading_continuation_bytes(self):
        # A ring window can start mid-character.
        assert trim_to_char_boundary(b"\x80\x80abc") == b"abc"
        assert trim_to_char_boundary(b"\xbf\xbf\xbfabc") == b"abc"

    def test_keeps_a_lead_byte_at_the_front(self):
        # 0xC3 is a lead byte, not a continuation — it starts a character.
        assert trim_to_char_boundary(b"\xc3\xa9abc") == b"\xc3\xa9abc"

    def test_empty(self):
        assert trim_to_char_boundary(b"") == b""

    def test_result_never_starts_mid_character(self):
        # The contract is about the front only: a ring window can begin part
        # way through a character, so the first byte must be ASCII or a lead
        # byte, never a continuation byte. A window can still be truncated at
        # the far end — that is the caller's problem, and strip_ansi decodes
        # with errors="replace" for it.
        for data in (b"\x80abc", b"\xe2\x82ac", b"\xf0\x9f\x8e\x89x", b"\xc3\xa9"):
            out = trim_to_char_boundary(data)
            if out:
                assert not (0x80 <= out[0] < 0xC0), f"starts mid-character: {out!r}"


class TestInteractiveProgram:

    def test_flags_things_needing_a_tty(self):
        assert interactive_program("cmatrix") == "cmatrix"
        assert interactive_program("vim notes.txt") == "vim"
        assert interactive_program("sudo vim /etc/hosts") == "vim"
        assert interactive_program("timeout 3 cmatrix") == "cmatrix"
        assert interactive_program("cd /tmp && vim x.txt") == "vim"
        assert interactive_program("echo hi | less") == "less"
        assert interactive_program("sqlite3 mydb.db") == "sqlite3"

    def test_repl_only_when_it_would_be_a_prompt(self):
        assert interactive_program("python3") == "python3"
        assert interactive_program("node") == "node"
        assert interactive_program("python3 -i") == "python3"
        # These run and exit — flagging them would send the agent at a
        # terminal for an ordinary script.
        assert interactive_program("python3 script.py") is None
        assert interactive_program('python3 -c "print(1)"') is None
        assert interactive_program("python3 -m http.server") is None
        assert interactive_program("node app.js") is None

    def test_installs_are_left_alone(self):
        # The install half of "install X and run it" must stay on run().
        assert interactive_program("pkg install cmatrix -y") is None
        assert interactive_program("apt update && apt install -y cmatrix") is None
        assert interactive_program("pip install flask") is None

    def test_ordinary_commands(self):
        assert interactive_program("ls -la") is None
        assert interactive_program("git status") is None
        assert interactive_program("grep -r cmatrix .") is None
        assert interactive_program("") is None
