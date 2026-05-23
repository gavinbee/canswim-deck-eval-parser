"""Provincial template registry.

``TEMPLATES`` maps stable template IDs (e.g. ``"swim_ontario_v1"``) to their
``Template`` instances. The vision-driven template-detection step picks one
of these IDs based on what page 1 of the input PDF looks like, and the
rest of the pipeline reads everything it needs from that ``Template``.

Adding a new template:

1. Drop a new module beside the existing ones (``src/templates/swim_FOO_v1.py``).
2. Export a module-level ``TEMPLATE: Template`` instance.
3. Add it to ``TEMPLATES`` below.
4. Add a section to ``docs/templates/swim_FOO.md``.

See ``docs/templates/README.md`` for the full contract.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import swim_alberta_v1, swim_bc_v1, swim_ontario_v1, swim_quebec_v1
from .base import Template

if TYPE_CHECKING:
    pass


def _stub_metadata(module) -> tuple[str, str]:
    """Return ``(id, display_name)`` for a not-yet-implemented stub module
    without triggering its ``NotImplementedError``."""
    return module.ID, module.DISPLAY_NAME


# Implemented templates only.
TEMPLATES: dict[str, Template] = {
    swim_ontario_v1.TEMPLATE.id: swim_ontario_v1.TEMPLATE,
}


# Stubs that are *known* (so we can mention them by name in CLI errors and
# detection prompts) but not yet implemented. Accessing ``.TEMPLATE`` on any
# of these raises ``NotImplementedError`` with a helpful pointer to the
# GitHub issue.
TEMPLATE_STUBS: dict[str, str] = dict([
    _stub_metadata(swim_quebec_v1),
    _stub_metadata(swim_alberta_v1),
    _stub_metadata(swim_bc_v1),
])


def get_template(template_id: str) -> Template:
    """Look up an implemented template by id.

    Raises:
        NotImplementedError: if the id matches a registered stub.
        KeyError:            if the id is unknown.
    """
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]
    if template_id in TEMPLATE_STUBS:
        # Re-route through the stub module so the user gets the helpful
        # per-template message.
        from importlib import import_module
        module = import_module(f"{__name__}.{template_id}")
        return module.TEMPLATE  # raises NotImplementedError
    raise KeyError(
        f"Unknown template id {template_id!r}. "
        f"Known: {sorted(TEMPLATES) + sorted(TEMPLATE_STUBS)}"
    )


def known_template_ids() -> list[str]:
    """Every id the parser will *recognize*, including not-yet-implemented
    stubs. Detection's classification prompt uses this list."""
    return sorted(list(TEMPLATES) + list(TEMPLATE_STUBS))


__all__ = ["TEMPLATES", "TEMPLATE_STUBS", "Template", "get_template", "known_template_ids"]
