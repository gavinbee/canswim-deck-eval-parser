"""Regenerate the synthetic typed-scan vision fixture + its golden.

This is the **vision-path** sibling of
``tests/fixtures/form_field/make_synthetic_fixture.py``. Where that script
produces a *fillable* PDF (exercising the form-field fast path), this one
produces a **flattened, image-only** PDF — no widgets, no text layer — that
routes through the vision model, plus an exact ``golden.json`` describing
what's on it.

The point (see ``docs/design/0002-synthetic-vision-fixtures.md``): a typed
digital-export form that's been flattened is a *real production input
shape*, and it doubles as a committed, deterministic regression fixture for
the straightforward (typed) case. It is intentionally easier than a
handwritten scan — handwriting robustness stays the job of the Tier-2 (real
scans) and Tier-3 (pseudonymized) fixtures.

Everything is generated from a fixed Faker seed, so re-running with the same
seed and Faker version reproduces an identical PDF + golden. No real
person's information is ever committed.

Recorded command for the committed fixture:

.. code-block:: bash

    python tests/fixtures/vision_scan/make_synthetic_scan.py --seed 42

Inputs:
    A blank template PDF, committed in-tree at
    ``templates/<template_id>.pdf`` (one per supported template, alongside
    ``src/templates/<template_id>.py`` and ``docs/templates/``). It's a
    first-class reference asset, not a test fixture: nothing in the test
    suite opens it — only this generator and template detection do — so it
    lives at the repo root rather than under ``tests/``. Keeping a copy in
    the source tree means anyone can regenerate the fixture without cloning
    eval-gen, and a material change to a province's form shows up as a diff
    to its committed blank — a useful signal when template *detection*
    starts failing on a redesigned form. ``--template`` overrides the path
    (e.g. to regenerate from a fresh eval-gen export).

Outputs (overwritten in this directory):
    ``session_1_evals_scan.pdf``        — flattened image-only PDF
    ``session_1_evals_scan.golden.json`` — exact ground truth
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from itertools import cycle
from pathlib import Path

import fitz
from faker import Faker
from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# Constrained vocabularies — kept in step with the form-field generator so
# both fixtures read as the same kind of meet.
# ---------------------------------------------------------------------------

POSITIONS = [
    "Starter",
    "Stroke Judge",
    "Inspector of Turns",
    "Inspector of Turns",
    "Chief Timer",
    "Timer",
    "Timer",
    "Admin Desk",
    "Recorder",
    "Timer",
    "Timer",
]

LANE_BY_POSITION = {
    "Inspector of Turns": cycle(["4", "5", "6", "7"]),
    "Timer":              cycle(["1", "2", "3", "4", "5", "6", "7", "8"]),
}

# The mentor's officiating level. On these forms the level is virtually
# always written as an Arabic numeral, so the fixture uses Arabic too.
LEVELS = cycle(["2", "3", "4", "5"])

# How many times the official has worked the position — a small spread of
# plausible values. Deliberately *not* equal to the lane number, so the
# harness can catch the model copying the lane column into this one
# (the "column bleed" bug noted in design doc 0002).
TIMES_WORKED = cycle(["1", "3", "5", "8", "12", "20"])

ROWS_PER_PAGE = 9
DUPLICATE_SUFFIX_RE = re.compile(r"\s\[\d+\]$")

# Rasterization DPI. Matches src/pdf_io.DEFAULT_DPI so the committed scan
# resembles what the parser would render from a real flattened PDF.
DEFAULT_DPI = 200


# ---------------------------------------------------------------------------
# successful-intent scheme (deterministic)
#
# A typed form's natural states are "initials present -> true" and
# "blank -> null". To exercise a clear ``false`` and a genuine ``null`` we
# pick two specific 0-based official indices and render them specially:
#   - FALSE_INDEX:  whole row struck through (clear not-successful).
#   - NULL_INDEX:   mentor assigned but initials left blank (ambiguous).
# Every other row gets typed initials -> true.
# ---------------------------------------------------------------------------

FALSE_INDEX = 7   # page 1, row 8
NULL_INDEX = 8    # page 1, row 9


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    return "".join(p[0].upper() for p in parts[:2]) or "X"


def _make_officials(faker: Faker, n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        position = POSITIONS[i % len(POSITIONS)]
        lane_iter = LANE_BY_POSITION.get(position)
        lane = next(lane_iter) if lane_iter is not None else ""
        mentor = faker.name()

        if i == FALSE_INDEX:
            successful = False
            initials = ""        # struck-through row; no sign-off initials
        elif i == NULL_INDEX:
            successful = None
            initials = ""        # mentor assigned, but not signed off
        else:
            successful = True
            initials = _initials(mentor)

        out.append({
            "name":     faker.name(),
            "club":     faker.bothify(text="???", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            "position": position,
            "lane":     lane,
            "times":    next(TIMES_WORKED),
            "mentor":   mentor,
            "level":    next(LEVELS),
            "initials": initials,
            "successful": successful,
        })
    return out


def _meet_header(faker: Faker, total_pages: int) -> dict[str, str]:
    return {
        "Competition Name":        f"{faker.last_name()} Open 2026",
        "Competition Coordinator": faker.name(),
        "Level":                   "",   # cc_level intentionally blank
        "Date  Session":           "Fri, Jan 9, 2026 / Session 1",
        "Host Club":               faker.bothify(text="???", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        "COC":                     faker.name(),
        "Page":                    "",   # set per page below
        "of":                      str(total_pages),
    }


def _row_widget_values(o: dict) -> dict[str, str]:
    """Map base widget names -> typed value for one official's row."""
    return {
        "Name of OfficialRow":                             o["name"],
        "ClubRow":                                         o["club"],
        "PositionRow":                                     o["position"],
        "Lane numberRow":                                  o["lane"],
        "How many times have you worked this positionRow": o["times"],
        "Mentor Official  Session refereeRow":             o["mentor"],
        "LevelRow":                                        o["level"],
        "Successful initialRow":                           o["initials"],
    }


