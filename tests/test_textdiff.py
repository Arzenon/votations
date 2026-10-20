"""Revision history: showing what changed rather than both full values."""

from __future__ import annotations

import re

from app import textdiff
from app.models import LawRevision
from app.textdiff import field_change
from tests.helpers import make_law


def _text(segments, kind):
    return "".join(s.text for s in segments if s.kind == kind)


def _rebuilt(segments):
    """(old, new) rebuilt from the segments."""
    old = "".join(s.text for s in segments if s.kind in ("equal", "removed"))
    new = "".join(s.text for s in segments if s.kind in ("equal", "added"))
    return old, new


def test_only_the_words_that_changed_are_marked():
    """Only the changed words are marked."""
    change = field_change(
        "La loi impose aux plateformes de plus de 10 millions d'utilisateurs.\n\nUn second.",
        "La loi impose aux très grandes plateformes de plus de 15 millions d'utilisateurs."
        "\n\nUn second.",
    )
    assert change.short is False
    assert "très grandes" in _text(change.segments, "added")
    assert "15" in _text(change.segments, "added")
    assert "10" in _text(change.segments, "removed")
    # The unchanged part isn't marked.
    assert "impose" not in _text(change.segments, "added")
    assert "impose" not in _text(change.segments, "removed")


def test_the_segments_rebuild_both_values_exactly():
    """The segments rebuild both values exactly, whitespace included."""
    old = "Premier paragraphe.\n\nDeuxième   paragraphe, avec   des espaces.\n"
    new = "Premier paragraphe modifié.\n\nDeuxième   paragraphe, avec   des espaces.\n"
    rebuilt_old, rebuilt_new = _rebuilt(field_change(old, new).segments)
    assert rebuilt_old == old
    assert rebuilt_new == new


def test_a_short_value_is_shown_whole_rather_than_diffed():
    """Short values like "oui"/"non" or dates are shown whole."""
    for old, new in (("non", "oui"), ("", "24 juin 2026"), ("20/08/2026 à 19:50", "")):
        change = field_change(old, new)
        assert change.short is True, (old, new)
        assert change.segments == []

    # A short sentence is still diffed.
    sentence = field_change(
        "La loi impose aux plateformes de publier leurs critères.",
        "La loi impose aux très grandes plateformes de publier leurs critères.",
    )
    assert sentence.short is False
    assert _text(sentence.segments, "added").strip() == "très grandes"


def test_a_block_written_from_nothing_is_a_rewrite_not_an_edit():
    """A block written from scratch counts as a rewrite."""
    change = field_change("", "mot " * 200)
    assert change.short is False
    assert change.rewritten is True
    assert change.segments == []
    assert change.new.startswith("mot")


def test_a_heavily_rewritten_block_is_shown_whole_rather_than_marked():
    """Above half the words changed, both versions are shown instead of a diff."""
    change = field_change(
        "Le chat dort sur le tapis rouge du salon principal chaque après-midi.",
        "Une phrase entièrement différente parlant de tout autre chose ici présente.",
    )
    assert change.rewritten is True
    assert change.segments == []
    assert change.old and change.new, "both versions are still carried, whole"


def test_an_ordinary_edit_is_still_marked_word_by_word():
    """A normal edit is still diffed word by word."""
    change = field_change(
        "La loi impose aux plateformes de publier leurs critères techniques chaque année.",
        "La loi impose aux très grandes plateformes de publier leurs critères "
        "techniques chaque année.",
    )
    assert change.rewritten is False
    assert _text(change.segments, "added").strip() == "très grandes"


def test_a_long_unchanged_run_is_collapsed_with_its_count():
    """A long unchanged run is collapsed, with its word count."""
    middle = " ".join(f"mot{i}" for i in range(300))
    change = field_change(f"Début. {middle} Fin ancienne.", f"Début. {middle} Fin nouvelle.")

    elided = [s for s in change.segments if s.kind == "elided"]
    assert len(elided) == 1, "the unchanged middle should collapse to one marker"
    assert elided[0].words > 200
    # Only unchanged text is collapsed.
    assert "ancienne" in _text(change.segments, "removed")
    assert "nouvelle" in _text(change.segments, "added")
    # And a little context survives on each side of the gap.
    assert "Début." in _text(change.segments, "equal")


def test_a_short_unchanged_run_is_left_alone():
    change = field_change("Le chat dort ici.", "Le chien dort ici.")
    assert not any(s.kind == "elided" for s in change.segments)


def test_common_french_words_are_not_reported_as_changed():
    """Common words like "de" or "la" aren't marked as changed (autojunk is off)."""
    old = "de la loi " * 60 + "avant"
    new = "de la loi " * 60 + "après"
    marked = _text(change_segments := field_change(old, new).segments, "removed")
    assert "avant" in marked
    assert marked.replace("avant", "").strip() == "", "only the real change is marked"
    assert _text(change_segments, "added").strip() == "après"


# --- On the page --------------------------------------------------------------


def _law_with_revision(db, old, new, field="summary_text"):
    from app.crud import laws as laws_crud

    law = make_law(db, slug="loi-diff", title="Loi modifiée", published=True, summary_text=old)
    laws_crud.update(db, law, **{field: new})
    return law


def test_the_results_page_marks_the_change(client, db_session):
    law = _law_with_revision(
        db_session,
        "La loi impose aux plateformes de publier leurs critères.",
        "La loi impose aux très grandes plateformes de publier leurs critères.",
    )
    body = client.get(f"/loi/{law.slug}/resultats").text
    assert "<ins>très grandes</ins>" in body or "<ins> très grandes</ins>" in body
    assert "Ce que dit la loi" in body, "the field is named"
    assert 'class="evolution revisions"' not in body


