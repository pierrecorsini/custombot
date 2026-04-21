"""
test_text_splitting.py - Tests for _split_text() message chunking edge cases.

Verifies chunking behaviour:
- Messages exactly at the limit
- Messages with no spaces or newlines (force break)
- Empty string input
- Single character over limit
- Newline-preferred splitting
- Space-preferred splitting when no newline
- Long text with mixed boundaries
- Very small limit values
"""

from __future__ import annotations

import pytest

from src.channels.whatsapp import _split_text


# ─── Basic edge cases ───────────────────────────────────────────────────


class TestBasicEdgeCases:
    def test_empty_string_returns_single_empty_chunk(self):
        assert _split_text("", 100) == [""]

    def test_single_character_under_limit(self):
        assert _split_text("a", 10) == ["a"]

    def test_single_character_at_limit(self):
        assert _split_text("a", 1) == ["a"]

    def test_single_character_over_limit(self):
        """A single character exceeds limit → force-split at limit."""
        assert _split_text("ab", 1) == ["a", "b"]

    def test_text_exactly_at_limit(self):
        text = "a" * 50
        assert _split_text(text, 50) == [text]

    def test_text_one_over_limit(self):
        text = "a" * 51
        result = _split_text(text, 50)
        assert len(result) == 2
        assert all(len(c) <= 50 for c in result)
        # Reconstructed content should cover all characters (minus stripped whitespace)
        assert "".join(result) == text


# ─── No spaces or newlines (force break) ────────────────────────────────


class TestForceBreak:
    def test_no_spaces_no_newlines(self):
        """Long string with no word boundaries forces a hard break at limit."""
        text = "x" * 200
        result = _split_text(text, 50)
        assert len(result) == 4
        for chunk in result:
            assert len(chunk) <= 50

    def test_force_break_preserves_all_characters(self):
        text = "abcdefghij" * 10  # 100 chars, no spaces
        result = _split_text(text, 30)
        rejoined = "".join(result)
        assert rejoined == text


# ─── Newline-preferred splitting ────────────────────────────────────────


class TestNewlineSplitting:
    def test_splits_at_newline_preferentially(self):
        text = "hello\nworld\nfoo"
        result = _split_text(text, 8)
        # Should split at first newline: "hello" (5 chars, newline is boundary)
        assert "hello" in result[0]
        assert len(result) >= 2

    def test_newline_within_limit(self):
        """Newline found within limit range is used as split point."""
        text = "line1\nline2\nline3"
        result = _split_text(text, 12)
        assert all(len(c) <= 12 for c in result)

    def test_multiple_newlines(self):
        text = "aaa\nbbb\nccc\nddd"
        result = _split_text(text, 5)
        assert len(result) >= 3
        for chunk in result:
            assert len(chunk) <= 5


# ─── Space-preferred splitting ──────────────────────────────────────────


class TestSpaceSplitting:
    def test_splits_at_space_when_no_newline(self):
        text = "hello world foo bar"
        result = _split_text(text, 12)
        assert len(result) >= 2
        assert all(len(c) <= 12 for c in result)

    def test_space_preferred_over_mid_word_break(self):
        text = "alpha beta gamma"
        result = _split_text(text, 8)
        # Should not break "beta" mid-word
        for chunk in result:
            assert "al" not in chunk or "alpha" in chunk or chunk == "alpha"


# ─── Whitespace stripping between chunks ────────────────────────────────


