"""Render the fleet records summary as one self-contained HTML document: no script, no
external asset. The same layout as the per-aircraft summary: a fixed print header on every
printed page, the honest statement first in the body after the title."""

from __future__ import annotations

from typing import Any

from .html import CSS, _e, _table, _val
from .records_html import _status


def render_fleet_records_html(summary: dict[str, Any]) -> str:
    p = summary
    s = {sec["number"]: sec["title"] for sec in p["sections"]}
    integrity = p["integrity"]
    counts = p["counts"]
    out: list[str] = []
    out.append("<!doctype html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(f"<title>{_e(p['title'])}: the fleet</title><style>{CSS}</style></head><body>")
    out.append(
        f'<div class="print-header">{_e(p["title"])}: the fleet, {_e(counts["aircraft"])} '
        f"aircraft, generated {_e(p['generated_utc'])}, journal head "
        f"{_e(integrity['head'][:16])}. Draft: states no record as true, decides nothing, "
        "certifies nothing.</div>"
    )
    out.append(f"<h1>{_e(p['title'])}</h1>")
    out.append(
        f'<p class="meta">The fleet: <strong>{_e(counts["aircraft"])}</strong> aircraft, '
        f"{_e(counts['synthetic'])} synthetic and {_e(counts['real'])} real.</p>"
    )
    out.append('<div class="statement">')
    out.extend(f"<p>{_e(line)}</p>" for line in p["statement"])
    out.append("</div>")
    out.append(
        f'<p class="meta">Generated {_e(p["generated_utc"])} from commit {_e(p["commit"])}, '
        f"workbench version {_e(p['version'])}; computed as of {_e(p['as_of'])}.</p>"
    )

    out.append(f"<h2>2. {_e(s[2])}</h2>")
    cls = "supported" if integrity["ok"] else "not-evidenced"
    out.append(f'<p class="{cls}">{_e(integrity["line"])}</p>')
    out.append(
        _table(
            ["Journal head", "Links", "Chain"],
            [
                [
                    f"<code>{_e(integrity['head'])}</code>",
                    _val(integrity["length"], 0),
                    "intact" if integrity["ok"] else "broken",
                ]
            ],
        )
    )
    if integrity["migration"]:
        m = integrity["migration"]
        out.append(
            f'<p class="muted">Link {_e(m["seq"])}, recorded {_e(m["recorded_utc"])}: '
            f"{_e(m['note'])}.</p>"
        )
    out.append(f'<p class="muted">{_e(integrity["note"])}</p>')

    r = p["readable"]
    out.append(f"<h2>3. {_e(s[3])}</h2>")
    cls = "supported" if r["ok"] else "muted"
    out.append(f'<p class="{cls}">{_e(r["line"])}</p>')
    out.append(f'<p class="muted">{_e(p["not_known_rule"])}</p>')

    out.append(f"<h2>4. {_e(s[4])}</h2>")
    out.append(
        "<p>Each record-keeping point of UAS.SPEC.050(1)(g), paraphrased, and how many "
        "aircraft hold records that bear on it, by status: supported by these records, partly, "
        "not evidenced by this workbench, or not known. Nothing here decides whether an "
        "obligation is satisfied; that is the authority's.</p>"
    )
    statuses = list(next(iter(counts["by_point"].values())))
    out.append(
        _table(
            ["Point", "What it asks for (paraphrase)", *statuses],
            [
                [
                    _e(item["point"]),
                    _e(item["text"]),
                    *(_val(counts["by_point"][item["id"]][st], 0) for st in statuses),
                ]
                for item in p["items"]
            ],
        )
    )
    for src in p["sources"]:
        out.append(
            f'<p class="muted">Source: {_e(src["title"])}, <a href="{_e(src["url"])}">'
            f"{_e(src['url'])}</a>. {_e(src['note'])}</p>"
        )

    out.append(f"<h2>5. {_e(s[5])}</h2>")
    out.append(f"<p>{_e(p['rows_note'])}</p>")
    headers = [
        "Aircraft",
        "Data",
        "Training records",
        "Maintenance activities",
        "Operations and occurrences",
        "Span",
        "Entries",
        "Flight records",
        "Earliest record",
    ]
    out.append(
        _table(
            headers,
            [
                [
                    f"<strong>{_e(row['key'])}</strong> {_e(row['label'])}",
                    "synthetic" if row["synthetic"] else "real",
                    _status(row["statuses"]["training-records"]),
                    _status(row["statuses"]["maintenance-activities"]),
                    _status(row["statuses"]["operations-occurrences"]),
                    _status(row["span"], "span"),
                    _val(row["entries"], 0),
                    _val(row["flights"], 0),
                    _val(row["earliest_utc"]),
                ]
                for row in p["rows"]
            ],
            ["muted" if row["not_known"] else "" for row in p["rows"]],
        )
    )
    out.append(
        f'<p class="muted">Rows with something not known: {_e(counts["rows_not_known"])} of '
        f"{_e(counts['aircraft'])}.</p>"
    )

    out.append(f"<h2>6. {_e(s[6])}</h2>")
    out.append(
        f"<p>Against the {_e(p['retention']['minimum_years'])}-year minimum, computed as of "
        f"{_e(p['retention']['as_of'])}: "
        + ", ".join(
            f"{_status(st, 'span')} on {_e(n)} aircraft" for st, n in counts["span"].items()
        )
        + ".</p>"
    )
    out.append(
        '<p class="muted">The span of each aircraft is from its earliest dated record to the '
        "computation date; it says how far back the records held reach, never whether a "
        "record was ever removed. The rule and each reason are in that aircraft's own "
        "summary.</p>"
    )

    out.append(f"<h2>7. {_e(s[7])}</h2>")
    out.append(
        _table(
            ["Generated (UTC)", "Computed as of", "Commit", "Version", "Journal head", "Links"],
            [
                [
                    _e(p["generated_utc"]),
                    _e(p["as_of"]),
                    _e(p["commit"]),
                    _e(p["version"]),
                    f"<code>{_e(integrity['head'])}</code>",
                    _val(integrity["length"], 0),
                ]
            ],
        )
    )
    out.append(
        "<p>The journal head names the state of every entry and flight record in the store "
        "when this summary was made; every row and the head were read from that one state. "
        "Two summaries with the same head rest on the same stored records; a different head "
        "means a record was added or the file was changed.</p>"
    )
    out.append("</body></html>")
    return "\n".join(out)
