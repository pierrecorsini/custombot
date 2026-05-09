"""
src.memory — Structured memory subsystem.

Provides episodic memory, decay scoring, cross-chat sharing,
consolidation, working memory, semantic graph, hybrid retrieval,
preferences, versioning, and context budget allocation on top of the
per-chat MEMORY.md layer in ``src.memory`` (top-level module).
"""

from src.memory.episodic import EpisodicMemory
from src.memory.decay import MemoryDecayManager
from src.memory.cross_chat import CrossChatMemory
from src.memory.consolidation import MemoryConsolidationJob
from src.memory.working import WorkingMemory
from src.memory.semantic_graph import SemanticMemoryGraph
from src.memory.hybrid_retrieval import HybridRetriever
from src.memory.preferences import PreferenceLearner
from src.memory.versioning import MemoryVersionManager
from src.memory.budget import ContextBudgetAllocator

__all__ = [
    "EpisodicMemory",
    "MemoryDecayManager",
    "CrossChatMemory",
    "MemoryConsolidationJob",
    "WorkingMemory",
    "SemanticMemoryGraph",
    "HybridRetriever",
    "PreferenceLearner",
    "MemoryVersionManager",
    "ContextBudgetAllocator",
]
