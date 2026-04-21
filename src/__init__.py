"""
src/ — Core application modules.

Modules:
    config — Configuration dataclasses with JSON loading/saving
    db — File-based persistence layer
    llm — OpenAI-compatible async LLM client
    memory — Per-chat persistent memory
    protocols — Protocol definitions for structural subtyping
    routing — Message routing engine
    bot — Core bot orchestrator (ReAct loop)
"""

from src.bot import Bot
from src.config import CONFIG_PATH, Config, load_config, save_config
from src.utils.protocols import Channel, MessageHandler, Skill, Storage

__all__ = [
    "Config",
    "load_config",
    "save_config",
    "CONFIG_PATH",
    "Bot",
    "Channel",
    "Skill",
    "Storage",
    "MessageHandler",
]