def _blank_row_values() -> dict[str, str]:
    return {k: "" for k in _row_widget_values(
        {"name": "", "club": "", "position": "", "lane": "", "times": "",
         "mentor": "", "level": "", "initials": ""}
    )}


def _write_widget(widget, value: str) -> None:
    widget.field_value = value
    widget.update()


def _render_page_image(
    template_path: Path,
    page_meet: dict[str, str],
    page_officials: list[dict],
    dpi: int,
) -> Image.Image:
    """Fill, flatten, and rasterize one page; return a PIL image.

    Strike-through marks for not-successful rows are drawn onto the
    rasterized pixels using the (pre-bake) widget geometry, so they land
    exactly over the intended row.
    """
    template = fitz.open(template_path)
    page = template[0]

    # y-center + x-extent (PDF points) of any row we need to strike through.
    strike_rows: list[tuple[float, float, float]] = []  # (x0, x1, y_center)

    for widget in page.widgets() or ():
        bare = DUPLICATE_SUFFIX_RE.sub("", widget.field_name)

        if bare in page_meet:
            _write_widget(widget, page_meet[bare])
            continue

        m = re.match(r"^(.+?)Row(\d+)$", bare)
        if not m:
            continue
        base, row_index = m.group(1), int(m.group(2))
        base_key = f"{base}Row"

        if row_index <= len(page_officials):
            o = page_officials[row_index - 1]
            row_vals = _row_widget_values(o)
            # Capture geometry off the leftmost (name) widget for a row we
            # render as struck-through.
            if base_key == "Name of OfficialRow" and o["successful"] is False:
                r = widget.rect
                # Extend to the row's right edge (the Successful column).
                strike_rows.append((r.x0, 736.2, (r.y0 + r.y1) / 2.0))
        else:
            row_vals = _blank_row_values()
        _write_widget(widget, row_vals.get(base_key, ""))

    # Flatten widgets into static page content, then rasterize.
    template.bake(annots=True, widgets=True)
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    template.close()

    # Draw strike-throughs in pixel space.
    draw = ImageDraw.Draw(img)
    for x0, x1, yc in strike_rows:
        draw.line(
            [(x0 * zoom, yc * zoom), (x1 * zoom, yc * zoom)],
            fill=(0, 0, 0),
            width=max(2, round(zoom * 1.2)),
        )

    return img


