"""Seed a few sample laws so the platform can be clicked through.

Idempotent: laws are keyed by slug and skipped if they already exist.
Run with:  python -m scripts.seed
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal, utcnow
from app.models import DemographicTally, Law, Vote, VoteChoice


def _utc(days: int = 0, hours: int = 0) -> datetime:
    return utcnow() + timedelta(days=days, hours=hours)


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
        "demographics": None,
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
        # Aggregate demographics (only a subset of voters provided them). Every
        # cell is well above the suppression threshold so the breakdown shows.
        "demographics": {
            "age": {
                "25-34": {"for": 60, "against": 22, "abstain": 12},
                "35-49": {"for": 95, "against": 51, "abstain": 14},
                "50-64": {"for": 48, "against": 60, "abstain": 12},
                "65+": {"for": 20, "against": 44, "abstain": 11},
            },
            "sexe": {
                "femme": {"for": 110, "against": 92, "abstain": 25},
                "homme": {"for": 103, "against": 81, "abstain": 18},
            },
            "region": {
                "Île-de-France": {"for": 30, "against": 14, "abstain": 11},
                "Auvergne-Rhône-Alpes": {"for": 24, "against": 22, "abstain": 10},
                "Nouvelle-Aquitaine": {"for": 18, "against": 28, "abstain": 10},
                "Occitanie": {"for": 16, "against": 30, "abstain": 12},
                "Hauts-de-France": {"for": 14, "against": 26, "abstain": 11},
            },
        },
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
        "demographics": None,
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


def _seed_demographics(db, law: Law, data: dict) -> None:
    for dimension, categories in data.items():
        for category, choices in categories.items():
            for choice, count in choices.items():
                db.add(
                    DemographicTally(
                        law_id=law.id,
                        dimension=dimension,
                        category=category,
                        choice=VoteChoice(choice).value,
                        count=count,
                    )
                )

    # Keep the profile-provision rate coherent with the seeded counters: mark
    # `n` votes as having shared a profile, where n is the largest dimension total
    # (i.e. assume everyone who shared anything shared at least their age).
    n = max(
        sum(c for choices in categories.values() for c in choices.values())
        for categories in data.values()
    )
    votes = db.scalars(select(Vote).where(Vote.law_id == law.id).limit(n)).all()
    for v in votes:
        v.demographics_recorded = True
        # Which choice their profile was counted under, so that changing the vote
        # moves the counters rather than leaving them stale.
        v.demographics_choice = v.choice


def _has_demographics(db, law_id: int) -> bool:
    return (
        db.scalar(select(DemographicTally.id).where(DemographicTally.law_id == law_id)) is not None
    )


def main() -> None:
    db = SessionLocal()
    created = 0
    try:
        for spec in SAMPLE_LAWS:
            # Copied, not mutated: `SAMPLE_LAWS` is module state, and popping the
            # two extra keys out of it and putting them back left the module in a
            # different shape on every path through this loop.
            columns = {k: v for k, v in spec.items() if k not in ("votes", "demographics")}
            votes, demographics = spec["votes"], spec["demographics"]

            existing = db.scalar(select(Law).where(Law.slug == columns["slug"]))
            if existing is not None:
                # Top up demographics if this law has none yet (idempotent).
                if demographics and not _has_demographics(db, existing.id):
                    _seed_demographics(db, existing, demographics)
                    print(f"↻ added demographics: {columns['slug']}")
                else:
                    print(f"• skip (exists): {columns['slug']}")
                continue

            law = Law(**columns)
            db.add(law)
            db.flush()  # assign id
            if votes:
                _seed_votes(db, law, votes)
            if demographics:
                _seed_demographics(db, law, demographics)
            created += 1
            print(f"✓ created: {law.slug}")
        db.commit()
    finally:
        db.close()
    print(f"\nDone. {created} new law(s) seeded.")


if __name__ == "__main__":
    main()
