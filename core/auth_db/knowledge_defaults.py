"""Seed data for knowledge-base data classification (categories + sensitive-content rules)."""

# category -> may cloud models read sources filed under it. Admins add/rename/toggle these in the
# Knowledge Base panel; a source with no category falls back to its own cloud_ok switch.
DEFAULT_CATEGORIES = (
    ("Public", 1),
    ("Internal", 0),
    ("HR", 0),
    ("Finance", 0),
    ("Customer data", 0),
)

# (name, kind, pattern). kind "keywords" = comma separated whole words / phrases, "regex" = Python regex.
# These only ever HOLD BACK chunks for local models; they can never make a source cloud-readable.
DEFAULT_RULES = (
    ("Salary and compensation", "keywords",
     "salary, salaries, payroll, pay slip, payslip, compensation, bonus, increment, take-home pay, CTC"),
    ("National ID / passport", "regex",
     r"(?i)\b(?:nid|national\s*id|passport|tin|nationality\s*id)\b[^\n\d]{0,25}[A-Z0-9]{8,17}"),
    ("Bank account / IBAN", "regex",
     r"(?i)(?:\b(?:account|a/c|acc)\b\s*(?:no\.?|number|#)?\W{0,6}\d{8,20}|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b)"),
    ("Phone numbers", "regex", r"(?<!\d)(?:\+?880|0)1[3-9]\d{8}(?!\d)"),
    ("Email addresses", "regex", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
)
