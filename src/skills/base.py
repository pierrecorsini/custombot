"""
skills/base.py — BaseSkill abstract class.

Every skill — whether a built-in Python class or a user-defined
markdown prompt skill — exposes:
  • name          : str   (used as the OpenAI tool function name)
  • description   : str   (shown to the LLM)
  • parameters    : dict  (JSON Schema object for the function args)
  • execute()     : async method that runs the skill

Markdown "prompt skills" (skill.md files, inspired by picoclaw's
workspace/skills/ layout) are wrapped automatically by PromptSkill and
use the LLM itself as the execution engine.

The BaseSkill ABC provides a concrete base class for skill implementations,
while the Skill Protocol in src/protocols.py enables structural subtyping
for any class that implements the required methods and attributes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import cached_property, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, TypeVar, cast

from src.constants import DEFAULT_SKILL_TIMEOUT
from src.exceptions import SkillError

if TYPE_CHECKING:
    from src.llm_provider import LLMProvider
    from src.utils.protocols import Skill

F = TypeVar("F", bound=Callable[..., Any])


# ─────────────────────────────────────────────────────────────────────────────
# Type Validation Helpers
# ─────────────────────────────────────────────────────────────────────────────

_JSON_TYPE_MAP: Dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _validate_type(value: Any, expected_type: str, param_name: str) -> Optional[str]:
    """
    Validate that value matches the expected JSON Schema type.

    Returns None if valid, or an error message string if invalid.
    """
    if expected_type not in _JSON_TYPE_MAP:
        # Unknown type, skip validation
        return None

    expected_types = _JSON_TYPE_MAP[expected_type]
    if not isinstance(value, expected_types):
        actual_type = type(value).__name__
        return f"Parameter '{param_name}' must be {expected_type}, got {actual_type}"

    return None


def _validate_parameters(kwargs: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    """
    Validate kwargs against a JSON Schema parameters object.

    Returns a list of error messages (empty if valid).
    """
    errors: List[str] = []

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Check required parameters
    for req_param in required:
        if req_param not in kwargs or kwargs[req_param] is None:
            errors.append(f"Missing required parameter: '{req_param}'")

    # Validate types for provided parameters
    for param_name, value in kwargs.items():
        # Skip workspace_dir (special parameter)
        if param_name == "workspace_dir":
            continue

        # Skip None values for optional parameters
        if value is None:
            continue

        param_schema = properties.get(param_name)
        if param_schema is None:
            # Parameter not in schema - allow it (flexible)
            continue

        expected_type = param_schema.get("type")
        if expected_type:
            type_error = _validate_type(value, expected_type, param_name)
            if type_error:
                errors.append(type_error)

        # Validate array items if applicable
        if expected_type == "array" and isinstance(value, list):
            items_schema = param_schema.get("items", {})
            items_type = items_schema.get("type")
            if items_type:
                for i, item in enumerate(value):
                    item_error = _validate_type(item, items_type, f"{param_name}[{i}]")
                    if item_error:
                        errors.append(item_error)

    return errors


def validate_input(func: F) -> F:
    """
    Decorator that validates skill input parameters against the skill's schema.

    This decorator should be applied to the execute() method of skill classes.
    It validates:
    - Required parameters are present
    - Parameter types match the schema definition
    - Array item types (when items schema is defined)

    Raises:
        ValidationError: If validation fails with detailed error messages

    Example:
        class MySkill(BaseSkill):
            parameters = {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }

            @validate_input
            async def execute(self, workspace_dir: Path, **kwargs) -> str:
                ...
    """

    @wraps(func)
    async def wrapper(self: "BaseSkill", workspace_dir: Path, **kwargs: Any) -> str:
        # Validate against the skill's parameter schema
        errors = _validate_parameters(kwargs, self.parameters)

        if errors:
            raise SkillError(
                f"Input validation failed for skill '{self.name}'",
                skill=self.name,
                errors=errors,
                reason="validation",
            )

        return await func(self, workspace_dir, **kwargs)

    return cast(F, wrapper)


class BaseSkill(ABC):
    """Base class for all executable tool-skills."""

    #: Tool name exposed to the LLM (must be a valid Python identifier)
    name: str = ""
    #: Human-readable description the LLM uses to decide when to call this tool
    description: str = ""
    #: JSON Schema object describing the function parameters
    # (each subclass gets its own copy via __init_subclass__)
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    #: Per-skill timeout in seconds (overrides DEFAULT_SKILL_TIMEOUT).
    # Subclasses that need more time (e.g. web_research) should set this.
    timeout_seconds: float = DEFAULT_SKILL_TIMEOUT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Give each subclass its own copy of the parameters dict to prevent
        # shared-mutable-state bugs where one skill's mutations leak to others.
        if "parameters" not in cls.__dict__:
            parent = cls.parameters
            cls.parameters = {
                "type": parent.get("type", "object"),
                "properties": dict(parent.get("properties", {})),
                "required": list(parent.get("required", [])),
            }

    @abstractmethod
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        """
        Run the skill.

        All file I/O and subprocess execution MUST use *workspace_dir*
        as the working directory to ensure per-chat isolation.

        Returns a string result that will be fed back to the LLM as a
        tool response.
        """
        ...

    @cached_property
    def tool_definition(self) -> Dict[str, Any]:
        """
        Cached OpenAI tools-array entry for this skill.

        The tool definition is computed once and cached for the lifetime
        of the skill instance, avoiding repeated dict construction.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_tool_definition(self) -> Dict[str, Any]:
        """Deprecated: Use tool_definition property instead."""
        return self.tool_definition

    # ── LLM wiring (self-service) ──────────────────────────────────────────

    def needs_llm(self) -> bool:
        """Return True if this skill requires an LLM client to execute."""
        return False

    def wire_llm(self, llm: "LLMProvider") -> None:
        """Inject the shared LLM client. Override alongside needs_llm()."""
        pass

    def __repr__(self) -> str:
        return f"<Skill {self.name!r}>"
