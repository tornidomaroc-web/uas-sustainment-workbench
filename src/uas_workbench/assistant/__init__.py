"""A read-only assistant over the workbench's own API: the model phrases, the engine computes."""

from .agent import (
    SYSTEM_PROMPT,
    Answer,
    Call,
    Grounding,
    RefusedCall,
    StepLimit,
    ask,
    ground,
    service_caller,
)
from .backends import OllamaBackend, ReplayBackend, ToolCall, Turn
from .recording import Recording, load_recordings, replay
from .tools import TOOLS, UnknownTool, resolve

__all__ = [
    "SYSTEM_PROMPT",
    "TOOLS",
    "Answer",
    "Call",
    "Grounding",
    "OllamaBackend",
    "Recording",
    "RefusedCall",
    "ReplayBackend",
    "StepLimit",
    "ToolCall",
    "Turn",
    "UnknownTool",
    "ask",
    "ground",
    "load_recordings",
    "replay",
    "resolve",
    "service_caller",
]
