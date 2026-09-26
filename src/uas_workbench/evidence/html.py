"""Render a pack as one self-contained HTML document: no script, no external asset.

A fixed print header repeats the data label on every printed page; the honest statement
is the first thing in the body after it. Prints to PDF from any browser.
"""

from __future__ import annotations

from html import escape
from typing import Any

CSS = """
  :root { --fg: #1d1d1b; --muted: #5b5b56; --line: #d9d7d0; --bad: #8c1d18; --ok: #235e33;
          --amber: #7a5200; --box: #f6f6f3; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 64px 24px 32px; color: var(--fg);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         max-width: 1000px; }
  .print-header { position: fixed; top: 0; left: 0; right: 0; padding: 6px 24px;
                  background: var(--box); border-bottom: 1px solid var(--line); font-size: 12px;
                  color: var(--muted); }
  h1 { font-size: 1.5rem; margin: 0 0 8px; }
  h2 { font-size: 1.1rem; margin: 28px 0 8px; border-bottom: 1px solid var(--line);
       padding-bottom: 4px; }
  .statement { border: 2px solid var(--bad); padding: 10px 14px; margin: 12px 0; }
  .statement p { margin: 4px 0; }
  .meta { color: var(--muted); font-size: 13px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
  th, td { text-align: left; vertical-align: top; padding: 5px 6px;
           border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .supported { color: var(--ok); font-weight: 600; }
  .partly { color: var(--amber); font-weight: 600; }
  .not-evidenced { color: var(--bad); font-weight: 600; }
  .muted { color: var(--muted); }
  .history { color: var(--muted); }
  .history td { text-decoration: none; }
  code { font-size: 12px; }
  @media print { body { padding-top: 56px; } h2 { break-after: avoid; }
                 tr { break-inside: avoid; } }
"""


def _e(value: Any) -> str:
    return escape(str(value))


def _val(value: Any, digits: int = 1) -> str:
    if isinstance(value, dict) and set(value) == {"unknown"}:
        return f'<span class="muted">unknown: {_e(value["unknown"])}</span>'
    if value is None:
        return '<span class="muted">none</span>'
    if isinstance(value, float):
        return _e(f"{value:.{digits}f}")
    return _e(value)


def _hours(seconds: Any) -> str:
    if isinstance(seconds, int | float):
        return f"{seconds / 3600:.1f} h ({seconds:.0f} s)"
    return _val(seconds)


