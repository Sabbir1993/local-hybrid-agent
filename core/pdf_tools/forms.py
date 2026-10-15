"""AcroForm fields: list them, fill them, optionally flatten. Never invents a value: unknown field names are an error that lists
the fields the form really has."""
import io
from typing import Optional

from .common import PdfError, open_reader

_TYPES = {"/Tx": "text", "/Btn": "checkbox / button", "/Ch": "choice", "/Sig": "signature"}


def fields(data: bytes, password: Optional[str] = None) -> list:
    reader = open_reader(data, password)
    out = []
    for name, f in (reader.get_fields() or {}).items():
        ftype = _TYPES.get(str(f.get("/FT")), str(f.get("/FT") or "?"))
        item = {"name": str(name), "type": ftype, "value": "" if f.get("/V") is None else str(f.get("/V"))}
        opts = f.get("/Opt")
        if opts:
            item["options"] = [str(o if not isinstance(o, list) else o[-1]) for o in opts][:30]
        states = f.get("/_States_")
        if states and ftype.startswith("checkbox"):
            item["states"] = [str(s) for s in states]
        out.append(item)
    return out


def describe(data: bytes, password: Optional[str] = None) -> str:
    items = fields(data, password)
    if not items:
        return "This PDF has no fillable form fields."
    lines = [f"{len(items)} form field(s):"]
    for it in items[:200]:
        extra = (f" options: {', '.join(it['options'])}" if it.get("options") else "") + \
                (f" states: {', '.join(it['states'])}" if it.get("states") else "")
        lines.append(f"- {it['name']} ({it['type']}){' = ' + it['value'] if it['value'] else ''}{extra}")
    return "\n".join(lines)


def fill(data: bytes, values: dict, flatten: bool = False, password: Optional[str] = None) -> tuple:
    """-> (pdf bytes, [what was filled])."""
    from pypdf import PdfWriter
    if not isinstance(values, dict) or not values:
        raise PdfError("values required: {field name: value}; call with describe=true to see the field names")
    known = {f["name"]: f for f in fields(data, password)}
    if not known:
        raise PdfError("this PDF has no fillable form fields (it is not an AcroForm)")
    unknown = [k for k in values if k not in known]
    if unknown:
        raise PdfError(f"no form field named {', '.join(map(repr, unknown[:5]))}. The fields are: {', '.join(list(known)[:30])}")
    reader = open_reader(data, password)
    w = PdfWriter(clone_from=reader)
    for page in w.pages:
        w.update_page_form_field_values(page, {k: str(v) for k, v in values.items()}, auto_regenerate=False, flatten=flatten)
    w.set_need_appearances_writer(True)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), [f"{k} = {str(v)[:40]}" for k, v in values.items()]
