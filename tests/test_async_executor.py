"""
Tests for src/utils/async_executor.py

Unit tests for the AsyncExecutor class that handles subprocess execution
with timeout, logging, and proper error handling.
"""

import asyncio
import sys
import pytest

from src.utils.async_executor import AsyncExecutor, ExecutorResult


class TestExecutorResult:
    """Tests for ExecutorResult dataclass."""

    def test_result_creation(self):
        """Test basic ExecutorResult creation."""
        result = ExecutorResult(
            stdout="output",
            stderr="",
            return_code=0,
            success=True,
            timed_out=False,
        )
        assert result.stdout == "output"
        assert result.success is True
        assert result.timed_out is False

    def test_result_failure(self):
        """Test ExecutorResult for failed execution."""
        result = ExecutorResult(
            stdout="",
            stderr="error message",
            return_code=1,
            success=False,
            timed_out=False,
        )
        assert result.success is False
        assert result.return_code == 1


class TestAsyncExecutor:
    """Tests for AsyncExecutor class."""

    @pytest.fixture
    def executor(self):
        """Create a default AsyncExecutor instance."""
        return AsyncExecutor(timeout=5.0)

    @pytest.mark.asyncio
    async def test_simple_command(self, executor):
        """Test running a simple command."""
        result = await executor.run([sys.executable, "-c", "print('hello')"])
        assert result.success is True
        assert "hello" in result.stdout
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_command_with_exit_code(self, executor):
        """Test command that returns non-zero exit code."""
        result = await executor.run([sys.executable, "-c", "import sys; sys.exit(42)"])
        assert result.success is False
        assert result.return_code != 0

    @pytest.mark.asyncio
    async def test_shell_mode(self, executor):
        """Test shell mode execution."""
        result = await executor.run(
            f'"{sys.executable}" -c "print(\'hello world\')"', shell=True
        )
        assert result.success is True
        assert "hello world" in result.stdout

    @pytest.mark.asyncio
    async def test_timeout(self):
        """Test command timeout handling."""
        executor = AsyncExecutor(timeout=0.1)
        result = await executor.run(
            [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        assert result.timed_out is True
        assert result.success is False
        assert "timed out" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_custom_timeout(self, executor):
        """Test overriding default timeout."""
        result = await executor.run(
            [sys.executable, "-c", "print('test')"], timeout=1.0
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_command_not_found(self, executor):
        """Test handling of non-existent command."""
        result = await executor.run(["nonexistent_command_xyz"])
        assert result.success is False
        assert "not found" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_cwd_parameter(self, executor, tmp_path):
        """Test working directory parameter."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = await executor.run(
            [sys.executable, "-c", "import os; print(os.listdir('.'))"],
            cwd=str(tmp_path),
        )
        assert result.success is True
        assert "test.txt" in result.stdout

    @pytest.mark.asyncio
    async def test_stderr_capture(self, executor):
        """Test stderr is captured properly."""
        # Command that writes to stderr
        result = await executor.run(
            ["python", "-c", "import sys; sys.stderr.write('error')"]
        )
        assert "error" in result.stderr

    @pytest.mark.asyncio
    async def test_string_command_with_exec_mode(self, executor):
        """Test string command is converted to list in exec mode."""
        result = await executor.run(sys.executable)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_default_timeout_used(self):
        """Test that default timeout is applied when not specified."""
        executor = AsyncExecutor(timeout=2.0)
        result = await executor.run([sys.executable, "-c", "print('test')"])
        assert result.success is True

    @pytest.mark.asyncio
    async def test_shell_pipe(self, executor):
        """Test shell mode with pipe."""
        result = await executor.run(
            f'"{sys.executable}" -c "print(\'hello\')" | "{sys.executable}" -c "import sys; print(sys.stdin.read().upper())"',
            shell=True,
        )
        assert result.success is True
        assert "HELLO" in result.stdout


class TestAsyncExecutorEdgeCases:
    """Edge case tests for AsyncExecutor."""

    @pytest.mark.asyncio
    async def test_empty_command_list(self):
        """Test handling of empty command list."""
        executor = AsyncExecutor()
        result = await executor.run([])
        assert result.success is False

    @pytest.mark.asyncio
    async def test_unicode_output(self):
        """Test handling of unicode in output."""
        executor = AsyncExecutor()
        # Set PYTHONIOENCODING for proper unicode handling on Windows
        result = await executor.run(
            ["python", "-c", "print('Hello 世界 🌍')"],
            env={"PYTHONIOENCODING": "utf-8"},
        )
        assert result.success is True
        assert "世界" in result.stdout

    @pytest.mark.asyncio
    async def test_large_output(self):
        """Test handling of large output."""
        executor = AsyncExecutor(timeout=10.0)
        # Generate large output
        result = await executor.run(["python", "-c", "print('x' * 100000)"])
        assert result.success is True
        assert len(result.stdout) >= 100000