def _table(headers: list[str], rows: list[list[str]], classes: list[str] | None = None) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = ""
    for n, row in enumerate(rows):
        cls = f' class="{classes[n]}"' if classes and classes[n] else ""
        body += f"<tr{cls}>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(pack: dict[str, Any]) -> str:
    p = pack
    s = {sec["number"]: sec["title"] for sec in p["sections"]}
    out: list[str] = []
    out.append("<!doctype html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(f"<title>{_e(p['title'])}</title><style>{CSS}</style></head><body>")
    out.append(
        f'<div class="print-header">{_e(p["label"])}. {_e(p["title"])}, generated '
        f"{_e(p['generated_utc'])}, ledger hash {_e(p['ledger_hash'][:16])}. Draft: shows no "
        "compliance, claims no robustness level, certifies nothing.</div>"
    )
    out.append(f"<h1>{_e(p['title'])}</h1>")
    out.append('<div class="statement">')
    out.extend(f"<p>{_e(line)}</p>" for line in p["statement"])
    out.append("</div>")
    out.append(
        f'<p class="meta">{_e(p["label"])}. Generated {_e(p["generated_utc"])} from commit '
        f"{_e(p['commit'])}, workbench version {_e(p['version'])}; computed as of "
        f"{_e(p['as_of'])}; ledger hash {_e(p['ledger_hash'])}.</p>"
    )

    out.append(f"<h2>2. {_e(s[2])}</h2>")
    out.append(
        "<p>Paraphrased, not quoted: the JARUS texts may be used but not copied without "
        "permission. Each item in section 4 names its document, edition and page.</p>"
    )
    out.append(
        _table(
            ["Document", "Edition, date", "Pages", "Note", "URL"],
            [
                [
                    _e(src["title"]) + f"<br><code>{_e(src['document'])}</code>",
                    f"{_e(src['edition'])}, {_e(src['date'])}",
                    _e(src["pages"]),
                    _e(src["note"]),
                    f'<a href="{_e(src["url"])}">{_e(src["url"])}</a>',
                ]
                for src in p["sources"]
            ],
        )
    )
    out.append(f"<p>{_e(p['version_note'])}</p>")

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
        "<p>Each OSO #03 item, and whether this pack holds records that bear on it: "
        "supported, partly, or not evidenced by this workbench. Nothing here is a finding of "
        "compliance; that is the authority's.</p>"
    )
    out.append(
        _table(
            ["Item", "Level", "What it asks for (paraphrase)", "Source", "In this pack", "Why"],
            [
                [
                    _e(item["id"]),
                    f"{_e(item['criterion'])}, {_e(item['level'])}",
                    _e(item["text"]),
                    f"{_e(item['source'])}, p. {_e(item['page'])}",
                    f'<span class="{item["status"].replace(" ", "-")}">{_e(item["status"])}</span>'
                    + (
                        " (section"
                        + ("s " if len(item["sections"]) > 1 else " ")
                        + ", ".join(str(n) for n in item["sections"])
                        + ")"
                        if item["sections"]
                        else ""
                    ),
                    _e(item["reason"]),
                ]
                for item in p["items"]
            ],
        )
    )

    u = p["usage"]
    out.append(f"<h2>5. {_e(s[5])}</h2>")
    out.append(
        _table(
            ["Time in service", "Before the first log", "Logged flight", "Flight no log covers"],
            [
                [
                    _hours(u["time_in_service_s"]),
                    _hours(u["before_s"]),
                    _hours(u["logged_s"]),
                    _hours(u["unlogged_s"]),
                ]
            ],
        )
    )
    out.append(
        _table(
            ["Log", "UTC start", "Flight s", "Arm cycles", "Landings", "Faults reported", "Data"],
            [
                [
                    _e(f["log_ref"]),
                    _val(f["utc_start"]),
                    _val(f["flight_time_s"]),
                    _val(f["arm_cycles"], 0),
                    _val(f["landings"], 0),
                    (
                        _val(f["fault_events"])
                        if isinstance(f["fault_events"], dict)
                        else (
                            "; ".join(_e(e["kind"] + ": " + e["detail"]) for e in f["fault_events"])
                            or "none reported"
                        )
                    ),
                    "synthetic" if f["synthetic"] else "real",
                ]
                for f in u["flights"]
            ],
        )
    )
    if u["findings"]:
        out.append("<p>Findings from reconciling the logs against the autopilot's counter:</p><ul>")
        out.extend(f"<li>{_e(f['message'])}</li>" for f in u["findings"])
        out.append("</ul>")
    if u["unchecked"]:
        out.append(
            '<p class="muted">Not checked: '
            + "; ".join(f"{_e(k)} ({_e(v)})" for k, v in u["unchecked"].items())
            + "</p>"
        )

    pr = p["programme"]
    out.append(f"<h2>6. {_e(s[6])}</h2>")
    out.append(f"<p>Board state: <strong>{_val(pr['status'])}</strong></p>")
    if pr["status_reasons"]:
        out.append("<ul>" + "".join(f"<li>{_e(r)}</li>" for r in pr["status_reasons"]) + "</ul>")
    if pr["items"]:
        out.append(
            _table(
                ["Item", "Basis", "Used", "Limit", "Remaining", "State", "Source"],
                [
                    [
                        _e(i["subject"]),
                        _e(i["basis"]),
                        f"{_val(i['used'])} {_e(i['unit'])}",
                        _val(i["limit"], 0)
                        + " "
                        + ("months" if i["basis"] == "calendar" else _e(i["unit"])),
                        f"{_val(i['remaining'])} {_e(i['unit'])}",
                        _e(i["state"].replace("_", " ")),
                        _e(i["source"]),
                    ]
                    for i in pr["items"]
                ],
            )
        )
    else:
        out.append(f"<p>{_e(p['ledger_note'])}.</p>")
    if pr["notes"]:
        out.append('<p class="muted">Notes: ' + "; ".join(_e(n) for n in pr["notes"]) + "</p>")
    out.append(f'<p class="muted">{_e(pr["source_note"])}</p>')

    out.append(f"<h2>7. {_e(s[7])}</h2>")
    if p["components"]:
        for c in p["components"]:
            out.append(
                f"<p><strong>{_e(c['kind'])} {_e(c['id'])}</strong>, in service since "
                f"{_e(c['in_service_since'])}; usage {_hours(c['usage']['hours_s'])}, "
                f"{_e(c['usage']['cycles'])} cycles; "
                f"{'synthetic' if c['synthetic'] else 'real'}.</p>"
            )
            out.append(
                _table(
                    ["Installed on", "From (UTC)", "To (UTC)"],
                    [
                        [_e(i["aircraft_key"]), _e(i["from_utc"]), _val(i["to_utc"])]
                        for i in c["installations"]
                    ],
                )
            )
    else:
        out.append(f"<p>{_e(p['ledger_note'])}.</p>")

    out.append(f"<h2>8. {_e(s[8])}</h2>")
    out.append(f"<p>{_e(p['ledger_note'])}.</p>")
    if p["log"]:
        out.append(f'<p class="muted">{_e(p["entry_note"])}</p>')
        rows: list[list[str]] = []
        classes: list[str] = []
        for e in p["log"]:
            status = "current"
            if not e["current"]:
                status = f"superseded by #{_e(e['superseded_by'])}: {_e(e['superseded_reason'])}"
            elif e["supersedes"] is not None:
                status = f"corrects #{_e(e['supersedes'])}: {_e(e['reason'])}"
            details = ", ".join(f"{_e(k)}={_e(v)}" for k, v in e["details"].items())
            rows.append(
                [
                    f"#{_e(e['id'])}",
                    _e(e["occurred_utc"]),
                    _e(e["recorded_utc"]),
                    _e(e["kind"]),
                    _e(e["subject"]),
                    _e(e["entered_by"]),
                    _e(e["statement"]) + (f"<br><code>{details}</code>" if details else ""),
                    status,
                    "synthetic" if e["synthetic"] else "real",
                ]
            )
            classes.append("" if e["current"] else "history")
        out.append(
            _table(
                [
                    "Entry",
                    "Occurred",
                    "Recorded",
                    "Kind",
                    "Subject",
                    "Entered by",
                    "Statement",
                    "Status",
                    "Data",
                ],
                rows,
                classes,
            )
        )

    out.append(f"<h2>9. {_e(s[9])}</h2>")
    out.append("<ul>")
    out.extend(
        f"<li><strong>{_e(i['id'])}</strong> ({_e(i['criterion'])}, {_e(i['level'])}): "
        f"{_e(i['reason'])}.</li>"
        for i in p["not_evidenced"]
    )
    out.append("</ul>")

    out.append(f"<h2>10. {_e(s[10])}</h2>")
    c = p["counts"]
    out.append(
        _table(
            [
                "Generated (UTC)",
                "Computed as of",
                "Commit",
                "Version",
                "Ledger hash (SHA-256)",
                "Entries",
                "Flights",
                "Due items",
            ],
            [
                [
                    _e(p["generated_utc"]),
                    _e(p["as_of"]),
                    _e(p["commit"]),
                    _e(p["version"]),
                    f"<code>{_e(p['ledger_hash'])}</code>",
                    _e(c["entries"]),
                    _e(c["flights"]),
                    _e(c["due_items"]),
                ]
            ],
        )
    )
    out.append(
        "<p>The hash covers every entry read for this pack, superseded ones included. Two "
        "packs for the same aircraft with the same hash rest on the same records; a different "
        "hash means the ledger changed between them.</p>"
    )
    out.append("</body></html>")
    return "\n".join(out)