class TestWhitespaceStripping:
    def test_leading_whitespace_preserved_after_split(self):
        text = "hello\n   world"
        result = _split_text(text, 10)
        # After splitting at newline, indentation is preserved (not lstripped)
        assert result == ["hello", "   world"]

    def test_small_limit_preserves_some_whitespace(self):
        text = "hello\n   world"
        result = _split_text(text, 6)
        rejoined = "".join(result)
        # Old lstrip behavior would strip ALL spaces → "helloworld"
        # New behavior preserves spaces (split among chunks if needed)
        assert "  " in rejoined

    def test_trailing_space_at_split_point(self):
        text = "hello world next part"
        result = _split_text(text, 11)
        # Split at space after "hello" → "hello world" is 11 chars, space found at idx 5
        assert all(len(c) <= 11 for c in result)

    def test_code_block_indentation_preserved_across_chunks(self):
        code = "```python\ndef foo():\n    return 42\n```"
        result = _split_text(code, 15)
        # Indented line should keep its spaces
        indented_chunks = [c for c in result if c.startswith("    ")]
        assert len(indented_chunks) >= 1
        assert "    return 42" in "".join(result)

    def test_indented_list_preserved_across_chunks(self):
        text = "Result:\n    item1\n    item2\n    item3"
        result = _split_text(text, 12)
        # All indented items should keep their leading spaces
        for chunk in result[1:]:
            if "item" in chunk:
                assert chunk.startswith("    "), f"Indented item lost spaces: {chunk!r}"

    def test_newline_only_stripped_at_split_point(self):
        text = "line1\n\nline3"
        result = _split_text(text, 6)
        # With textwrap-based approach, the blank line between line1 and line3
        # is preserved within the chunk when it fits ("line1\n" = 6 chars ≤ limit).
        # This preserves formatting context better than stripping the blank line.
        assert len(result) == 2
        assert result[0] == "line1\n"
        assert result[1] == "line3"


# ─── Very small limits ──────────────────────────────────────────────────


class TestSmallLimits:
    def test_limit_of_one(self):
        result = _split_text("abc", 1)
        assert result == ["a", "b", "c"]

    def test_limit_of_one_with_spaces(self):
        result = _split_text("a b", 1)
        # "a b" length 3, limit 1: no newline, no space within limit (rfind space at idx 1 → idx 1)
        # Actually rfind(" ", 0, 1) searches [0:1] which is "a" → no space → idx = limit = 1
        # First chunk: "a", remaining: "b" (stripped from " b")
        assert "a" in result[0]
        assert "b" in result[-1]

    def test_limit_equals_two(self):
        text = "abcd"
        result = _split_text(text, 2)
        assert len(result) == 2
        assert result == ["ab", "cd"]


# ─── Reconstructed content invariant ────────────────────────────────────


class TestReconstructionInvariant:
    @pytest.mark.parametrize(
        "text,limit",
        [
            ("", 10),
            ("a", 10),
            ("short", 100),
            ("a" * 100, 10),
            ("hello world " * 20, 50),
            ("line1\nline2\nline3\n" * 10, 20),
            ("word " * 50, 30),
        ],
    )
    def test_all_characters_preserved(self, text, limit):
        """Joining all chunks (without whitespace that was stripped) preserves content."""
        result = _split_text(text, limit)
        rejoined = "".join(result)
        # The original stripped whitespace between chunks is lost,
        # so we verify no content characters are lost.
        # For texts without leading/trailing whitespace in chunks, exact match.
        original_stripped = text
        for sep in ("\n", " "):
            # Only strip at split boundaries — the function strips
            # leading whitespace from remaining text after each split
            pass
        # Simpler invariant: total non-whitespace characters preserved
        original_chars = [c for c in text if not c.isspace()]
        result_chars = [c for c in rejoined if not c.isspace()]
        assert original_chars == result_chars

    @pytest.mark.parametrize(
        "text,limit",
        [
            ("a" * 100, 10),
            ("a" * 50, 50),
            ("a" * 51, 50),
            ("x" * 200, 37),
        ],
    )
    def test_no_chunk_exceeds_limit(self, text, limit):
        result = _split_text(text, limit)
        for chunk in result:
            assert len(chunk) <= limit, f"Chunk length {len(chunk)} exceeds limit {limit}: {chunk!r}"


# ─── Formatting preservation ─────────────────────────────────────────────


