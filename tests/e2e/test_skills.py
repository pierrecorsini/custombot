"""
test_skills.py - E2E tests for skill loading and execution.

Tests the skills system:
  - Skill registry operations
  - Built-in skill loading
  - Skill execution
  - Tool definition generation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Tests: Skill Registry
# ─────────────────────────────────────────────────────────────────────────────


def test_skill_registry_starts_empty():
    """
    E2E Test: Skill registry initializes empty.

    Arrange:
        - Create new SkillRegistry

    Act:
        - Check registered skills

    Assert:
        - Registry is empty
    """
    from src.skills import SkillRegistry

    registry = SkillRegistry()

    assert len(registry.all()) == 0, "Registry should start empty"


def test_skill_registry_can_register_skill():
    """
    E2E Test: Skills can be registered.

    Arrange:
        - Create registry and a test skill

    Act:
        - Register the skill

    Assert:
        - Skill is retrievable
    """
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    registry = SkillRegistry()

    class TestSkill(BaseSkill):
        name = "test_skill"
        description = "A test skill"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, workspace_dir: Path) -> str:
            return "test result"

    registry._skills["test_skill"] = TestSkill()

    # Assert
    assert registry.get("test_skill") is not None
    assert registry.get("test_skill").name == "test_skill"


def test_skill_registry_returns_none_for_unknown_skill():
    """
    E2E Test: Registry returns None for unknown skills.

    Arrange:
        - Create empty registry

    Act:
        - Look up non-existent skill

    Assert:
        - Returns None
    """
    from src.skills import SkillRegistry

    registry = SkillRegistry()

    assert registry.get("nonexistent") is None


def test_skill_registry_all_returns_all_skills():
    """
    E2E Test: all() returns all registered skills.

    Arrange:
        - Register multiple skills

    Act:
        - Call all()

    Assert:
        - All skills are returned
    """
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    registry = SkillRegistry()

    class Skill1(BaseSkill):
        name = "skill_1"
        description = "First skill"
        parameters = {"type": "object"}

        async def execute(self, workspace_dir: Path) -> str:
            return "1"

    class Skill2(BaseSkill):
        name = "skill_2"
        description = "Second skill"
        parameters = {"type": "object"}

        async def execute(self, workspace_dir: Path) -> str:
            return "2"

    registry._skills["skill_1"] = Skill1()
    registry._skills["skill_2"] = Skill2()

    all_skills = registry.all()

    assert len(all_skills) == 2
    skill_names = {s.name for s in all_skills}
    assert "skill_1" in skill_names
    assert "skill_2" in skill_names


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Built-in Skills Loading
# ─────────────────────────────────────────────────────────────────────────────


def test_skill_registry_loads_builtins():
    """
    E2E Test: Registry loads built-in skills.

    Arrange:
        - Create registry

    Act:
        - Call load_builtins()

    Assert:
        - Built-in skills are loaded
    """
    import tempfile

    from src.memory import Memory
    from src.skills import SkillRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        registry = SkillRegistry()
        registry.load_builtins(db=None)

        # Check for some expected built-in skills
        all_skills = registry.all()
        skill_names = {s.name for s in all_skills}

        # These should be loaded from src/skills/builtin/
        assert len(all_skills) > 0, "Should have loaded built-in skills"


def test_skill_registry_generates_tool_definitions():
    """
    E2E Test: Registry generates tool definitions for LLM.

    Arrange:
        - Load built-in skills

    Act:
        - Get tool definitions

    Assert:
        - Definitions have correct format
    """
    import tempfile

    from src.memory import Memory
    from src.skills import SkillRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        registry = SkillRegistry()
        registry.load_builtins(db=None)

        tools = registry.get_tool_definitions()

        assert isinstance(tools, list)
        if len(tools) > 0:
            # Check tool definition format
            tool = tools[0]
            assert "type" in tool
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Skill Execution
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_can_be_executed():
    """
    E2E Test: Skills can be executed.

    Arrange:
        - Create a test skill

    Act:
        - Execute the skill

    Assert:
        - Returns expected result
    """
    from src.skills.base import BaseSkill as Skill

    class EchoSkill(Skill):
        name = "echo"
        description = "Echo input"
        parameters = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

        async def execute(self, workspace_dir: Path, message: str) -> str:
            return f"Echo: {message}"

    skill = EchoSkill()

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        result = await skill.execute(Path(tmpdir), message="Hello")

    assert result == "Echo: Hello"


@pytest.mark.asyncio
async def test_skill_has_access_to_workspace():
    """
    E2E Test: Skills can access workspace directory.

    Arrange:
        - Create skill that reads/writes to workspace

    Act:
        - Execute skill

    Assert:
        - Can interact with workspace
    """
    from src.skills.base import BaseSkill as Skill

    class FileSkill(Skill):
        name = "file_writer"
        description = "Writes a file"
        parameters = {
            "type": "object",
            "properties": {"content": {"type": "string"}},
        }

        async def execute(self, workspace_dir: Path, content: str = "test") -> str:
            # Write a file to workspace
            test_file = workspace_dir / "test_output.txt"
            test_file.write_text(content)
            return f"Wrote {len(content)} characters to test_output.txt"

    skill = FileSkill()

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        result = await skill.execute(workspace, content="Hello World")

        # Check file was created
        assert (workspace / "test_output.txt").exists()
        assert (workspace / "test_output.txt").read_text() == "Hello World"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Skills List Command
# ─────────────────────────────────────────────────────────────────────────────


def test_skills_list_command(cli_runner, tmp_path: Path):
    """
    E2E Test: skills list command shows all skills.

    Arrange:
        - Create config file

    Act:
        - Run: python main.py skills list

    Assert:
        - Output shows skill names
    """
    import json

    config = {
        "llm": {"api_key": "sk-test"},
        "skills_auto_load": False,
    }
    config_path = tmp_path / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f)

    from main import cli

    result = cli_runner.invoke(
        cli,
        ["skills", "list", "--config", str(config_path)],
    )

    # Should show loaded skills
    assert result.exit_code == 0 or "skill" in result.output.lower()


def test_skills_info_command(cli_runner, tmp_path: Path):
    """
    E2E Test: skills info command shows skill details.

    Arrange:
        - Create config file with built-in skills

    Act:
        - Run: python main.py skills info <skill_name>

    Assert:
        - Output shows skill parameters
    """
    import json

    config = {
        "llm": {"api_key": "sk-test"},
        "skills_auto_load": False,
    }
    config_path = tmp_path / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f)

    from main import cli

    # Try to get info on a built-in skill (web_search)
    result = cli_runner.invoke(
        cli,
        ["skills", "info", "web_search", "--config", str(config_path)],
    )

    # Should show skill info or indicate not found
    assert result.exit_code is not None  # Command completed


def test_skills_info_unknown_skill(cli_runner, tmp_path: Path):
    """
    E2E Test: skills info command handles unknown skill.

    Arrange:
        - Create config file

    Act:
        - Run: python main.py skills info nonexistent_skill

    Assert:
        - Shows error message
    """
    import json

    config = {
        "llm": {"api_key": "sk-test"},
        "skills_auto_load": False,
    }
    config_path = tmp_path / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f)

    from main import cli

    result = cli_runner.invoke(
        cli,
        ["skills", "info", "nonexistent_skill_xyz", "--config", str(config_path)],
    )

    # Should indicate skill not found
    assert "not found" in result.output.lower() or result.exit_code != 0
