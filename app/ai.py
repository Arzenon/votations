"""AI-assisted first draft of a law's neutral presentation.

Given the official text published at the Journal Officiel, produce a draft of the
two **factual** blocks of the charter's presentation structure:

1. « Ce que dit la loi » — a factual summary of the official text.
2. « Ce que ça change en pratique » — the concrete consequences, in plain French.

The two argument blocks are deliberately **not** generated. Arguments have to be
sourced and attributed, and the Journal Officiel carries no argumentative
material at all — no exposé des motifs, no debates, no attributed positions.
Asking a model for them would be asking it to invent, which is exactly what the
charter forbids. A human researches and attributes those.

Design notes:
- The client is injectable so the prompt and parsing are testable offline.
- Drafts are never generated automatically, never stored, and never published:
  the output pre-fills a form that a person edits and validates.
- The input is measured before sending, so an outsized text is refused rather
  than silently billed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.config import get_settings
from app.debates import Speech
from app.legifrance import LawText

if TYPE_CHECKING:  # the SDK is imported lazily below, so the feature stays optional
    from anthropic.types import MessageParam


class AiDraftError(RuntimeError):
    """Raised when no draft can be produced; the admin writes the blocks by hand."""


def build_client() -> Any:
    """An Anthropic client, or the refusal that says the feature is switched off.

    Public because one draft makes four requests across the two functions below —
    a token count and a completion each — and they are worth making down one
    connection. Each function used to build its own client and drop it
    unreferenced, so a draft opened two TLS connections to do the work of one and
    closed neither on purpose.

    The SDK import stays inside the function: with no key configured the feature
    is simply absent, and it must not become an import-time dependency of the app.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AiDraftError(
            "aucune clé API configurée — renseignez ANTHROPIC_API_KEY pour activer les brouillons"
        )
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


class LawDraft(BaseModel):
    """The two factual blocks. Anything not supported by the text stays empty."""

    summary_text: str
    practical_changes: str


SYSTEM_PROMPT = """\
Tu prépares la présentation neutre d'une loi française promulguée, pour une \
plateforme civique qui ne prend jamais parti et dont les résumés sont relus par \
une personne avant publication.

On te donne le texte officiel publié au Journal Officiel. Tu produis deux blocs :

1. summary_text — « Ce que dit la loi » : **l'idée de la loi, pas son plan**. De \
quoi elle traite, par quels moyens elle agit, quels textes elle modifie, à partir \
de quand et sur quels territoires elle s'applique — et ce qui, le cas échéant, \
n'est pas en vigueur (censure du Conseil constitutionnel, entrée en vigueur \
différée). La loi vue de l'extérieur.

Règles propres à ce premier bloc :
- **N'y détaille pas les obligations qui en découlent pour les gens.** C'est le \
travail du second bloc, qui le fait depuis la place du lecteur et le fait mieux. \
Nomme le domaine sur lequel la loi intervient, pas la règle qu'elle y produit.
- Aussi court que le texte le permet : deux ou trois paragraphes suffisent pour la \
plupart des lois.
- Une exception : si la loi ne crée d'obligation pour personne — ratification d'un \
accord, réorganisation administrative, mesure purement budgétaire — le second bloc \
sera vide et celui-ci porte seul la présentation. Il peut alors être plus détaillé.

2. practical_changes — « Ce que ça change en pratique » : **pas le premier bloc \
reformulé en langage simple**, mais la réponse à une autre question — « est-ce que \
cela me concerne, et qu'est-ce que je dois faire différemment ? ».

Règles propres à ce second bloc :
- Organise-le par **personnes concernées** (« si vous êtes locataire », « pour les \
employeurs », « pour les communes »), jamais en suivant le plan du texte.
- Pour chacune : ce qu'elle doit ou peut faire qu'elle ne pouvait pas avant, à \
partir de quand, et ce qui arrive si elle ne le fait pas — lorsque le texte le dit.
- Dis ce qui **ne change pas** lorsque le sujet ou l'intitulé de la loi pourrait \
laisser croire le contraire.
- **Un paragraphe court par public concerné : deux ou trois phrases.** Le nombre \
de publics n'est pas plafonné — une loi qui touche quinze codes en concerne \
beaucoup, et en écarter serait choisir de qui on ne parle pas. C'est la longueur \
de chacun qui est tenue. Cette contrainte est là pour te faire *choisir* ce qui \
compte, jamais pour te faire rester vague : garde le seuil, la date ou le montant \
qui change une décision, écarte le reste, et ne remplace jamais un fait précis par \
une formule générale. Trois faits exacts valent mieux qu'une phrase floue sur dix.
- N'y répète pas ce qui est déjà dans summary_text. Si, une fois les redites \
écartées, il ne reste rien à dire, **laisse ce bloc vide** : c'est une information \
exacte, alors qu'un bloc qui paraphrase le précédent fait perdre son temps au \
lecteur et lui donne l'impression d'avoir tout compris deux fois.

Règles impératives :
- N'écris que ce qui découle du texte fourni. N'invente aucun fait, chiffre, \
date, seuil ni conséquence qui n'y figure pas.
- Aucune prise de position : pas de jugement de valeur, pas d'adjectif laudatif \
ou critique, aucune mention de qui soutient ou combat le texte, aucune \
appréciation de son opportunité ou de son efficacité.
- Pas de méta-commentaire : écris « la loi crée… », jamais « le texte fourni \
indique que… ».
- Si la loi se borne à ratifier un accord international ou à modifier \
ponctuellement un code, dis-le simplement et reste bref.
- Français clair, phrases courtes, paragraphes séparés par une ligne vide. Pas \
de titres ni de listes à puces.
- **Le premier paragraphe doit se suffire à lui-même** : quelqu'un qui ne lit que \
lui doit savoir de quoi la loi traite et ce qu'elle change. Les paragraphes \
suivants ajoutent le détail, du plus important au moins important.
- **Les deux blocs ne se recouvrent pas** : le premier dit ce que la loi *est*, le \
second ce qu'elle *change pour vous*. Une même information ne doit apparaître \
qu'une fois. Seules exceptions : une date d'entrée en vigueur et un champ \
d'application, utiles des deux côtés.
- Sois bref. Une loi volumineuse se résume par ses lignes de force, jamais par \
l'énumération de ses articles. Ce qui n'aide pas une personne à décider de son \
vote n'a pas sa place.
- Si le texte ne permet pas de rédiger honnêtement un bloc, laisse-le vide \
plutôt que de le combler."""