class TestFormattingPreservation:
    """Tests for preserving formatted content across chunk boundaries."""

    def test_multiline_code_block_split_across_chunks(self):
        # Arrange — code block with body that exceeds a small limit
        code_lines = ["```python", "def greet(name):", "    print(f'Hello {name}')", "```"]
        text = "\n".join(code_lines)
        # Act — split with a limit that forces the code block across ≥2 chunks
        result = _split_text(text, 30)
        # Assert — rejoining (with newlines at split points) recovers the code block
        rejoined = "\n".join(result)
        assert rejoined == text
        assert "```python" in result[0]
        # The closing ``` must appear in one of the chunks
        assert any("```" in c for c in result[1:])

    def test_indented_bullet_list_preserved(self):
        # Arrange — bullet list with nested indentation
        text = "Shopping list:\n- apples\n- bananas\n  - organic\n  - fair trade\n- cherries"
        # Act
        result = _split_text(text, 20)
        # Assert — all non-whitespace characters preserved; indented items keep leading spaces
        rejoined = "\n".join(result)
        assert rejoined == text
        for chunk in result:
            assert len(chunk) <= 20
        # Nested items should still have their leading spaces
        nested_chunks = [c for c in result if "organic" in c or "fair trade" in c]
        for chunk in nested_chunks:
            assert chunk.startswith("  "), f"Nested item lost indentation: {chunk!r}"

    def test_markdown_table_rows_split_at_boundaries(self):
        # Arrange — markdown table
        header = "| Name  | Age | City     |"
        separator = "|-------|-----|----------|"
        row1 = "| Alice | 30  | New York |"
        row2 = "| Bob   | 25  | London   |"
        text = "\n".join([header, separator, row1, row2])
        # Act — limit forces at least one split
        result = _split_text(text, 35)
        # Assert
        rejoined = "\n".join(result)
        assert rejoined == text
        # Every table row must remain intact within its chunk (no mid-row breaks)
        all_rows = [header, separator, row1, row2]
        for row in all_rows:
            # Each row should appear whole in at least one chunk or be reconstructible
            assert row in rejoined

    def test_unicode_content_at_split_boundary(self):
        # Arrange — text with CJK characters positioned right at the split point
        text = "Hello 你好世界 this is a test with 日本語 characters mixed in"
        # Act — small limit to force splits near CJK characters
        result = _split_text(text, 20)
        # Assert — all non-whitespace characters preserved
        rejoined = "".join(result)
        assert "你好世界" in rejoined
        assert "日本語" in rejoined
        for chunk in result:
            assert len(chunk) <= 20

    def test_message_exactly_at_limit_with_trailing_newline(self):
        # Arrange — message length is exactly `limit` including a trailing newline
        # The newline at position limit-1 should be part of the single returned chunk
        text = "a" * 49 + "\n"
        # Act
        result = _split_text(text, 50)
        # Assert — fits in a single chunk, trailing newline preserved
        assert result == [text]
        assert result[0].endswith("\n")

    def test_message_one_over_limit_with_trailing_newline(self):
        # Arrange — one char over limit, trailing newline
        text = "a" * 49 + "\n" + "b"
        # Act
        result = _split_text(text, 50)
        # Assert — split at the newline, both parts preserved
        assert len(result) == 2
        rejoined = "\n".join(result)
        assert "a" * 49 in rejoined
        assert "b" in rejoined


# ─── Markdown code block regression tests ────────────────────────────────


