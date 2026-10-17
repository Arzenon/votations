"""AI-assisted first draft of a law's presentation.

`generate_draft` writes the two factual blocks ("Ce que dit la loi" and "Ce que
ça change en pratique") from the Journal Officiel text.

Arguments are quoted, not written: `select_arguments` gets the speeches from
the sitting records (see `app.debates`), the model returns a speech id and a
quote, and the quote is checked against that speech. Speaker, date and link
come from our own data, never from the model.

Drafts only run when an admin asks, are stored to avoid paying twice, and only
pre-fill a form. Inputs are measured before sending so huge texts are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.config import get_settings
from app.debates import Speech
from app.legifrance import LawText

if TYPE_CHECKING:  # the SDK is imported lazily, the feature is optional
    from anthropic.types import MessageParam


class AiDraftError(RuntimeError):
    """No draft could be produced; the admin writes the blocks by hand."""


# Attempts per call, retry included (the SDK default is 3). The timeout is
# divided by this so retries fit in the draft's budget, see
# `app.drafting._within`.
AI_ATTEMPTS = 2


def build_client() -> Any:
    """An Anthropic client, or AiDraftError if no key is configured.

    Shared by the four requests of a draft. Imports the SDK lazily.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AiDraftError(
            "aucune clé API configurée - renseignez ANTHROPIC_API_KEY pour activer les brouillons"
        )
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=AI_ATTEMPTS - 1)


class LawDraft(BaseModel):
    """The two factual blocks."""

    summary_text: str
    practical_changes: str


