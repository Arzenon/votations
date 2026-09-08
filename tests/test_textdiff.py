"""The public record of changes: showing *what* changed, not both whole values."""

from __future__ import annotations

from app import textdiff
from app.textdiff import field_change
from tests.helpers import make_law


def _text(segments, kind):
    return "".join(s.text for s in segments if s.kind == kind)


def _rebuilt(segments):
    """(old, new) put back together from the segments."""
    old = "".join(s.text for s in segments if s.kind in ("equal", "removed"))
    new = "".join(s.text for s in segments if s.kind in ("equal", "added"))
    return old, new


def test_only_the_words_that_changed_are_marked():
    """The point of the whole thing: a reworded sentence marks the rewording.

    Before this, a change of two words inside a four-thousand-character block
    looked exactly like a block that had been rewritten — both were two full
    copies in two narrow grey cells.
    """
    change = field_change(
        "La loi impose aux plateformes de plus de 10 millions d'utilisateurs.\n\nUn second.",
        "La loi impose aux très grandes plateformes de plus de 15 millions d'utilisateurs."
        "\n\nUn second.",
    )
    assert change.short is False
    assert "très grandes" in _text(change.segments, "added")
    assert "15" in _text(change.segments, "added")
    assert "10" in _text(change.segments, "removed")
    # And the untouched half of the sentence is not marked at all.
    assert "impose" not in _text(change.segments, "added")
    assert "impose" not in _text(change.segments, "removed")


def test_the_segments_rebuild_both_values_exactly():
    """A diff that cannot be put back together is a diff that lost something.

    Whitespace is tokenised alongside the words for this reason: these fields are
    prose, and `app.templating.paragraphs` splits the law page on blank lines.
    """
    old = "Premier paragraphe.\n\nDeuxième   paragraphe, avec   des espaces.\n"
    new = "Premier paragraphe modifié.\n\nDeuxième   paragraphe, avec   des espaces.\n"
    rebuilt_old, rebuilt_new = _rebuilt(field_change(old, new).segments)
    assert rebuilt_old == old
    assert rebuilt_new == new


def test_a_short_value_is_shown_whole_rather_than_diffed():
    """« oui » → « non » needs no marking; the publication flag and the dates."""
    for old, new in (("non", "oui"), ("", "24 juin 2026"), ("20/08/2026 à 19:50", "")):
        change = field_change(old, new)
        assert change.short is True, (old, new)
        assert change.segments == []

    # And a sentence is not a token, however short: the threshold sits below the
    # shortest real prose, not above the longest scalar.
    sentence = field_change(
        "La loi impose aux plateformes de publier leurs critères.",
        "La loi impose aux très grandes plateformes de publier leurs critères.",
    )
    assert sentence.short is False
    assert _text(sentence.segments, "added").strip() == "très grandes"


def test_a_block_written_from_nothing_is_a_rewrite_not_an_edit():
    """Every word is new, so there is no context for marks to stand out against."""
    change = field_change("", "mot " * 200)
    assert change.short is False
    assert change.rewritten is True
    assert change.segments == []
    assert change.new.startswith("mot")


def test_a_heavily_rewritten_block_is_shown_whole_rather_than_marked():
    """Past half the words, the marks stop being signal and become confetti.

    Measured on the real corpus: a rewritten « ce qui a été débattu » produced
    fifty-odd alternating marks in one paragraph, with deletions and insertions
    abutting so that « garanties » and « garde-fous » read as one word.
    """
    change = field_change(
        "Le chat dort sur le tapis rouge du salon principal chaque après-midi.",
        "Une phrase entièrement différente parlant de tout autre chose ici présente.",
    )
    assert change.rewritten is True
    assert change.segments == []
    assert change.old and change.new, "both versions are still carried, whole"


def test_an_ordinary_edit_is_still_marked_word_by_word():
    """The counterpart: the fallback must not swallow the common case."""
    change = field_change(
        "La loi impose aux plateformes de publier leurs critères techniques chaque année.",
        "La loi impose aux très grandes plateformes de publier leurs critères "
        "techniques chaque année.",
    )
    assert change.rewritten is False
    assert _text(change.segments, "added").strip() == "très grandes"


def test_a_long_unchanged_run_is_collapsed_with_its_count():
    """A full rewrite would otherwise print the old text and then the new one."""
    middle = " ".join(f"mot{i}" for i in range(300))
    change = field_change(f"Début. {middle} Fin ancienne.", f"Début. {middle} Fin nouvelle.")

    elided = [s for s in change.segments if s.kind == "elided"]
    assert len(elided) == 1, "the unchanged middle should collapse to one marker"
    assert elided[0].words > 200
    # What is collapsed is only ever text both versions carry, so nothing the
    # reader needs in order to see the change is hidden.
    assert "ancienne" in _text(change.segments, "removed")
    assert "nouvelle" in _text(change.segments, "added")
    # And a little context survives on each side of the gap.
    assert "Début." in _text(change.segments, "equal")


def test_a_short_unchanged_run_is_left_alone():
    change = field_change("Le chat dort ici.", "Le chien dort ici.")
    assert not any(s.kind == "elided" for s in change.segments)


def test_common_french_words_are_not_reported_as_changed():
    """`difflib`'s autojunk treats frequent tokens as noise, which here is prose.

    Left on, every space and half the articles in a long block scored as junk and
    « de » and « la » came back marked wherever they appeared.
    """
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
    # The old four-column table is gone, and with it the horizontal scroll.
    assert 'class="evolution revisions"' not in body


def test_a_scalar_change_is_shown_as_before_and_after(client, db_session):
    law = _law_with_revision(db_session, "Un résumé.", "2026-07-01", field="official_ref")
    body = client.get(f"/loi/{law.slug}/resultats").text
    assert "revision-swap" in body


def test_administrator_text_can_never_inject_markup(client, db_session):
    """The segments are data; the template escapes them like any other value.

    This is why the diff is not built as HTML in Python — doing that would mean
    hand-escaping administrator prose into markup, on a public page, and V3 opens
    the form to every authenticated user.
    """
    law = _law_with_revision(
        db_session,
        "Texte initial parfaitement ordinaire ici.",
        "Texte <script>alert('xss')</script> ordinaire ici.",
    )
    body = client.get(f"/loi/{law.slug}/resultats").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_the_thresholds_are_the_ones_the_module_declares():
    """Named constants rather than numbers in the branches that use them."""
    assert textdiff.SHORT_VALUE_CHARS > 0
    assert textdiff.CONTEXT_WORDS * 2 < textdiff.ELIDE_OVER_WORDS, (
        "a collapsed run must be longer than the context kept around it, or the "
        "marker would replace less text than it stands for"
    )
