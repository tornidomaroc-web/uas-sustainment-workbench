"""What OSO #03 asks for, paraphrased from the public texts and cited by edition and page.

The JARUS documents may be used but not copied without permission, so every item below is
a paraphrase in this project's words, with the document, edition and page it comes from.
Nothing here decides anything: an item's status in a pack says only whether the workbench
holds records that bear on it.
"""

from __future__ import annotations

from typing import Any

SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "annex-e-2.5",
        "title": "JARUS guidelines on SORA, Annex E: integrity and assurance levels for the "
        "operational safety objectives",
        "document": "JAR-DEL-SRM-SORA-E-2.5",
        "edition": "2.5",
        "date": "2024-05-13",
        "pages": "12 to 14 (OSO #03)",
        "url": "http://jarus-rpas.org/wp-content/uploads/2024/06/SORA-v2.5-Annex-E-Release.JAR_doc_28pdf.pdf",
        "note": "The current JARUS text. Ties the low, medium and high levels to SAIL I and "
        "II, III and IV, V and VI.",
    },
    {
        "id": "annex-e-1.0",
        "title": "JARUS guidelines on SORA, Annex E: integrity and assurance levels for the "
        "operational safety objectives",
        "document": "JAR-DEL-WG6-D.04",
        "edition": "1.0",
        "date": "2019-01-25",
        "pages": "6 (OSO #03)",
        "url": "http://jarus-rpas.org/wp-content/uploads/2023/07/jar_doc_06_jarus_sora_annex_e_v1.0_.pdf",
        "note": "The text carried by the AMC to Article 11 of Regulation (EU) 2019/947, which "
        "an EU authority may still assess against.",
    },
    {
        "id": "easa-moc-oso3",
        "title": "EASA, Means of Compliance with OSO #3 (design requirements, SAIL III) and "
        "Light-UAS.2625",
        "document": "MOC OSO #03 and SC-LUAS2625",
        "edition": "Issue 1",
        "date": "2024-12-19",
        "pages": "1 to 4 (scope; airworthiness limitations)",
        "url": "https://www.easa.europa.eu/sites/default/files/dfu/MoC_to_OSO_3.pdf",
        "note": "Covers the designer's instructions for continuing airworthiness only, up to "
        "SAIL IV; it states that the procedural and staff-training provisions of OSO #03 are "
        "not addressed by it.",
    },
)

VERSION_NOTE = (
    "Editions 1.0 and 2.5 of Annex E ask for the same things of OSO #03. Edition 2.5 "
    "separates maintenance instructions from maintenance requirements and defines both, "
    "adds that the operator's programme is adapted to the specifics of its operations, and "
    "ties the three levels to SAIL bands. The AMC to Article 11 of Regulation (EU) 2019/947 "
    "carries edition 1.0, so an EU authority may assess against either. The EASA means of "
    "compliance adds that the designer's instructions for continuing airworthiness carry an "
    "airworthiness limitations section listing life-limited parts, batteries among them, with "
    "their inspections, removals and installations; that is the part this workbench records."
)

STATEMENT: tuple[str, ...] = (
    "This is a draft evidence pack, gathered by a software tool for the operator's own "
    "review before anything is sent to an authority.",
    "It does not show compliance with OSO #03 and claims no robustness level; the authority "
    "decides what the records show.",
    "It does not certify airworthiness or return to service, and no entry in it does: each "
    "entry records what the entering person stated.",
    "Every value comes from the flight logs, the maintenance ledger, the projected records or "
    "the due list; what the records cannot support is shown as unknown with its reason.",
)

# Section numbers, as the pack lays them out.
S_USAGE, S_PROGRAMME, S_COMPONENTS, S_LOG = 5, 6, 7, 8

# id, level, criterion, paraphrase, source id, page. The status of each item for one
# aircraft is decided in pack.py from what that aircraft's records hold.
OSO_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "id": "integrity-low",
        "level": "low",
        "criterion": "integrity",
        "text": "The operator's maintenance instructions and requirements are defined, cover "
        "the designer's where they apply, and are followed; maintenance staff are competent "
        "and hold an authorisation to maintain the UAS.",
        "source": "annex-e-2.5",
        "page": "12",
    },
    {
        "id": "integrity-medium",
        "level": "medium",
        "criterion": "integrity",
        "text": "As low, and: scheduled maintenance follows the operator's programme, built "
        "on the designer's scheduled requirements; a log system records all maintenance done, "
        "releases included, and only staff authorised for that model release to service.",
        "source": "annex-e-2.5",
        "page": "12",
    },
    {
        "id": "integrity-high",
        "level": "high",
        "criterion": "integrity",
        "text": "As medium, and: staff work to a maintenance procedure manual covering the "
        "facility, records, instructions, release, tools, material, components and defect "
        "deferral.",
        "source": "annex-e-2.5",
        "page": "12",
    },
    {
        "id": "assurance-1-low",
        "level": "low",
        "criterion": "assurance, criterion 1 (procedure)",
        "text": "The maintenance instructions are documented; the maintenance done is "
        "recorded in a maintenance log system, with why it was done, and the log may be asked "
        "for at audit; a current list of staff authorised to maintain exists.",
        "source": "annex-e-2.5",
        "page": "13",
    },
    {
        "id": "assurance-1-medium",
        "level": "medium",
        "criterion": "assurance, criterion 1 (procedure)",
        "text": "As low, and: the programme's layout follows standards or a means of "
        "compliance the competent authority accepts; a current list of staff authorised to "
        "release to service exists.",
        "source": "annex-e-2.5",
        "page": "13",
    },
    {
        "id": "assurance-1-high",
        "level": "high",
        "criterion": "assurance, criterion 1 (procedure)",
        "text": "As medium, and: the programme and the procedure manual are validated by a "
        "competent third party.",
        "source": "annex-e-2.5",
        "page": "13",
    },
    {
        "id": "assurance-2-low",
        "level": "low",
        "criterion": "assurance, criterion 2 (training)",
        "text": "A current record of the qualifications, experience and training of the "
        "maintenance staff exists.",
        "source": "annex-e-2.5",
        "page": "14",
    },
    {
        "id": "assurance-2-medium",
        "level": "medium",
        "criterion": "assurance, criterion 2 (training)",
        "text": "As low, and: an initial training syllabus and standard fit the authorisation "
        "held; release-to-service staff are trained on that UAS model; all staff have had "
        "initial training.",
        "source": "annex-e-2.5",
        "page": "14",
    },
    {
        "id": "assurance-2-high",
        "level": "high",
        "criterion": "assurance, criterion 2 (training)",
        "text": "As medium, and: a recurrent training programme for release-to-service staff "
        "exists and is validated by a competent third party.",
        "source": "annex-e-2.5",
        "page": "14",
    },
    {
        "id": "ica-airworthiness-limitations",
        "level": "design side, up to SAIL IV",
        "criterion": "airworthiness limitations in the instructions for continuing airworthiness",
        "text": "The designer's instructions carry a separate airworthiness limitations "
        "section: boundaries beyond which the UAS or a part must not be operated, typically "
        "life-limited parts such as batteries, with their inspections, removals and "
        "installations.",
        "source": "easa-moc-oso3",
        "page": "3",
    },
)