SYSTEM_PROMPT = """\
Tu prépares la présentation neutre d'une loi française promulguée, pour une \
plateforme civique qui ne prend jamais parti.

On te donne le texte officiel publié au Journal Officiel. Tu produis deux blocs :

1. summary_text - « Ce que dit la loi » : **l'idée de la loi, pas son plan**. De \
quoi elle traite, ce qu'elle cherche à obtenir - et ce qui, le cas échéant, n'est \
pas en vigueur (censure du Conseil constitutionnel, entrée en vigueur différée). La \
loi vue de l'extérieur.

Règles propres à ce premier bloc :
- **N'y détaille pas les obligations qui en découlent pour les gens.** C'est le \
travail du second bloc, qui le fait depuis la place du lecteur et le fait mieux. \
Nomme le domaine sur lequel la loi intervient, pas la règle qu'elle y produit.
- Aussi court que le texte le permet : deux ou trois paragraphes suffisent pour la \
plupart des lois.
- **Écris pour quelqu'un qui n'a pas de connaissances particulières en droit.** Ce \
bloc est le premier que lit une personne qui hésite sur son vote, et trois habitudes \
de juriste le lui rendent illisible :
  - **Ne cite aucun numéro d'article ni de code.** Ni « article L. 1111-12-1 », ni \
« article 122-4 du code pénal », ni « les articles 16, 26, 32 et 35 ont été \
déclarés non conformes ». Le texte officiel est en lien sur la page pour qui veut \
les références ; ici elles n'apprennent rien à personne. Dis *ce que* le Conseil \
constitutionnel a censuré, jamais sous quels numéros.
  - **N'énumère pas les codes modifiés.** « Elle modifie les codes de la défense, \
de la commande publique, de la sécurité intérieure, de l'environnement, de \
l'urbanisme… » est une liste que personne ne lit et dont personne ne retient rien. \
Nomme en français ordinaire les **domaines** sur lesquels la loi agit, et seulement \
ceux qui portent l'essentiel.
- Une exception : si la loi ne crée d'obligation pour personne - ratification d'un \
accord, réorganisation administrative, mesure purement budgétaire - le second bloc \
sera vide et celui-ci porte seul la présentation. Il peut alors être plus détaillé.

2. practical_changes - « Ce que ça change en pratique » : **pas le premier bloc \
reformulé en langage simple**, mais la réponse à une autre question - « est-ce que \
cela me concerne, et qu'est-ce que je dois faire différemment ? ».

Règles propres à ce second bloc :
- Organise-le par **personnes concernées** (« si vous êtes locataire », « pour les \
employeurs », « pour les communes »), jamais en suivant le plan du texte.
- **Commence chaque paragraphe par le public concerné, suivi d'un deux-points** : \
« Pour les employeurs : vous devez… », « Si vous êtes réserviste : la durée… ». La \
page met cette étiquette en évidence pour qu'on puisse parcourir le bloc et y \
trouver son cas sans le lire en entier. Garde-la courte - c'est un public, pas une \
phrase.
- **L'étiquette fait au plus 75 caractères et ne se termine par aucun signe de \
ponctuation forte** (point, point-virgule, point d'interrogation ou \
d'exclamation) : au-delà, la page cesse de la reconnaître et rend le paragraphe en \
texte plein. La virgule est permise - « Pour les établissements de santé, EHPAD et \
services médico-sociaux » est un public que quelqu'un reconnaît. Préfère quand \
même le terme qui couvre tout le monde et remets le détail *à l'intérieur* du \
paragraphe ; et si l'énumération cache deux lecteurs qui n'ont pas les mêmes \
obligations, fais-en deux paragraphes plutôt que de n'en garder qu'un.
- **Un public, c'est la façon dont quelqu'un se décrit lui-même** - pas la \
catégorie que vise l'article. Personne ne se présente comme « irrigant », \
« betteravier », « éleveur d'ovins » ou « producteur de lait au sein d'une \
organisation de producteurs reconnue » : ces gens-là se disent **agriculteurs**, et \
c'est sous ce mot qu'ils se reconnaîtront. Regroupe-les, et mets les seuils, les \
dates et les cas particuliers *à l'intérieur* du paragraphe.
- Passe chaque paragraphe à cette question : **quelqu'un peut-il dire « ça, c'est \
moi » ?** Si la réponse tient à une qualité juridique qu'il faut déjà connaître pour \
se l'appliquer, le paragraphe est mal découpé - sa mesure appartient au public plus \
large dont ces personnes font partie. **Ne supprime jamais la mesure pour autant** : \
la remonter d'un cran, oui ; l'écarter parce que peu de gens sont concernés, jamais.
- **Commence par les publics les plus nombreux**, finis par les plus étroits : \
quelqu'un qui s'arrête en cours de lecture doit avoir lu ce qui le concernait le plus.
- Pour chacune : ce qu'elle doit ou peut faire qu'elle ne pouvait pas avant, à \
partir de quand, et ce qui arrive si elle ne le fait pas - lorsque le texte le dit.
- Dis ce qui **ne change pas** lorsque le sujet ou l'intitulé de la loi pourrait \
laisser croire le contraire.
- **Un paragraphe court par public concerné : deux ou trois phrases.** Choisis ce \
qui décide - suis-je concerné, que dois-je faire, à partir de quand - sans jamais \
remplacer un fait précis par une formule générale.
- Le nombre de publics n'est pas plafonné : en écarter serait choisir de qui on ne \
parle pas. Mais il suit le nombre de **lecteurs** concernés et non le nombre de \
mesures - une loi qui creuse un seul secteur peut n'en compter que trois ou quatre.
- N'y répète pas ce qui est déjà dans summary_text - mais c'est alors le premier \
bloc qui doit se raccourcir, jamais celui-ci qui se vide. Un bloc qui paraphrase le \
précédent fait perdre son temps au lecteur ; un bloc vide sur une loi qui change \
quelque chose lui cache ce qu'il est venu chercher, ce qui est pire.
- **Ne laisse ce bloc vide que si la loi ne crée d'obligation ni de droit nouveau \
pour personne** - ratification d'un accord, mesure purement budgétaire, \
réorganisation interne à l'État. Dès que le texte impose, autorise ou interdit \
quelque chose à quelqu'un, il y a un public à qui le dire : le bloc n'est pas vide, \
il est à écrire.

Règles impératives :
- N'écris que ce qui découle du texte fourni. N'invente aucun fait, chiffre, \
date, seuil ni conséquence qui n'y figure pas.
- Aucune prise de position : pas de jugement de valeur, pas d'adjectif laudatif \
ou critique, aucune mention de qui soutient ou combat le texte, aucune \
appréciation de son opportunité ou de son efficacité.
- Pas de méta-commentaire : écris « la loi crée… », jamais « le texte fourni \
indique que… ».
- Français clair, phrases courtes, paragraphes séparés par une ligne vide. Pas \
de titres ni de listes à puces.
- **N'utilise jamais le tiret cadratin (—) ni le tiret demi-cadratin (–).** Pour \
une incise, écris deux phrases, ou utilise une virgule, un deux-points ou des \
parenthèses. Si un tiret reste nécessaire, prends le tiret simple (-).
- **Le premier paragraphe doit se suffire à lui-même** : quelqu'un qui ne lit que \
lui doit savoir de quoi la loi traite et ce qu'elle change. Les paragraphes \
suivants ajoutent le détail, du plus important au moins important.
- **Les deux blocs ne se recouvrent pas** : le premier dit ce que la loi *est*, le \
second ce qu'elle *change pour vous*. Une même information ne doit apparaître \
qu'une fois - et **quand elle pourrait tenir des deux côtés, elle appartient au \
second**, parce que c'est là que le lecteur la cherche. Le premier nomme alors le \
sujet sans la conséquence - « la loi encadre l'usage de certains insecticides » - \
et laisse au second le produit, le seuil, la date et la démarche. Seules \
exceptions : une date d'entrée en vigueur et un champ d'application, utiles des \
deux côtés.
- Sois bref. Une loi volumineuse se résume par ses lignes de force, jamais par \
l'énumération de ses articles. Ce qui n'aide pas une personne à décider de son \
vote n'a pas sa place."""


