# Votations Françaises

A civic platform where French citizens give their opinion on laws passed by
Parliament, inspired by Swiss popular votes. It doesn't replace the legislative
process: it offers a transparent, accessible and honest space for expression.
Technical documentation for developers is in `README.md` (in French).

## Principles

- **Transparency.** The system's limitations are displayed permanently. Results
  are never presented as representative of the French population, only as the
  opinion of the people who took part. Data is published as open data, raw and
  anonymised, and the evolution of votes over time is visible so organised
  campaigns can be spotted.
- **Neutrality.** Every law is presented with the same structure:
  1. **Ce que dit la loi**: factual summary of the official text, no
     interpretation.
  2. **Ce que ça change en pratique**: concrete effects, in plain language.
  3. **Arguments des partisans**: sourced and attributed, no comment.
  4. **Arguments des opposants**: sourced and attributed, no comment.

  The platform aims for objectivity as far as possible.
- **Simplicity.** Usable by everyone: on mobile, on a slow connection, without
  being comfortable with technology. An ethical choice as much as a technical
  one.
- **Resistance to manipulation.** Statistics stay simple: for, against, abstain,
  number of voters and the evolution curve. No cross-referencing of data without
  enough volume to prevent re-identification. The same threshold applies to the
  export: below it, the export has no timestamps, otherwise knowing when someone
  voted would reveal how they voted.
- **No personal data.** A vote must not be linkable to an identity, even
  indirectly. No browsing, location or behavioural data is collected.

## What it is not

- **A representative poll**: participants select themselves and results can't
  be generalised.
- **A political tool**: the platform supports no side and makes no
  recommendation.
- **A social network**: no comments, no likes, nothing designed to create
  dependency or polarisation.

## Features

- The home page highlights the latest laws put to the vote, with a search over
  all laws.
- A law page shows the four blocks and the ballot (for, against, abstain), with
  the representativeness disclaimer.
- Results are public for the whole duration of the vote: the results page shows
  a provisional count and raw data can be downloaded at any time. Hiding the
  figures until closing would mean asking people to trust a number they can't
  check. The law page itself only shows the count once voting has closed.
- Laws come from the Journal Officiel. Presentations are written by hand or with
  AI assistance, then reviewed and validated by a person before publication.
- The admin creates and manages votations, sets opening and closing dates, and
  follows votes in real time.

## Roadmap

| Phase | Goal |
|---|---|
| MVP | Home page, vote page, results, admin, provisional authentication |
| V1 | Advanced search, raw data export, evolution curve |
| V2 | Certified uniqueness through a private identity provider |
| V3 | Admin opened to all authenticated users |

FranceConnect is deliberately absent: see below.

## Authentication

Authentication has one purpose: **one person, one vote**. It doesn't need to know
who votes or their nationality, and must not retain personal data.

Today `AnonymousProvider` stores a random token in a signed cookie (hashed in the
database). It's a weak guarantee, and the site's disclaimer says so.

### FranceConnect is not available

The obstacle is legal, not technical. The arrêté of 8 November 2018 (NOR
PRMJ1819224A), article 4, as amended by the arrêté of 17 March 2026, admits four
categories of service provider: administrative authorities empowered by a legal
text; change-of-address services; administrative authorities running electronic
voting for their own professional elections; and private legal entities under an
explicit statutory obligation to verify their users' identity. A private civic
platform falls under none of them. Only a change in the project's legal nature
(being carried by a public authority) would change that.

These findings are established and not to be re-litigated:

- **FranceConnect doesn't transmit nationality.** The pivot identity is limited
  to surname, given names, usual name, sex, date of birth, INSEE codes of the
  place and country of birth, a verified email and a `sub` identifier unique to
  each service. Country of birth is not nationality, and using it as such would
  be wrong and discriminatory.
- It proves attachment to the French administration, not citizenship. This is
  accepted: laws apply to those who live in France.

If the project were ever carried by a public authority, the `sub` identifier
alone, stored hashed, would ensure uniqueness without keeping any civil status
data. Otherwise, the only route to certified uniqueness is a private identity
provider such as L'Identité Numérique La Poste (eIDAS substantial level), which
costs money and covers far fewer people.

Whatever the mechanism, the disclaimer states plainly that voters are people
attached to the French administration, that one person carries one vote, and
that **nationality is not verified**.

## Git

- Never commit: the maintainer reviews and commits changes themselves.
- Never push to `master`.

## Code conventions

- Comments and docstrings are in **English**, short, and only explain the *why*
  when it isn't obvious. No narrative history of past bugs.
- Everything users or operators read is in **French**: the UI, displayed
  messages, logs, `README.md`, `DEPLOY.md`, `.env.example`.
- French domain terms stay in French, even in English comments (votation,
  scrutin, CMP, compte rendu, Journal Officiel).
- Never use em dashes (—) or en dashes (–), in code, comments or docs. Use a
  comma, colon or parentheses instead.
