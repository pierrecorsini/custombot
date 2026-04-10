"""
type_guards.py — Runtime type guards for static type narrowing.

Provides TypeGuard functions for common types used throughout the codebase.
These functions enable both runtime type checking and static type narrowing
with mypy, improving type safety without sacrificing runtime validation.

Usage:
    from src.utils.type_guards import is_incoming_message, is_valid_config, is_routing_rule

    def process(obj: object) -> None:
        if is_incoming_message(obj):
            # obj is narrowed to IncomingMessage for mypy
            print(obj.text)  # Type-safe access
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, TypeGuard

from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig, WhatsAppConfig, NeonizeConfig
from src.routing import RoutingRule


# ─────────────────────────────────────────────────────────────────────────────
# IncomingMessage Type Guard
# ─────────────────────────────────────────────────────────────────────────────


def is_incoming_message(obj: Any) -> TypeGuard[IncomingMessage]:
    """
    Type guard for IncomingMessage dataclass.

    Validates that obj is an IncomingMessage instance with all required
    fields present and of the correct type.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid IncomingMessage, False otherwise.
        When True, narrows type to IncomingMessage for static checkers.
    """
    if not isinstance(obj, IncomingMessage):
        return False

    # Validate required string fields are non-empty strings
    required_strings = ("message_id", "chat_id", "sender_id", "sender_name", "text")
    for field_name in required_strings:
        value = getattr(obj, field_name, None)
        if not isinstance(value, str) or not value:
            return False

    # Validate timestamp is a float/int
    timestamp = getattr(obj, "timestamp", None)
    if not isinstance(timestamp, (int, float)):
        return False

    # Validate boolean fields
    if not isinstance(getattr(obj, "fromMe", None), bool):
        return False
    if not isinstance(getattr(obj, "toMe", None), bool):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Config Type Guards
# ─────────────────────────────────────────────────────────────────────────────


def is_llm_config(obj: Any) -> TypeGuard[LLMConfig]:
    """
    Type guard for LLMConfig dataclass.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid LLMConfig instance.
    """
    if not isinstance(obj, LLMConfig):
        return False

    # Validate required fields
    if not isinstance(obj.model, str) or not obj.model:
        return False
    if not isinstance(obj.base_url, str):
        return False
    if not isinstance(obj.temperature, (int, float)):
        return False
    if obj.max_tokens is not None and not isinstance(obj.max_tokens, int):
        return False
    if not isinstance(obj.timeout, (int, float)):
        return False
    if not isinstance(obj.system_prompt_prefix, str):
        return False
    if not isinstance(obj.max_tool_iterations, int):
        return False

    return True


def is_neonize_config(obj: Any) -> TypeGuard[NeonizeConfig]:
    """
    Type guard for NeonizeConfig dataclass.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid NeonizeConfig instance.
    """
    if not isinstance(obj, NeonizeConfig):
        return False

    if not isinstance(obj.db_path, str):
        return False

    return True


def is_whatsapp_config(obj: Any) -> TypeGuard[WhatsAppConfig]:
    """
    Type guard for WhatsAppConfig dataclass.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid WhatsAppConfig instance.
    """
    if not isinstance(obj, WhatsAppConfig):
        return False

    if not isinstance(obj.provider, str):
        return False
    if not is_neonize_config(obj.neonize):
        return False
    if not isinstance(obj.allowed_numbers, list):
        return False
    if not all(isinstance(n, str) for n in obj.allowed_numbers):
        return False

    return True


def is_valid_config(obj: Any) -> TypeGuard[Config]:
    """
    Type guard for Config dataclass.

    Validates that obj is a Config instance with all required nested
    configs properly initialized and valid.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid Config instance, False otherwise.
        When True, narrows type to Config for static checkers.
    """
    if not isinstance(obj, Config):
        return False

    # Validate nested configs
    if not is_llm_config(obj.llm):
        return False
    if not is_whatsapp_config(obj.whatsapp):
        return False

    # Validate root-level fields
    if not isinstance(obj.memory_max_history, int):
        return False
    if not isinstance(obj.skills_auto_load, bool):
        return False
    if not isinstance(obj.skills_user_directory, str):
        return False
    if not isinstance(obj.log_incoming_messages, bool):
        return False
    if not isinstance(obj.log_routing_info, bool):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# RoutingRule Type Guard
# ─────────────────────────────────────────────────────────────────────────────


def is_routing_rule(obj: Any) -> TypeGuard[RoutingRule]:
    """
    Type guard for RoutingRule dataclass.

    Validates that obj is a RoutingRule instance with all required
    fields present and of the correct type.

    Args:
        obj: Any object to check.

    Returns:
        True if obj is a valid RoutingRule, False otherwise.
        When True, narrows type to RoutingRule for static checkers.
    """
    if not isinstance(obj, RoutingRule):
        return False

    # Validate required string fields
    required_strings = (
        "id",
        "sender",
        "recipient",
        "channel",
        "content_regex",
        "instruction",
    )
    for field_name in required_strings:
        value = getattr(obj, field_name, None)
        if not isinstance(value, str):
            return False

    # Validate priority is an int
    if not isinstance(obj.priority, int):
        return False

    # Validate enabled is a bool
    if not isinstance(obj.enabled, bool):
        return False

    # Validate optional boolean fields (can be None, True, or False)
    if obj.fromMe is not None and not isinstance(obj.fromMe, bool):
        return False
    if obj.toMe is not None and not isinstance(obj.toMe, bool):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Generic Dataclass Validation Helper
# ─────────────────────────────────────────────────────────────────────────────


def is_dataclass_of_type(obj: Any, cls: type) -> TypeGuard[Any]:
    """
    Generic type guard for dataclass instances of a specific type.

    Performs runtime validation that obj is an instance of the specified
    dataclass type with all fields present.

    Args:
        obj: Any object to check.
        cls: The expected dataclass type.

    Returns:
        True if obj is an instance of cls with valid fields.
    """
    if not is_dataclass(obj) or isinstance(obj, type):
        return False
    if type(obj) is not cls:
        return False

    # Verify all fields exist
    for field in fields(cls):
        if not hasattr(obj, field.name):
            return False

    return True