def _user_prompt(law: LawText) -> str:
    header = f"{law.title}\n"
    if law.nature:
        header += f"Nature : {law.nature}\n"
    if law.signed_on:
        header += f"Promulguée le : {law.signed_on:%d/%m/%Y}\n"
    body = f"{header}\nTexte officiel :\n\n{law.plain_text}"
    # The JO sometimes includes a plain-language notice, useful for the second block.
    if law.notice:
        body += f"\n\nNotice publiée au Journal Officiel :\n\n{law.notice}"
    return body


def _refuse_if_oversized(
    client: Any, *, system: str, messages: list[MessageParam], what: str
) -> None:
    """Count tokens first and refuse oversized requests instead of paying for them."""
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
            f"{settings.ai_max_input_tokens}), à rédiger à la main"
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
# The model returns (speech id, quote) and we check the quote really appears in
# that speech. Attribution comes from our Speech record, not from the model.


class ArgumentPick(BaseModel):
    speech_id: str
    quote: str


class ArgumentSelection(BaseModel):
    pour: list[ArgumentPick]
    contre: list[ArgumentPick]
    # What the debate was about (a subject, never a position). Optional so that
    # a response without it doesn't fail validation and lose the quotes.
    sujet_debat: str = ""


@dataclass
class SelectedArguments:
    """Verified arguments, plus what was rejected and why."""

    pour: list[dict] = field(default_factory=list)
    contre: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Written by the model, unlike the quotes.
    sujet_debat: str = ""

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
ne contient sincèrement aucune opposition, renvoie une liste « contre » vide - \
c'est une information exacte, et il vaut mieux la donner que de combler.
- N'invente jamais d'identifiant : n'utilise que ceux fournis.

Enfin, renvoie `sujet_debat` : **une ou deux phrases nommant ce sur quoi le débat \
a porté**. C'est le seul texte que tu rédiges ici, et il obéit à une règle stricte.