def _user_prompt(law: LawText) -> str:
    header = f"{law.title}\n"
    if law.nature:
        header += f"Nature : {law.nature}\n"
    if law.signed_on:
        header += f"Promulguée le : {law.signed_on:%d/%m/%Y}\n"
    body = f"{header}\nTexte officiel :\n\n{law.plain_text}"
    # The Journal Officiel sometimes carries its own plain-language notice. It is
    # official, factual and already written for a non-specialist — exactly the
    # register « Ce que ça change en pratique » is asking for — so it is given as
    # source material rather than discarded. Still source material: the rules
    # above forbid going beyond what the text says, and this is part of the text.
    if law.notice:
        body += f"\n\nNotice publiée au Journal Officiel :\n\n{law.notice}"
    return body


def _refuse_if_oversized(
    client: Any, *, system: str, messages: list[MessageParam], what: str
) -> None:
    """Measure the request before sending it, so an outlier is refused not billed.

    Applied to both calls. The argument selection used to rely on a character
    budget alone — a ratio times a ratio — which is a good enough way to decide
    what to trim and no way at all to decide what is too expensive to send.
    """
    settings = get_settings()
    try:
        counted = client.messages.count_tokens(
            model=settings.ai_model, system=system, messages=messages
        ).input_tokens
    except Exception as exc:
        raise AiDraftError(f"impossible de mesurer le {what} : {exc}") from exc

    if counted > settings.ai_max_input_tokens:
        raise AiDraftError(
            f"{what} trop volumineux ({counted} jetons, limite "
            f"{settings.ai_max_input_tokens}) — à rédiger à la main"
        )


def generate_draft(law: LawText, *, client: Any | None = None) -> LawDraft:
    """Draft the two factual blocks for this law. Raises AiDraftError on failure."""
    settings = get_settings()
    client = build_client() if client is None else client

    if not law.articles:
        raise AiDraftError("le texte officiel ne contient aucun article exploitable")

    messages: list[MessageParam] = [{"role": "user", "content": _user_prompt(law)}]
    _refuse_if_oversized(client, system=SYSTEM_PROMPT, messages=messages, what="texte")

    try:
        response = client.messages.parse(
            model=settings.ai_model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": settings.ai_effort},
            output_format=LawDraft,
        )
    except Exception as exc:
        raise AiDraftError(str(exc)) from exc

    draft = response.parsed_output
    if draft is None:
        raise AiDraftError("réponse illisible du modèle")
    return draft