def _build_golden(
    output_pdf_name: str,
    meet: dict[str, str],
    officials: list[dict],
    pages_of_officials: list[list[dict]],
) -> dict:
    """Exact ground truth for the rendered scan (no model in the loop)."""
    rows = []
    for page_index, page_officials in enumerate(pages_of_officials):
        for row_index, o in enumerate(page_officials, start=1):
            rows.append({
                "source_page": page_index + 1,
                "row_index": row_index,
                "official_name": o["name"],
                "club": o["club"],
                "position": o["position"],
                "lane_number": o["lane"] or None,
                "times_worked_position": o["times"],
                "mentor": o["mentor"],
                "level": o["level"],
                "successful": o["successful"],
            })
    return {
        "source_pdf": output_pdf_name,
        "template_id": "swim_ontario_v1",
        "meet": {
            "competition_name": meet["Competition Name"],
            "host_club": meet["Host Club"],
            "coc": meet["COC"],
        },
        "session": {
            "competition_coordinator": meet["Competition Coordinator"],
            "cc_level": None,
            "date_session": "Fri, Jan 9, 2026 / Session 1",
            "session_number": 1,
        },
        "rows": rows,
    }


def generate(
    template_path: Path,
    output_pdf: Path,
    output_golden: Path,
    seed: int,
    pages: int,
    officials_on_last_page: int,
    dpi: int,
) -> None:
    faker = Faker("en_CA")
    Faker.seed(seed)

    total_officials = (pages - 1) * ROWS_PER_PAGE + officials_on_last_page
    officials = _make_officials(faker, total_officials)
    meet = _meet_header(faker, total_pages=pages)

    pages_of_officials = [
        officials[i : i + ROWS_PER_PAGE]
        for i in range(0, len(officials), ROWS_PER_PAGE)
    ]

    out = fitz.open()
    for page_index in range(pages):
        page_officials = pages_of_officials[page_index]
        page_meet = dict(meet)
        page_meet["Page"] = str(page_index + 1)

        img = _render_page_image(template_path, page_meet, page_officials, dpi)

        # Insert the rendered page as a pure image — no widgets, no text
        # layer — so the parser routes it to the vision path.
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        new_page = out.new_page(width=792, height=612)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())

    out.save(output_pdf, deflate=True, garbage=4, clean=True)
    out.close()

    golden = _build_golden(output_pdf.name, meet, officials, pages_of_officials)
    output_golden.write_text(
        json.dumps(golden, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# Template id this fixture is generated for. The blank form lives in-tree
# at the repo-root templates/<id>.pdf (a first-class reference asset).
TEMPLATE_ID = "swim_ontario_v1"


def _in_tree_template_path() -> Path:
    """The committed blank template for ``TEMPLATE_ID`` (repo-root templates/)."""
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates" / f"{TEMPLATE_ID}.pdf"


def _default_template_path() -> Path:
    # Prefer the in-tree copy so regeneration needs no external clone.
    in_tree = _in_tree_template_path()
    if in_tree.is_file():
        return in_tree
    # Fall back to a sibling eval-gen checkout (handy when refreshing the
    # committed blank from a new export).
    for c in (
        Path(__file__).resolve().parents[3].parent / "eval-gen" / "eval_form.pdf",
        Path("C:/Users/gavbe/src/eval-gen/eval_form.pdf"),
    ):
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"Could not find a blank template. Expected the committed copy at "
        f"{in_tree}, or a sibling eval-gen checkout. Pass --template "
        f"/path/to/eval_form.pdf to override."
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--template", type=Path, default=None,
                   help="Path to blank eval_form.pdf (defaults next to repo).")
    p.add_argument("--output", type=Path,
                   default=Path(__file__).parent / "session_1_evals_scan.pdf",
                   help="Output path for the flattened image-only PDF.")
    p.add_argument("--golden", type=Path, default=None,
                   help="Output path for the golden JSON "
                        "(defaults to <output>.golden.json).")
    p.add_argument("--seed", type=int, default=42, help="Faker seed.")
    p.add_argument("--pages", type=int, default=2, help="Number of pages.")
    p.add_argument("--officials-on-last-page", type=int, default=2,
                   help="How many of the 9 row slots on the last page to fill.")
    p.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                   help="Rasterization DPI.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    template = args.template or _default_template_path()
    golden = args.golden or args.output.with_suffix(".golden.json")
    print(f"Reading template: {template}")
    print(f"Writing scan:     {args.output}")
    print(f"Writing golden:   {golden}")
    print(f"Seed:             {args.seed}")
    generate(
        template_path=template,
        output_pdf=args.output,
        output_golden=golden,
        seed=args.seed,
        pages=args.pages,
        officials_on_last_page=args.officials_on_last_page,
        dpi=args.dpi,
    )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
