"""Render a records summary as one self-contained HTML document: no script, no external asset.

The same layout as the OSO #03 pack: a fixed print header repeats the data label on every
printed page, and the honest statement is the first thing in the body after the title.
"""

from __future__ import annotations

from typing import Any

from .html import CSS, _e, _not_known, _table, _val

STATUS_CLASS = {
    "supported by these records": "supported",
    "partly": "partly",
    "not evidenced by this workbench": "not-evidenced",
    "not known": "muted",
    "covered": "supported",
    "not yet 3 years": "partly",
}


def _status(value: str, kind: str = "status") -> str:
    return f'<span class="{kind} {STATUS_CLASS.get(value, "muted")}">{_e(value)}</span>'


def _hours(seconds: Any) -> str:
    if isinstance(seconds, int | float):
        return f"{seconds / 3600:.1f} h ({seconds:.0f} s)"
    return _val(seconds)


def render_records_html(pack: dict[str, Any]) -> str:
    p = pack
    s = {sec["number"]: sec["title"] for sec in p["sections"]}
    integrity = p["integrity"]
    out: list[str] = []
    out.append("<!doctype html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(
        f"<title>{_e(p['title'])}: {_e(p['aircraft']['key'])}</title><style>{CSS}</style>"
        "</head><body>"
    )
    out.append(
        f'<div class="print-header">{_e(p["label"])}. {_e(p["title"])}: '
        f"{_e(p['aircraft']['key'])}, generated {_e(p['generated_utc'])}, journal head "
        f"{_e(integrity['head'][:16])}. Draft: states no record as true, decides nothing, "
        "certifies nothing.</div>"
    )
    out.append(f"<h1>{_e(p['title'])}</h1>")
    out.append(f'<p class="meta">Aircraft <strong>{_e(p["aircraft"]["key"])}</strong>.</p>')
    out.append('<div class="statement">')
    out.extend(f"<p>{_e(line)}</p>" for line in p["statement"])
    out.append("</div>")
    out.append(
        f'<p class="meta">{_e(p["label"])}. Generated {_e(p["generated_utc"])} from commit '
        f"{_e(p['commit'])}, workbench version {_e(p['version'])}; computed as of "
        f"{_e(p['as_of'])}.</p>"
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

    a = p["aircraft"]
    out.append(f"<h2>3. {_e(s[3])}</h2>")
    out.append(
        _table(
            ["Key", "Label", "Autopilot", "Data", "Licence", "Attribution", "Identified by"],
            [
                [
                    _e(a["key"]),
                    _e(a["label"]),
                    _e(a["source"]),
                    "synthetic" if a["synthetic"] else "real",
                    _e(a["licence"]),
                    _e(a["attribution"]),
                    _e(a["identified_by"]),
                ]
            ],
        )
    )

    out.append(f"<h2>4. {_e(s[4])}</h2>")
    out.append(
        "<p>Each record-keeping point of UAS.SPEC.050(1)(g), paraphrased, and whether this "
        "workbench holds records that bear on it: supported by these records, partly, not "
        "evidenced by this workbench, or not known. Nothing here decides whether an obligation "
        "is satisfied; that is the authority's.</p>"
    )
    out.append(
        _table(
            ["Point", "What it asks for (paraphrase)", "In this workbench", "Why"],
            [
                [
                    _e(item["point"]),
                    _e(item["text"]),
                    _status(item["status"]),
                    _e(item["reason"]),
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

    m = p["maintenance"]
    out.append(f"<h2>5. {_e(s[5])}</h2>")
    if (why := _not_known(m["entries"])) is not None:
        out.append(f'<p class="muted">Not known: {_e(why)}.</p>')
    elif m["entries"] == 0:
        out.append("<p>The ledger holds no entry about this aircraft by the computation date.</p>")
    else:
        out.append(
            _table(
                ["Entries", "Superseded", "First", "Last", "Entered by"],
                [
                    [
                        _val(m["entries"], 0),
                        _val(m["superseded"], 0),
                        _val(m["first_utc"]),
                        _val(m["last_utc"]),
                        "; ".join(_e(who) for who in m["entered_by"]),
                    ]
                ],
            )
        )
        out.append(
            _table(
                ["Kind of entry", "Count"],
                [[_e(k), _val(v, 0)] for k, v in m["kinds"].items()],
            )
        )
        out.append(
            '<p class="muted">The entries themselves, with what each person stated, are in the '
            "draft evidence pack for OSO #03 of this aircraft. " + _e(p["entry_note"]) + "</p>"
        )

    o = p["operations"]
    out.append(f"<h2>6. {_e(s[6])}</h2>")
    if (why := _not_known(o["flights"])) is not None:
        out.append(f'<p class="muted">Not known: {_e(why)}.</p>')
    elif o["flights"] == 0:
        out.append("<p>No flight record of this aircraft is held by the computation date.</p>")
    else:
        out.append(
            _table(
                ["Flight records", "Without a UTC start", "First", "Last", "Logged flight"],
                [
                    [
                        _val(o["flights"], 0),
                        _val(o["undated_flights"], 0),
                        _val(o["first_utc"]),
                        _val(o["last_utc"]),
                        _hours(o["logged_s"]),
                    ]
                ],
            )
        )
        out.append("<p><strong>Fault events the autopilot reported.</strong></p>")
        if o["fault_events"]:
            out.append(
                _table(
                    ["Log", "At s", "Kind", "Detail"],
                    [
                        [_e(f["log_ref"]), _val(f["t_s"]), _e(f["kind"]), _e(f["detail"])]
                        for f in o["fault_events"]
                    ],
                )
            )
        else:
            out.append("<p>The logs held report none.</p>")
        out.append("<p><strong>The logs and the autopilot's counter disagree.</strong></p>")
        if o["reconcile_findings"]:
            out.append("<ul>" + "".join(f"<li>{_e(f)}</li>" for f in o["reconcile_findings"]))
            out.append("</ul>")
        else:
            out.append("<p>No finding.</p>")
        out.append(
            "<p><strong>Logs that started while the ledger shows the aircraft grounded.</strong>"
            "</p>"
        )
        g = o["grounded"]
        if (why := _not_known(g)) is not None:
            out.append(f'<p class="muted">Not known: {_e(why)}.</p>')
        elif g["findings"]:
            out.append("<ul>" + "".join(f"<li>{_e(f)}</li>" for f in g["findings"]) + "</ul>")
        elif g["judged"]:
            out.append(
                f"<p>Of the {_e(g['judged'])} log{'' if g['judged'] == 1 else 's'} judged, not "
                "one.</p>"
            )
        else:
            out.append("<p>No log of this aircraft could be judged.</p>")
        if _not_known(g) is None and g["not_judged"]:
            out.append(
                '<p class="muted">Not judged: '
                + "; ".join(_e(n) for n in dict.fromkeys(g["not_judged"]))
                + "</p>"
            )
    out.append(f'<p class="muted">{_e(p["occurrence_note"])}</p>')

    r = p["retention"]
    out.append(f"<h2>7. {_e(s[7])}</h2>")
    out.append(f"<p>Span: {_status(r['status'], 'span')}. {_e(r['reason'])}.</p>")
    out.append(
        _table(
            ["Minimum", "Computed as of", "3 years before it", "Earliest record", "Days held"],
            [
                [
                    f"{_e(r['minimum_years'])} years",
                    _e(r["as_of"]),
                    _e(r["threshold_utc"]),
                    (
                        f"{_e(r['earliest_utc'])} ({_e(r['earliest_kind'])})"
                        if r["earliest_utc"]
                        else '<span class="muted">none dated</span>'
                    ),
                    _val(r["days"], 0),
                ]
            ],
        )
    )
    out.append(f'<p class="muted">{_e(r["note"])}</p>')

    out.append(f"<h2>8. {_e(s[8])}</h2>")
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
        "when this summary was made. Two summaries with the same head rest on the same stored "
        "records; a different head means a record was added or the file was changed.</p>"
    )
    out.append("</body></html>")
    return "\n".join(out)
