"""Report shape, markdown rendering and the run-to-run diff. Pure functions: no companion, no disk."""

from typing import Optional

SEVERITIES = ("error", "warning")


def issue_key(scope: str, issue: dict) -> str:
    return f"{scope}|{issue.get('kind')}|{issue.get('selector') or ''}|{str(issue.get('message') or '')[:160]}"


def scoped_issues(report: dict) -> list:
    """[(scope, issue)] across pages and flows; a failed flow counts as an error of its own."""
    out = []
    for p in report.get("pages") or []:
        out += [(p.get("path") or p.get("url") or "page", i) for i in p.get("issues") or []]
    for f in report.get("flows") or []:
        scope = f"flow:{f.get('name')}"
        out += [(scope, i) for i in f.get("issues") or []]
        if f.get("error"):
            out.append((scope, {"kind": "flow_failed", "severity": "error", "message": f["error"], "selector": None}))
    return out


def summarize(report: dict) -> dict:
    issues = [i for _, i in scoped_issues(report)]
    return {"errors": sum(1 for i in issues if i.get("severity") == "error"),
            "warnings": sum(1 for i in issues if i.get("severity") == "warning"),
            "pages": len(report.get("pages") or []), "flows": len(report.get("flows") or []),
            "failed_flows": sum(1 for f in report.get("flows") or [] if not f.get("ok", True))}


def diff_reports(prev: Optional[dict], cur: dict) -> Optional[dict]:
    if not prev:
        return None
    before = {issue_key(s, i): (s, i) for s, i in scoped_issues(prev)}
    after = {issue_key(s, i): (s, i) for s, i in scoped_issues(cur)}
    return {"prev_run_id": prev.get("run_id"),
            "new": [after[k] for k in after if k not in before],
            "fixed": [before[k] for k in before if k not in after]}


def _line(scope: str, i: dict, with_scope: bool = True) -> str:
    where = f" ({i['selector']})" if i.get("selector") else ""
    return f"- [{i.get('kind')}] {i.get('message')}{where}" + (f"  — {scope}" if with_scope and scope else "")


def render_issues(issues: list, dropped: int = 0) -> str:
    lines = []
    for sev, title in (("error", "Errors"), ("warning", "Warnings")):
        group = [i for i in issues if i.get("severity") == sev]
        if group:
            lines += [f"## {title}"] + [_line("", i, False) for i in group] + [""]
    if dropped:
        lines.append(f"(+{dropped} more issues not shown)")
    return "\n".join(lines).strip() or "No issues found."


def render_audit(d: dict) -> str:
    s = d.get("summary") or {}
    head = [f"# Page audit — {d.get('url', '')}", f"title: {d.get('title', '')}",
            f"**{s.get('errors', 0)} errors, {s.get('warnings', 0)} warnings**"
            + (f" (device: {d['device']})" if d.get("device") and d["device"] != "desktop" else ""), ""]
    c = s.get("checked") or {}
    if c:
        head.append("checked: " + ", ".join(f"{v} {k}" for k, v in c.items()) + "\n")
    return "\n".join(head) + render_issues(d.get("issues") or [], d.get("dropped") or 0)


def render_markdown(report: dict, diff: Optional[dict] = None) -> str:
    s = report.get("summary") or summarize(report)
    out = [f"# Web check — {report.get('base_url', '')}", "",
           f"**{s['errors']} errors, {s['warnings']} warnings** across {s['pages']} page(s) and "
           f"{s['flows']} flow(s) (device: {report.get('device', 'desktop')}, run {report.get('run_id')})", ""]
    for p in report.get("pages") or []:
        ps = p.get("summary") or {}
        out.append(f"### {p.get('path') or p.get('url')} — {p.get('title', '')}".rstrip(" —"))
        if p.get("error"):
            out.append(f"- could not load: {p['error']}")
        out.append(render_issues(p.get("issues") or [], p.get("dropped") or 0) if p.get("issues") else
                   ("" if p.get("error") else f"clean ({ps.get('errors', 0)} errors, {ps.get('warnings', 0)} warnings)"))
        out.append("")
    if report.get("flows"):
        out.append("## Flows")
        for f in report["flows"]:
            out.append(f"- {'✓' if f.get('ok') else '✗'} {f.get('name')}" + (f": {f['error']}" if f.get("error") else ""))
            out += [f"    - ✗ {i.get('message')}" for i in f.get("issues") or [] if i.get("kind") == "expect_failed"]
            out += [f"    - [{i.get('kind')}] {i.get('message')}" for i in f.get("issues") or [] if i.get("kind") != "expect_failed"]
        out.append("")
    if diff is not None:
        out.append(f"## Since last run ({diff.get('prev_run_id')})")
        out += [f"+ new: [{i.get('kind')}] {i.get('message')}  — {s_}" for s_, i in diff["new"]]
        out += [f"- fixed: [{i.get('kind')}] {i.get('message')}  — {s_}" for s_, i in diff["fixed"]]
        if not diff["new"] and not diff["fixed"]:
            out.append("no change")
        out.append("")
    return "\n".join(out).strip()