def test_a_scalar_change_is_shown_as_before_and_after(client, db_session):
    law = _law_with_revision(db_session, "Un résumé.", "2026-07-01", field="official_ref")
    body = client.get(f"/loi/{law.slug}/resultats").text
    assert "revision-swap" in body


def test_administrator_text_can_never_inject_markup(client, db_session):
    """Diff segments are escaped by the template."""
    law = _law_with_revision(
        db_session,
        "Texte initial parfaitement ordinaire ici.",
        "Texte <script>alert('xss')</script> ordinaire ici.",
    )
    body = client.get(f"/loi/{law.slug}/resultats").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_the_thresholds_are_the_ones_the_module_declares():
    """The thresholds are the module's constants."""
    assert textdiff.SHORT_VALUE_CHARS > 0
    assert textdiff.CONTEXT_WORDS * 2 < textdiff.ELIDE_OVER_WORDS, (
        "a collapsed run must be longer than the context kept around it, or the "
        "marker would replace less text than it stands for"
    )


# --- Large changes --------------------------------------------------------------


def _long_text(words: int) -> str:
    return " ".join(f"mot{i % 97} de la loi" for i in range(words // 4))


def test_a_small_edit_in_a_long_field_is_still_diffed_inline():
    """Only the changed region counts towards the inline limit."""
    old = _long_text(8000)
    new = old.replace("mot50 de", "mot50 modifié de", 1)
    change = field_change(old, new)
    assert change.deferred is False
    assert _text(change.segments, "added").strip() == "modifié"


def _edited_at_both_ends(text: str) -> str:
    words = text.split(" ")
    words[1], words[-2] = "DÉBUT", "FIN"
    return " ".join(words)


def test_a_change_spread_over_a_long_field_is_left_to_its_own_page():
    """Edits at both ends of a long field aren't diffed on the results page."""
    old = _long_text(8000)
    new = _edited_at_both_ends(old)
    assert field_change(old, new).deferred is True

    full = textdiff.full_change(old, new)
    assert full.deferred is False
    assert [seg.text for seg in full.segments if seg.kind == "added"] == ["DÉBUT", "FIN"]
    assert [seg.text for seg in full.segments if seg.kind == "removed"] == ["de", "la"]


def test_a_large_changed_region_still_rebuilds_both_versions():
    """Above FINE_DIFF_TOKENS the coarser tokens keep every character."""
    old = " ".join(f"mot{i}\n\n" if i % 50 == 0 else f"mot{i}" for i in range(900))
    words = old.split(" ")
    for i in range(1, len(words) - 1, 30):
        words[i] = "changé"
    new = " ".join(words)
    segments = textdiff.diff(old, new)
    assert segments is not None
    assert not [s for s in segments if s.kind == "elided"], "runs too short to collapse"
    assert _rebuilt(segments) == (old, new)


def test_a_whitespace_change_in_a_large_region_marks_only_the_whitespace():
    """A paragraph break added among coarse tokens doesn't mark the next word."""
    # Distinct words: a repeating text can be aligned one period off.
    old = " ".join(f"mot{i}" for i in range(1200))
    words = old.split(" ")
    words[1], words[-2] = "DÉBUT", "FIN"
    words[500] = "\n\n" + words[500]
    new = " ".join(words)
    segments = textdiff.diff(old, new)
    # Only the space that became a paragraph break is marked, not the words around it.
    assert [seg.text for seg in segments if seg.kind == "added"] == ["DÉBUT", " \n\n", "FIN"]
    assert [seg.text for seg in segments if seg.kind == "removed"] == ["mot1", " ", "mot1198"]


def test_the_results_page_links_a_large_change_to_its_own_page(client, db_session):
    old = _long_text(8000)
    law = _law_with_revision(db_session, old, _edited_at_both_ends(old))
    body = client.get(f"/loi/{law.slug}/resultats").text
    link = re.search(r'href="(/loi/loi-diff/modifications/\d+)"', body)
    assert link, "the full diff stays reachable"

    page = client.get(link.group(1))
    assert page.status_code == 200
    assert "DÉBUT</ins>" in page.text and "FIN</ins>" in page.text
    assert "Ce que dit la loi" in page.text


def test_a_revision_page_only_shows_that_law_s_revisions(client, db_session):
    law = _law_with_revision(db_session, "Texte initial parfaitement ordinaire.", "Autre texte.")
    revision_id = db_session.query(LawRevision).filter_by(law_id=law.id).one().id
    other = make_law(db_session, slug="loi-voisine", title="Loi voisine")
    assert client.get(f"/loi/{other.slug}/modifications/{revision_id}").status_code == 404
    assert client.get(f"/loi/{law.slug}/modifications/{revision_id}").status_code == 200


def test_a_revision_id_that_cannot_exist_is_a_404_not_an_error(client, db_session):
    law = make_law(db_session, slug="loi-ids")
    for odd in ("99999999999999999999", "abc", "-1", "1e3", "١٢", "0", "007"):
        assert client.get(f"/loi/{law.slug}/modifications/{odd}").status_code == 404, odd


def test_the_results_page_diffs_each_revision_once():
    """Revisions never change: the page reuses the diff instead of recomputing it."""
    from app.templating import templates

    diff_for_page = templates.env.globals["field_change"]
    old, new = "Le chat dort ici. " * 10, "Le chien dort ici. " * 10
    assert diff_for_page(old, new) is diff_for_page(old, new)
    assert diff_for_page(old, new) == field_change(old, new)
