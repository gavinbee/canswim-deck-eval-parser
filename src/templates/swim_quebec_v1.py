"""Natation Québec On-Deck Evaluation — placeholder.

Reserved in the registry so detection of a Quebec form fails with a clear,
template-specific message rather than a generic "no match". Tracked in the
"Add support for Natation Québec deck eval template" GitHub issue.
"""
from .base import Template


class _NotImplementedTemplate:
    """Marker raised whenever the parser tries to use this template."""


def _raise() -> Template:
    raise NotImplementedError(
        "Natation Québec On-Deck Evaluation template is not yet implemented. "
        "See the open GitHub issue 'Add support for Natation Québec deck "
        "eval template' to contribute a sample PDF or implementation."
    )


# Module-level constants the registry uses to surface the stub by id without
# instantiating the (non-existent) Template. ``TEMPLATE`` itself is a property
# that raises on access.
ID = "swim_quebec_v1"
DISPLAY_NAME = "Natation Québec On-Deck Evaluation"


def __getattr__(name: str):
    # PEP 562: lazy module attribute. ``TEMPLATE`` raises when accessed.
    if name == "TEMPLATE":
        return _raise()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