# --- Argument selection ------------------------------------------------------
#
# The factual draft above asks the model not to invent. Here invention is made
# mechanically impossible instead: the model returns the id of a speech and a
# span it claims to have copied from it, and we verify that span really occurs
# in that speech before keeping it. Attribution never comes from the model —
# speaker, date and link are copied from the Speech record we fetched, so a real
# quote cannot be pinned on the wrong person.


class ArgumentPick(BaseModel):
    speech_id: str
    quote: str


class ArgumentSelection(BaseModel):
    pour: list[ArgumentPick]
    contre: list[ArgumentPick]


@dataclass
class SelectedArguments:
    """Verified arguments, plus what was rejected and why."""

    pour: list[dict] = field(default_factory=list)
    contre: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_symmetric(self) -> bool:
        return len(self.pour) == len(self.contre)


ARGUMENTS_SYSTEM_PROMPT = """\
Tu prépares les blocs « arguments des partisans » et « arguments des opposants » \
de la présentation neutre d'une loi française, pour une plateforme civique qui ne \
prend jamais parti.

On te donne les interventions prononcées en séance publique sur ce texte, chacune \
avec un identifiant. Tu ne rédiges rien : tu **sélectionnes** des passages et tu \
les **recopies mot pour mot**.

Pour chaque camp, choisis les passages qui exposent le plus clairement une raison \
de soutenir ou de combattre le texte. Pour chaque sélection, renvoie \
l'identifiant de l'intervention et le passage exact.

Règles impératives :
- Le passage doit être une suite de caractères **copiée à l'identique** depuis \
l'intervention citée. Ne reformule pas, ne corrige pas, ne raccourcis pas au \
milieu, n'ajoute pas de crochets. Une paraphrase sera rejetée automatiquement.
- Choisis un passage qui se suffit à lui-même : une ou deux phrases entières \
exposant un motif, pas une exclamation ni une formule de politesse.
- **Le passage doit dire *pourquoi*, pas seulement *quoi*.** Une énumération de ce \
que le texte contient n'est pas un argument : il faut la conséquence redoutée ou \
espérée, le principe invoqué, le risque décrit. « Ce texte étend les techniques de \
renseignement » n'apprend rien ; « ce texte étend les techniques de renseignement \
sans que le juge puisse en contrôler l'usage » est un argument.
- Quitte à citer un peu plus long, prends la phrase qui porte la justification. Si \
un orateur énumère sans jamais justifier, cherche un autre passage ou un autre \
orateur : mieux vaut moins d'arguments que des arguments creux.
- Ne mélange pas les camps : un passage classé « contre » doit exprimer une \
opposition au texte, pas une réserve d'un orateur qui le soutient. « Nous voterons \
ce texte, mais il manque les moyens », « c'est un premier pas insuffisant » sont \
des critiques de partisans : **ne les retiens d'aucun côté**. Les publier comme \
arguments d'opposants ferait dire à leur auteur le contraire de son vote.
- Cherche l'équilibre : autant de passages d'un côté que de l'autre. Si le débat \
ne contient sincèrement aucune opposition, renvoie une liste « contre » vide — \
c'est une information exacte, et il vaut mieux la donner que de combler.
- N'invente jamais d'identifiant : n'utilise que ceux fournis."""


def _pool_prompt(law: LawText, speeches: list[Speech]) -> str:
    blocks = [
        f"[{s.speech_id}] {s.speaker}"
        + (f" ({s.quality})" if s.quality else "")
        + f" — {s.section}\n{s.text}"
        for s in speeches
    ]
    return (
        f"Loi : {law.title}\n\n"
        f"Interventions en séance publique ({len(speeches)}) :\n\n" + "\n\n".join(blocks)
    )


def _fit_budget(speeches: list[Speech], budget_chars: int) -> list[Speech]:
    """Keep the longest speeches that fit, preserving chronological order."""
    if sum(len(s.text) for s in speeches) <= budget_chars:
        return speeches
    kept, used = [], 0
    for speech in sorted(speeches, key=lambda s: len(s.text), reverse=True):
        if used + len(speech.text) > budget_chars:
            continue
        kept.append(speech)
        used += len(speech.text)
    order = {s.speech_id: i for i, s in enumerate(speeches)}
    kept.sort(key=lambda s: order[s.speech_id])
    return kept


