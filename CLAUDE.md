# Votations Françaises — Citizens' Voice on French Legislation

## Vision

Votations Françaises is a civic platform that allows French citizens to express their opinion on laws passed by Parliament, inspired by the Swiss popular vote model. The goal is not to replace the legislative process, but to offer a transparent, accessible, and honest space for democratic expression.

The project is built on one cardinal principle: **radical transparency**. Where polls hide their biases and media outlets frame their analyses, Votations Françaises exposes its limitations, publishes its data, and lets every citizen form their own opinion.

---

## Founding Principles

### 1. Transparency First

- System limitations are displayed permanently and prominently.
- Results are never presented as representative of the French population as a whole, but as the expression of people registered on the platform.
- All collected data is published as open access, in raw anonymised format, to enable independent analysis.
- The evolution of votes over time is visible, in order to detect any organised mobilisation campaigns.

### 2. Objectivity in the Presentation of Laws

Each law is presented according to a strict, neutral structure:

- **What the law says**: factual summary of the official text, without interpretation.
- **What it changes in practice**: practical impacts for citizens, written in accessible language.
- **Arguments put forward by supporters**: sourced, attributed, without editorial comment.
- **Arguments put forward by opponents**: sourced, attributed, without editorial comment.

The platform takes no position. Symmetry between arguments for and against is systematically maintained.

### 3. Simplicity and Accessibility

The site must be usable by everyone: on mobile and desktop, on slow connections, by people who are not digitally confident. Interface simplicity is not a technical constraint — it is an ethical choice.

### 4. Anti-Manipulation

Displayed statistics are deliberately simple and resistant to manipulation:
- Percentage for / against
- Total number of voters
- Vote evolution curve over time

No cross-referencing of data will be displayed without guaranteeing sufficient volume to prevent re-identification or partisan exploitation.

---

## What Votations Françaises Is Not

- **It is not a representative poll.** Participants self-select. Results cannot be generalised to the French population.
- **It is not a political tool.** The platform takes no side, supports no movement, and makes no recommendations.
- **It is not a social network.** There are no comments, likes, or engagement mechanisms designed to create dependency or polarisation.

---

## Core Features

### Home Page
- Highlights the latest laws submitted to a vote
- Search bar to find any law in the database, regardless of age

### Law / Vote Page
- Neutral presentation of the law (4-block structure described above)
- Voting interface (for / against / abstain)
- Permanent disclaimer on representativeness limitations
- Results displayed after the vote closes, with raw data available for download

### Administration Page
- Creation and management of laws submitted to a vote
- Configuration of opening and closing dates
- Real-time vote tracking
- Export of anonymised data

---

## Authentication

### Provisional Phase
Pending integration of FranceConnect, the provisional authentication mechanism is yet to be decided. Whatever solution is chosen, the site disclaimer will explicitly state how it works and what its limitations are.

### Target Phase
Integration of **FranceConnect** for authentication tied to civic identity, subject to the legal and technical requirements of the DILA. This step requires a mature project and specific administrative procedures.

---

## Data and Privacy

The goal is to collect **no personal data whatsoever**, which radically simplifies GDPR obligations and builds user trust. The technical design must ensure that a vote cannot be linked to a real identity, even indirectly.

- Data exported as open access contains no information that could identify a voter.
- No browsing, location, or behavioural data is collected.

---

## Source of Laws

Laws are sourced from the **Journal Officiel de la République Française**, accessible via the public Légifrance API (DILA). Presentation summaries are written manually or AI-assisted, then reviewed and validated by a human before publication, to ensure neutrality and accuracy.

---

## Roadmap

| Phase | Goal |
|---|---|
| MVP | Home page, vote page, results page, admin, provisional auth |
| V1 | Advanced search, raw data export, vote evolution curve |
| V2 | FranceConnect integration |
| V3 | Opening the admin page to all authenticated users |

---

*This document defines the vision and values of the project. Technology choices will be documented separately as development progresses.*
