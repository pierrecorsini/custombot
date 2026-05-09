"""
src/skills/builtin/routing.py — Routing rule CRUD skills.

Provides LLM-callable tools for managing message routing rules stored
as YAML frontmatter in .md instruction files:

  • RoutingListSkill   — List all routing rules
  • RoutingAddSkill    — Create a new routing rule with validation
  • RoutingDeleteSkill — Delete a routing rule by ID
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Optional, TYPE_CHECKING

from src.routing import RoutingRule
from src.skills.base import BaseSkill, validate_input
from src.utils.frontmatter import (
    dump_frontmatter,
    extract_routing_rules,
    parse_frontmatter,
)

if TYPE_CHECKING:
    from src.core.instruction_loader import InstructionLoader
    from src.routing import RoutingEngine
    from pathlib import Path


class RoutingListSkill(BaseSkill):
    """List all routing rules from instruction file frontmatter."""

    name = "routing_list"
    description = (
        "List all routing rules that control how incoming messages are "
        "matched to instruction files. Shows rule ID, priority, patterns "
        "(sender, recipient, channel, content), and instruction file."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, routing_engine: RoutingEngine) -> None:
        self._engine = routing_engine

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        rules = self._engine.rules

        if not rules:
            return "No routing rules found."

        lines = ["📋 Routing Rules:\n"]
        for rule in rules:
            status = "✅" if rule.enabled else "❌"
            from_me_str = (
                "true" if rule.fromMe is True else "false" if rule.fromMe is False else "any"
            )
            to_me_str = "true" if rule.toMe is True else "false" if rule.toMe is False else "any"
            lines.append(
                f"{status} **{rule.id}** (priority: {rule.priority})\n"
                f"   • sender: `{rule.sender}`\n"
                f"   • recipient: `{rule.recipient}`\n"
                f"   • channel: `{rule.channel}`\n"
                f"   • content_regex: `{rule.content_regex}`\n"
                f"   • fromMe: `{from_me_str}`\n"
                f"   • toMe: `{to_me_str}`\n"
                f"   • showErrors: `{rule.showErrors}`\n"
                f"   • instruction: `{rule.instruction}`\n"
            )
        return "\n".join(lines)


class RoutingAddSkill(BaseSkill):
    """Create a new routing rule in an instruction file's frontmatter."""

    name = "routing_add"
    description = (
        "Create a new routing rule that matches incoming messages based on "
        "sender, recipient, channel, or content patterns, and routes them to "
        "a specific instruction file. Lower priority values are evaluated first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "priority": {
                "type": "integer",
                "description": (
                    "Priority of the rule (lower = higher priority, evaluated first). "
                    "Must be a non-negative integer."
                ),
            },
            "sender": {
                "type": "string",
                "description": (
                    "Pattern to match sender ID. Use '*' for wildcard, "
                    "or a specific sender identifier."
                ),
            },
            "recipient": {
                "type": "string",
                "description": (
                    "Pattern to match recipient/chat ID. Use '*' for wildcard, "
                    "or a specific chat identifier."
                ),
            },
            "channel": {
                "type": "string",
                "description": (
                    "Pattern to match channel type (e.g., 'whatsapp', 'telegram'). "
                    "Use '*' for wildcard."
                ),
            },
            "content_regex": {
                "type": "string",
                "description": (
                    "Regex pattern to match message content. Use '*' for wildcard, "
                    "or a regex pattern like '^hello.*'."
                ),
            },
            "instruction": {
                "type": "string",
                "description": (
                    "Filename of the instruction file to use when this rule matches. "
                    "Must end with '.md' (e.g., 'support.md', 'sales.md'). "
                    "If the file doesn't exist, it will be created with a basic template."
                ),
            },
            "rule_id": {
                "type": "string",
                "description": (
                    "Optional unique ID for the rule. If not provided, a UUID will be generated."
                ),
            },
            "fromMe": {
                "type": "string",
                "enum": ["true", "false", "any"],
                "description": (
                    "Match messages based on whether they were sent by the bot user. "
                    "'true' = only messages from the bot user, "
                    "'false' = only messages from others, "
                    "'any' = match all messages (default)."
                ),
            },
            "toMe": {
                "type": "string",
                "enum": ["true", "false", "any"],
                "description": (
                    "Match messages based on whether they were sent directly to the bot (private chat). "
                    "'true' = only direct/private messages, "
                    "'false' = only group messages, "
                    "'any' = match all messages (default)."
                ),
            },
            "showErrors": {
                "type": "boolean",
                "description": (
                    "Whether to send error messages to the channel when processing fails. "
                    "Set to false to silently suppress errors (they are still logged). "
                    "Default is true."
                ),
            },
        },
        "required": ["priority", "instruction"],
    }

    def __init__(
        self, routing_engine: RoutingEngine, instruction_loader: InstructionLoader
    ) -> None:
        self._engine = routing_engine
        self._loader = instruction_loader

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        priority: int = 0,
        instruction: str = "",
        sender: str = "*",
        recipient: str = "*",
        channel: str = "*",
        content_regex: str = "*",
        rule_id: Optional[str] = None,
        fromMe: str = "any",
        toMe: str = "any",
        showErrors: bool = True,
        **kwargs: Any,
    ) -> str:
        # Validation: priority must be non-negative integer
        if not isinstance(priority, int) or priority < 0:
            return "❌ Error: priority must be a non-negative integer."

        # Validation: instruction must end with .md
        if not instruction:
            return "❌ Error: instruction is required."
        if not instruction.endswith(".md"):
            return "❌ Error: instruction must end with '.md' (e.g., 'support.md')."

        rule_id = rule_id or str(uuid.uuid4())

        # Convert fromMe string to Optional[bool]
        from_me_value: Optional[bool] = None
        if fromMe == "true":
            from_me_value = True
        elif fromMe == "false":
            from_me_value = False

        # Convert toMe string to Optional[bool]
        to_me_value: Optional[bool] = None
        if toMe == "true":
            to_me_value = True
        elif toMe == "false":
            to_me_value = False

        # Build the routing rule dict for frontmatter
        new_rule_dict: dict[str, Any] = {
            "id": rule_id,
            "priority": priority,
            "sender": sender,
            "recipient": recipient,
            "channel": channel,
            "content_regex": content_regex,
            "enabled": True,
        }
        if from_me_value is not None:
            new_rule_dict["fromMe"] = from_me_value
        if to_me_value is not None:
            new_rule_dict["toMe"] = to_me_value
        if not showErrors:
            new_rule_dict["showErrors"] = False

        try:
            # Read existing file or create new
            raw = self._loader.load_raw(instruction)
            if raw is not None:
                parsed = parse_frontmatter(raw)
                metadata = copy.deepcopy(parsed.metadata)
                body = parsed.content
            else:
                metadata = {}
                body = f"# {instruction.replace('.md', '')}\n\nInstruction file for routing rule `{rule_id}`.\n"

            # Get existing routing rules from metadata
            existing_rules = extract_routing_rules(metadata)

            # Check for duplicate rule ID
            for er in existing_rules:
                if er.get("id") == rule_id:
                    return f"❌ Error: Rule with ID `{rule_id}` already exists in `{instruction}`."

            # Append new rule
            if len(existing_rules) == 0:
                # Single rule (or first rule) — store as dict
                metadata["routing"] = new_rule_dict
            elif len(existing_rules) == 1:
                # Convert from dict to list
                metadata["routing"] = [existing_rules[0], new_rule_dict]
            else:
                existing_rules.append(new_rule_dict)
                metadata["routing"] = existing_rules

            # Write file back
            content = dump_frontmatter(metadata, body)
            self._loader.save(instruction, content)

            # Refresh engine to pick up changes
            self._engine.refresh_rules()

            from_me_str = (
                "true" if from_me_value is True else "false" if from_me_value is False else "any"
            )
            to_me_str = (
                "true" if to_me_value is True else "false" if to_me_value is False else "any"
            )
            return (
                f"✅ Routing rule created successfully.\n"
                f"   • ID: `{rule_id}`\n"
                f"   • Priority: {priority}\n"
                f"   • Sender: `{sender}`\n"
                f"   • Recipient: `{recipient}`\n"
                f"   • Channel: `{channel}`\n"
                f"   • Content regex: `{content_regex}`\n"
                f"   • fromMe: `{from_me_str}`\n"
                f"   • toMe: `{to_me_str}`\n"
                f"   • showErrors: `{showErrors}`\n"
                f"   • Instruction: `{instruction}`"
            )
        except Exception as exc:
            return f"❌ Error creating routing rule: {exc}"


