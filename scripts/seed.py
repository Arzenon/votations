"""Seed a few sample laws so the platform can be clicked through.

Idempotent: laws are keyed by slug and skipped if they already exist.
Run with:  python -m scripts.seed
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta

from app.db import SessionLocal
from app.models import Law, Vote, VoteChoice


def _utc(days: int = 0, hours: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(days=days, hours=hours)


SAMPLE_LAWS = [
    {
        "slug": "transparence-algorithmes-plateformes",
        "title": "Loi sur la transparence des algorithmes des grandes plateformes",
        "official_ref": "LOI n° 2026-214 du 3 mars 2026",
        "legifrance_url": "https://www.legifrance.gouv.fr/",
        "summary_text": (
            "La loi impose aux plateformes numériques de plus de 10 millions "
            "d'utilisateurs de publier une description des principaux critères "
            "utilisés par leurs algorithmes de recommandation et de modération."
        ),
        "practical_changes": (
            "Les très grandes plateformes devront mettre à disposition du public "
            "une fiche explicative par algorithme et permettre à un organisme "
            "indépendant d'en auditer le fonctionnement une fois par an."
        ),
        "arguments_for": [
            {
                "text": "Renforce la confiance et permet de comprendre pourquoi un "
                "contenu est mis en avant ou retiré.",
                "author": "Collectif pour une information loyale",
                "source": "https://example.org/pour-transparence",
            },
            {
                "text": "Aligne la France sur les obligations du règlement européen "
                "sur les services numériques (DSA).",
                "author": "Rapporteur de la commission des lois",
                "source": "https://example.org/rapport-dsa",
            },
        ],
        "arguments_against": [
            {
                "text": "Publier les critères des algorithmes faciliterait leur "
                "contournement par les acteurs malveillants.",
                "author": "Fédération des entreprises du numérique",
                "source": "https://example.org/contre-securite",
            },
            {
                "text": "Le coût de conformité pèsera surtout sur les plateformes "
                "européennes face aux géants mondiaux.",
                "author": "Association des start-up françaises",
                "source": "https://example.org/contre-cout",
            },
        ],
        # Open now.
        "published": True,
        "opens_at": _utc(days=-5),
        "closes_at": _utc(days=10),
        "votes": None,
    },
    {
        "slug": "encadrement-loyers-zones-tendues",
        "title": "Loi d'encadrement des loyers dans les zones tendues",
        "official_ref": "LOI n° 2026-187 du 19 février 2026",
        "legifrance_url": "https://www.legifrance.gouv.fr/",
        "summary_text": (
            "Le texte étend l'encadrement des loyers à l'ensemble des communes "
            "situées en zone tendue et plafonne les hausses lors d'une relocation."
        ),
        "practical_changes": (
            "Dans les communes concernées, le loyer d'un logement remis en location "
            "ne pourra dépasser un loyer de référence majoré fixé par arrêté "
            "préfectoral. Les dépassements pourront être contestés par le locataire."
        ),
        "arguments_for": [
            {
                "text": "Protège le pouvoir d'achat des locataires dans les villes "
                "où la demande est très supérieure à l'offre.",
                "author": "Confédération nationale du logement",
                "source": "https://example.org/pour-loyers",
            },
        ],
        "arguments_against": [
            {
                "text": "Risque de réduire l'offre locative en décourageant "
                "l'investissement et la mise en location.",
                "author": "Union nationale des propriétaires",
                "source": "https://example.org/contre-loyers",
            },
        ],
        # Closed yesterday — results visible, with sample votes.
        "published": True,
        "opens_at": _utc(days=-30),
        "closes_at": _utc(days=-1),
        "votes": {"for": 412, "against": 233, "abstain": 61},
    },
    {
        "slug": "semaine-quatre-jours-fonction-publique",
        "title": "Loi expérimentant la semaine de quatre jours dans la fonction publique",
        "official_ref": "Projet de loi déposé le 2 juin 2026",
        "legifrance_url": "https://www.legifrance.gouv.fr/",
        "summary_text": (
            "Le projet autorise une expérimentation de trois ans de la semaine de "
            "quatre jours, à temps de travail constant, dans les administrations "
            "volontaires."
        ),
        "practical_changes": (
            "Les agents des administrations participantes pourront répartir leur "
            "temps de travail hebdomadaire sur quatre jours. Un bilan sera remis "
            "au Parlement au bout de trois ans."
        ),
        "arguments_for": [
            {
                "text": "Améliore la qualité de vie au travail et l'attractivité "
                "des emplois publics.",
                "author": "Syndicat de la fonction publique",
                "source": "https://example.org/pour-4jours",
            },
        ],
        "arguments_against": [
            {
                "text": "Pourrait dégrader la continuité du service public si "
                "l'organisation n'est pas adaptée.",
                "author": "Association des maires de France",
                "source": "https://example.org/contre-4jours",
            },
        ],
        # Scheduled to open in a week.
        "published": True,
        "opens_at": _utc(days=7),
        "closes_at": _utc(days=37),
        "votes": None,
    },
]


def _seed_votes(db, law: Law, counts: dict[str, int]) -> None:
    rng = random.Random(law.slug)  # deterministic per law
    window_start = law.opens_at
    window_end = law.closes_at
    span = (window_end - window_start).total_seconds()
    n = 0
    for choice, count in counts.items():
        for _ in range(count):
            # Unique anonymous token, hashed exactly as the real provider does.
            token = hashlib.sha256(f"{law.slug}-{n}".encode()).hexdigest()
            created = window_start + timedelta(seconds=rng.random() * span)
            db.add(
                Vote(
                    law_id=law.id,
                    voter_token=token,
                    choice=VoteChoice(choice).value,
                    created_at=created,
                )
            )
            n += 1


def main() -> None:
    db = SessionLocal()
    created = 0
    try:
        for spec in SAMPLE_LAWS:
            votes = spec.pop("votes")
            if db.query(Law).filter_by(slug=spec["slug"]).first():
                print(f"• skip (exists): {spec['slug']}")
                spec["votes"] = votes
                continue
            law = Law(**spec)
            db.add(law)
            db.flush()  # assign id
            if votes:
                _seed_votes(db, law, votes)
            created += 1
            print(f"✓ created: {law.slug}")
            spec["votes"] = votes
        db.commit()
    finally:
        db.close()
    print(f"\nDone. {created} new law(s) seeded.")


if __name__ == "__main__":
    main()