def _verify(pick: ArgumentPick, by_id: dict[str, Speech]) -> tuple[dict | None, str]:
    """Confirm the quote occurs in the cited speech, and build the argument from ours."""
    speech = by_id.get(pick.speech_id)
    if speech is None:
        return None, f"identifiant inconnu ({pick.speech_id}) — écarté"

    quote = re.sub(r"\s+", " ", pick.quote or "").strip().strip("«»\"' ")
    if len(quote) < 40:
        return None, f"passage trop court chez {speech.speaker} — écarté"

    start = speech.text.find(quote)
    if start < 0:
        return None, f"passage introuvable dans l'intervention de {speech.speaker} — écarté"

    # Store our own substring, not the model's string.
    return {
        "text": speech.text[start : start + len(quote)],
        "author": speech.attribution,
        "source": speech.url,
    }, ""


def select_arguments(
    law: LawText,
    speeches: list[Speech],
    *,
    client: Any | None = None,
    per_side: int | None = None,
) -> SelectedArguments:
    """Select verbatim arguments for and against, from the sitting records."""
    settings = get_settings()
    per_side = settings.ai_arguments_per_side if per_side is None else per_side
    if not speeches:
        raise AiDraftError("aucune intervention exploitable dans les comptes rendus")
    client = build_client() if client is None else client

    # Roughly 3.6 characters per token in French; leave room for the instructions.
    budget = int(settings.ai_max_input_tokens * 3.6 * 0.8)
    # The **last** ones, not the first. Sittings arrive oldest-first — the import
    # sorts them by date ascending — so slicing from the front kept the earliest
    # readings and discarded the latest, which is backwards: the closer a debate
    # is to the vote, the closer the text being argued about is to the one that
    # was actually adopted. Every other cut below is by length, on purpose; this
    # one is by position, and position here means date.
    pool = _fit_budget(speeches[-settings.ai_max_speeches * 4 :], budget)
    if len(pool) > settings.ai_max_speeches:
        order = {s.speech_id: i for i, s in enumerate(pool)}
        longest = sorted(pool, key=lambda s: len(s.text), reverse=True)
        pool = sorted(longest[: settings.ai_max_speeches], key=lambda s: order[s.speech_id])
    # Counted once, against everything we were given: the pool is trimmed twice
    # (a window on the most recent, then the budget) and each step alone
    # under-reports.
    dropped = len(speeches) - len(pool)
    notes = []
    if dropped:
        notes.append(f"{dropped} intervention(s) écartée(s) faute de place dans la requête")

    instruction = (
        f"Sélectionne au plus {per_side} passages pour chaque camp, en recopiant "
        "chaque passage mot pour mot. « Au plus » : n'atteins ce nombre que si les "
        "débats fournissent autant de passages qui exposent réellement un motif."
    )
    messages: list[MessageParam] = [
        {"role": "user", "content": f"{_pool_prompt(law, pool)}\n\n{instruction}"}
    ]
    _refuse_if_oversized(
        client, system=ARGUMENTS_SYSTEM_PROMPT, messages=messages, what="corpus de séance"
    )

    try:
        response = client.messages.parse(
            model=settings.ai_model,
            max_tokens=16000,
            system=ARGUMENTS_SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": settings.ai_effort},
            output_format=ArgumentSelection,
        )
    except Exception as exc:
        raise AiDraftError(str(exc)) from exc

    selection = response.parsed_output
    if selection is None:
        raise AiDraftError("réponse illisible du modèle")

    by_id = {s.speech_id: s for s in pool}
    result = SelectedArguments(notes=notes)
    sides = (("pour", selection.pour), ("contre", selection.contre))
    for side, picks in sides:
        for pick in picks[:per_side]:
            argument, why = _verify(pick, by_id)
            if argument is None:
                result.notes.append(why)
            else:
                getattr(result, side).append(argument)

    if not (result.pour or result.contre):
        raise AiDraftError("aucun passage n'a pu être vérifié dans les comptes rendus")
    if not result.is_symmetric:
        result.notes.append(
            f"déséquilibre : {len(result.pour)} pour / {len(result.contre)} contre — "
            "à compléter ou à assumer explicitement"
        )
    return result