- **Nomme le sujet du désaccord, jamais une position sur ce sujet.** « Le débat a \
porté sur le contrôle judiciaire des techniques de renseignement et sur la \
compensation des petites entreprises » : correct. « Cette loi affaiblit le \
contrôle judiciaire » : c'est l'argument d'un camp, et le reprendre revient à \
prendre parti.
- N'écris que ce qui ressort des passages que tu viens de sélectionner. Le \
lecteur les a sous les yeux, juste en dessous : ta phrase doit pouvoir être \
vérifiée contre eux.
- **Si les débats ne contiennent pas d'opposition réelle**, écris-le simplement : \
« Le texte n'a pas suscité d'opposition en séance. » C'est une information exacte \
et souvent la plus utile.
- Pas d'adjectif d'appréciation, pas de « vif débat », pas de « polémique ». Un \
sujet, éventuellement deux, et rien d'autre.
- Cette phrase-ci est de toi : n'y emploie ni tiret cadratin (—) ni tiret \
demi-cadratin (–). Les passages cités, eux, restent recopiés à l'identique."""


def _pool_prompt(law: LawText, speeches: list[Speech]) -> str:
    blocks = [
        f"[{s.speech_id}] {s.speaker}"
        + (f" ({s.quality})" if s.quality else "")
        + f", {s.section}\n{s.text}"
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
    """Check the quote appears in the cited speech and build the argument from it."""
    speech = by_id.get(pick.speech_id)
    if speech is None:
        return None, f"identifiant inconnu ({pick.speech_id}), écarté"

    quote = re.sub(r"\s+", " ", pick.quote or "").strip().strip("«»\"' ")
    if len(quote) < 40:
        return None, f"passage trop court chez {speech.speaker}, écarté"

    start = speech.text.find(quote)
    if start < 0:
        return None, f"passage introuvable dans l'intervention de {speech.speaker}, écarté"

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

    # About 3.6 characters per token in French, with room for the instructions.
    budget = int(settings.ai_max_input_tokens * 3.6 * 0.8)
    # Keep the most recent speeches: later debates are about a text closer to
    # the one adopted. Speeches are sorted oldest first.
    pool = _fit_budget(speeches[-settings.ai_max_speeches * 4 :], budget)
    if len(pool) > settings.ai_max_speeches:
        order = {s.speech_id: i for i, s in enumerate(pool)}
        longest = sorted(pool, key=lambda s: len(s.text), reverse=True)
        pool = sorted(longest[: settings.ai_max_speeches], key=lambda s: order[s.speech_id])
    # Count against the full input, since the pool was trimmed twice.
    dropped = len(speeches) - len(pool)
    notes = []
    if dropped:
        notes.append(f"{dropped} intervention(s) écartée(s) faute de place dans la requête")

    instruction = (
        f"Sélectionne au plus {per_side} passages pour chaque camp, en recopiant "
        "chaque passage mot pour mot. « Au plus » : n'atteins ce nombre que si les "
        "débats fournissent autant de passages qui exposent réellement un motif. "
        "Renvoie aussi `sujet_debat` : une ou deux phrases nommant ce sur quoi le "
        "débat a porté, sans prendre parti."
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
    result = SelectedArguments(notes=notes, sujet_debat=(selection.sujet_debat or "").strip())
    sides = (("pour", selection.pour), ("contre", selection.contre))
    from_joint = 0
    for side, picks in sides:
        for pick in picks[:per_side]:
            argument, why = _verify(pick, by_id)
            if argument is None:
                result.notes.append(why)
            else:
                getattr(result, side).append(argument)
                from_joint += bool(by_id[pick.speech_id].joint_debate)

    if not (result.pour or result.contre):
        raise AiDraftError("aucun passage n'a pu être vérifié dans les comptes rendus")
    if from_joint:
        # In a joint debate, a genuine quote may be about the other text.
        result.notes.append(
            f"{from_joint} passage(s) proviennent d'une discussion commune à "
            "plusieurs textes : vérifiez qu'ils portent bien sur cette loi avant "
            "de publier, l'orateur pouvait parler de l'autre."
        )
    if not result.is_symmetric:
        result.notes.append(
            f"déséquilibre : {len(result.pour)} pour / {len(result.contre)} contre, "
            "à compléter ou à assumer explicitement"
        )
    return result
