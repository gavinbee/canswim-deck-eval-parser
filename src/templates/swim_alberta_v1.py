"""Swim Alberta On-Deck Evaluation — placeholder.

Reserved in the registry so detection of an Alberta form fails with a clear,
template-specific message. Tracked in the "Add support for Swim Alberta
deck eval template" GitHub issue.
"""
from .base import Template


def _raise() -> Template:
    raise NotImplementedError(
        "Swim Alberta On-Deck Evaluation template is not yet implemented. "
        "See the open GitHub issue 'Add support for Swim Alberta deck eval "
        "template' to contribute a sample PDF or implementation."
    )


ID = "swim_alberta_v1"
DISPLAY_NAME = "Swim Alberta On-Deck Evaluation"


def __getattr__(name: str):
    if name == "TEMPLATE":
        return _raise()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