class RoutingDeleteSkill(BaseSkill):
    """Delete a routing rule by ID from instruction file frontmatter."""

    name = "routing_delete"
    description = "Delete a routing rule by its unique ID. This action cannot be undone."
    parameters = {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "The unique ID of the routing rule to delete.",
            },
        },
        "required": ["rule_id"],
    }

    def __init__(
        self, routing_engine: RoutingEngine, instruction_loader: InstructionLoader
    ) -> None:
        self._engine = routing_engine
        self._loader = instruction_loader

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        rule_id: str = "",
        **kwargs: Any,
    ) -> str:
        if not rule_id:
            return "❌ Error: rule_id is required."

        # Find which instruction file contains this rule
        target_instruction = None
        for rule in self._engine.rules:
            if rule.id == rule_id:
                target_instruction = rule.instruction
                break

        if target_instruction is None:
            return f"❌ Error: Routing rule `{rule_id}` not found."

        try:
            raw = self._loader.load_raw(target_instruction)
            if raw is None:
                return f"❌ Error: Instruction file `{target_instruction}` not found."

            parsed = parse_frontmatter(raw)
            metadata = copy.deepcopy(parsed.metadata)
            body = parsed.content

            existing_rules = extract_routing_rules(metadata)

            # Remove the rule with matching ID
            updated_rules = [r for r in existing_rules if r.get("id") != rule_id]

            if len(updated_rules) == len(existing_rules):
                return f"❌ Error: Routing rule `{rule_id}` not found in `{target_instruction}`."

            # Update metadata
            if len(updated_rules) == 0:
                metadata.pop("routing", None)
            elif len(updated_rules) == 1:
                metadata["routing"] = updated_rules[0]
            else:
                metadata["routing"] = updated_rules

            # Write file back
            if metadata:
                content = dump_frontmatter(metadata, body)
            else:
                content = body
            self._loader.save(target_instruction, content)

            # Refresh engine to pick up changes
            self._engine.refresh_rules()

            return f"✅ Routing rule `{rule_id}` deleted successfully."
        except Exception as exc:
            return f"❌ Error deleting routing rule: {exc}"
