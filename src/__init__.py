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

from src.config import Config, load_config, save_config, CONFIG_PATH
from src.bot import Bot
from src.utils.protocols import Channel, Skill, Storage, MessageHandler

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