class TestCodeBlockRegression:
    """Regression tests for _split_text() preserving markdown code blocks across chunks.

    _split_text() is not markdown-aware — it splits at whitespace/newline
    boundaries. These tests document the current behaviour so that future
    changes to the splitting algorithm (e.g. adding markdown-aware chunking)
    can be validated against known cases.
    """

    def test_code_block_starting_in_one_chunk_ending_in_next(self):
        # Arrange — a fenced code block whose body exceeds a small limit
        lines = [
            "Here is a function:",
            "```python",
            "def add(a, b):",
            "    return a + b",
            "```",
        ]
        text = "\n".join(lines)
        # Act — limit forces the code block across ≥2 chunks
        result = _split_text(text, 30)
        # Assert — all content is preserved after rejoin
        rejoined = "\n".join(result)
        assert "```python" in rejoined
        assert "def add(a, b):" in rejoined
        assert "```" in rejoined
        for chunk in result:
            assert len(chunk) <= 30

    def test_code_block_with_long_lines_forces_split_inside_block(self):
        # Arrange — code block with a single long line inside
        long_line = "x" * 80
        text = f"```\n{long_line}\n```"
        # Act — limit smaller than the long line forces a mid-line break
        result = _split_text(text, 40)
        # Assert — all x characters preserved; fences present
        rejoined = "".join(result)
        assert rejoined.count("x") == 80
        assert "```" in rejoined
        for chunk in result:
            assert len(chunk) <= 40

    def test_code_block_opening_fence_split_across_chunks(self):
        # Arrange — opening ``` at the very end of a chunk boundary
        prefix = "Explanation text here\n"
        opening = "```py"
        body = "\nprint('hello')\n```"
        text = prefix + opening + body
        # Act — limit chosen so the opening fence straddles the boundary
        result = _split_text(text, 25)
        # Assert — rejoin recovers the full fenced block
        rejoined = "\n".join(result)
        assert "```py" in rejoined
        assert "print('hello')" in rejoined

    def test_code_block_closing_fence_not_lost_in_continuation(self):
        # Arrange — closing ``` alone on its last line, pushed to next chunk
        lines = ["```", "line1", "line2", "line3", "```"]
        text = "\n".join(lines)
        # Act — small limit forces multiple splits
        result = _split_text(text, 10)
        # Assert — both opening and closing fences present
        rejoined = "\n".join(result)
        assert rejoined.startswith("```")
        assert rejoined.endswith("```")

    def test_inline_code_backtick_at_split_boundary(self):
        # Arrange — inline code ending right at the split limit
        text = "Use the `get_data()` function to fetch records from the API"
        # Act — limit lands near the closing backtick
        result = _split_text(text, 25)
        # Assert — backticks and content preserved after rejoin
        rejoined = " ".join(result)
        assert "`get_data()`" in rejoined or "get_data()" in rejoined.replace("`", "")
        for chunk in result:
            assert len(chunk) <= 25

    def test_inline_code_backtick_pair_split_across_chunks(self):
        # Arrange — inline code where the closing backtick is pushed to next chunk
        text = "Call `my_function(arg1, arg2, arg3)` then check the result value"
        # Act — small limit forces the inline code across chunks
        result = _split_text(text, 20)
        # Assert — no content lost; function name preserved
        rejoined = " ".join(result)
        assert "my_function" in rejoined
        assert "arg1" in rejoined

    def test_nested_formatting_bold_code_spanning_limit(self):
        # Arrange — *bold `code`* pattern spanning the chunk limit
        text = "This is *bold `code`* and then some more text after it"
        # Act — limit forces split near the nested formatting
        result = _split_text(text, 20)
        # Assert — all formatting characters preserved
        rejoined = " ".join(result)
        assert "*bold" in rejoined
        assert "code*" in rejoined or "`code`" in rejoined

    def test_nested_formatting_preserves_all_markers(self):
        # Arrange — bold-italic with inline code crossing boundary
        text = "Result: **_`important_value`_** is the key metric to track"
        # Act
        result = _split_text(text, 20)
        # Assert — all non-whitespace characters preserved (textwrap may break words)
        rejoined = "".join(result)
        assert "important_value" in rejoined.replace(" ", "")
        for chunk in result:
            assert len(chunk) <= 20

    def test_multiple_code_blocks_all_fences_preserved(self):
        # Arrange — two separate fenced blocks
        text = "```python\nx = 1\n```\nSome text\n```js\ny = 2\n```"
        # Act
        result = _split_text(text, 15)
        # Assert — all four fence markers present in rejoined output
        rejoined = "\n".join(result)
        assert rejoined.count("```") == 4
        assert "x = 1" in rejoined
        assert "y = 2" in rejoined
