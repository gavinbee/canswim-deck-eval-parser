"""Tests for src/templates/."""
import pytest

from src import schema as s
from src.templates import (
    TEMPLATES,
    TEMPLATE_STUBS,
    Template,
    get_template,
    known_template_ids,
)


class TestRegistry:
    def test_swim_ontario_v1_is_registered(self):
        assert "swim_ontario_v1" in TEMPLATES
        t = TEMPLATES["swim_ontario_v1"]
        assert isinstance(t, Template)

    def test_stubs_are_listed_but_not_in_implemented_dict(self):
        for stub_id in ("swim_quebec_v1", "swim_alberta_v1", "swim_bc_v1"):
            assert stub_id in TEMPLATE_STUBS
            assert stub_id not in TEMPLATES

    def test_known_template_ids_covers_both_dicts(self):
        ids = known_template_ids()
        for impl_id in TEMPLATES:
            assert impl_id in ids
        for stub_id in TEMPLATE_STUBS:
            assert stub_id in ids


class TestSwimOntarioTemplate:
    def setup_method(self):
        self.t = TEMPLATES["swim_ontario_v1"]

    def test_basic_attributes(self):
        assert self.t.id == "swim_ontario_v1"
        assert self.t.display_name == "Swim Ontario On-Deck Evaluation"
        assert self.t.language == "en"
        assert self.t.rows_per_page == 9

    def test_every_canonical_field_has_a_label(self):
        # Every canonical schema field (meet + session + row) must map to
        # a human label on this template — otherwise the vision prompt
        # would have nothing to call it.
        labels = self.t.field_names
        for canonical in s.ALL_FIELDS:
            # session_number is inferred (not a printed label), so it's
            # the one allowed exception.
            if canonical == s.SESSION_NUMBER:
                continue
            assert canonical in labels, (
                f"Swim Ontario template is missing a label for {canonical!r}"
            )

    def test_field_labels_match_eval_gen(self):
        # Tight regression on the verbatim widget labels eval-gen uses.
        labels = self.t.field_names
        assert labels[s.COMPETITION_NAME] == "Competition Name"
        assert labels[s.HOST_CLUB] == "Host Club"
        assert labels[s.COC] == "COC"
        assert labels[s.DATE_SESSION] == "Date  Session"  # two spaces
        assert labels[s.OFFICIAL_NAME] == "Name of Official"
        assert labels[s.SUCCESSFUL] == "Successful initial"
        assert labels[s.MENTOR] == "Mentor Official  Session referee"  # two spaces

    def test_widget_field_map_uses_canonical_keys(self):
        # Every value in the widget map must be a real canonical schema key.
        valid_keys = set(s.ALL_FIELDS)
        for widget_name, canonical in self.t.widget_field_map.items():
            assert canonical in valid_keys, (
                f"widget {widget_name!r} maps to unknown canonical key "
                f"{canonical!r}"
            )

    def test_per_row_widgets_use_index_placeholder(self):
        # Per-row widgets are stored with ``{i}`` so form_extract can
        # substitute 1..rows_per_page at lookup time.
        per_row_keys = {
            s.OFFICIAL_NAME, s.CLUB, s.POSITION, s.LANE_NUMBER,
            s.TIMES_WORKED_POSITION, s.MENTOR, s.LEVEL, s.SUCCESSFUL,
        }
        for widget_name, canonical in self.t.widget_field_map.items():
            if canonical in per_row_keys:
                assert "{i}" in widget_name, (
                    f"per-row widget {widget_name!r} (→ {canonical!r}) "
                    f"is missing the {{i}} placeholder"
                )

    def test_meet_level_widgets_have_no_placeholder(self):
        meet_keys = set(s.MEET_FIELDS)
        for widget_name, canonical in self.t.widget_field_map.items():
            if canonical in meet_keys:
                assert "{i}" not in widget_name, (
                    f"meet-level widget {widget_name!r} should not have "
                    f"a row placeholder"
                )

    def test_vision_prompt_addendum_mentions_swim_ontario(self):
        assert "Swim Ontario" in self.t.vision_prompt_addendum
        # And it should explicitly remind the model about the row-level
        # successful judgement, since that's where the design diverges
        # most from a naïve OCR pass.
        assert "successful" in self.t.vision_prompt_addendum.lower()


class TestStubTemplates:
    @pytest.mark.parametrize(
        "stub_id",
        ["swim_quebec_v1", "swim_alberta_v1", "swim_bc_v1"],
    )
    def test_get_template_raises_not_implemented(self, stub_id):
        with pytest.raises(NotImplementedError) as exc:
            get_template(stub_id)
        # The error message should name the province and point to the
        # GitHub issue so the user knows what to do.
        msg = str(exc.value)
        assert "GitHub issue" in msg
        assert "deck eval template" in msg


class TestGetTemplate:
    def test_returns_implemented_template(self):
        t = get_template("swim_ontario_v1")
        assert t.id == "swim_ontario_v1"

    def test_unknown_id_raises_key_error(self):
        with pytest.raises(KeyError) as exc:
            get_template("swim_yukon_v1")
        # The error should list what IS known so the user can see what
        # they should have typed.
        assert "Known" in str(exc.value)
        assert "swim_ontario_v1" in str(exc.value)
