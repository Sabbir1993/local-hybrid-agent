"""The fixed prompt sets for the live file-generation benchmark (scripts/bench_chat_files.py) and, for each prompt, a
representative markdown outline of what a good answer looks like (used by the offline builder benchmarks).

Prompts are realistic requests a gateway employee would type. No card numbers, tokens or merchant credentials appear anywhere:
sensitive-looking values are [PLACEHOLDER]s. `min_words` / `min_pages` are floors for "the model did real work".
"""

BN = "ঢাকা বাংলাদেশের রাজধানী এবং বৃহত্তম শহর"


def _rows(n, cols):
    return "".join("| " + " | ".join(f"{c} {i}" for c in cols) + " |\n" for i in range(1, n + 1))


DOCX_PROMPTS = {
    "incident_report": {
        "prompt": "Create a Word document (.docx): an incident report for a 40-minute payment authorisation outage on "
                  "[MERCHANT-PLACEHOLDER]'s checkout. Include summary, timeline table, root cause, impact, corrective actions "
                  "and lessons learned.",
        "min_words": 250,
        "outline": ("# Incident Report: Authorisation Outage\n\n## Summary\nA 40-minute outage affected card authorisation.\n\n"
                    "## Timeline\n| Time | Event |\n|---|---|\n" + _rows(6, ["10:0", "Alert raised and acknowledged"]) +
                    "\n## Root cause\nA **certificate** on the acquirer link expired.\n\n## Impact\n- Failed attempts rose\n- Retries "
                    "spiked\n\n## Corrective actions\n1. Automate renewal\n2. Alert 30 days ahead\n\n## Lessons learned\n- Monitor expiry\n"),
    },
    "policy_memo": {
        "prompt": "Write a Word document called Data Retention Policy for our payment gateway: purpose, scope, retention periods "
                  "by data type in a table, secure disposal, roles and responsibilities, and review cycle. Mention Bangladesh "
                  "Bank and PCI DSS expectations.",
        "min_words": 300,
        "outline": ("# Data Retention Policy\n\n## Purpose\nTo define how long records are kept.\n\n## Scope\nAll systems that "
                    "store transaction data.\n\n## Retention periods\n| Data type | Period | Basis |\n|---|---|---|\n"
                    + _rows(6, ["Transaction log", "5 years", "Bangladesh Bank"]) + "\n## Secure disposal\n- Cryptographic erase\n"
                    "- Certified shredding\n\n## Roles and responsibilities\n- Data owner\n- Security officer\n\n## Review\nAnnual.\n"),
    },
    "proposal": {
        "prompt": "Make a .docx proposal to onboard a mid-size e-commerce merchant to our gateway: executive summary, solution "
                  "overview, integration steps, pricing table with three tiers, timeline, and terms. Use [MERCHANT-PLACEHOLDER] "
                  "for the name.",
        "min_words": 350,
        "outline": ("# Merchant Onboarding Proposal\n\n## Executive summary\nOne paragraph.\n\n## Solution overview\n- Hosted checkout\n"
                    "- Tokenisation\n\n## Integration steps\n1. Sandbox keys\n2. Test cases\n3. Go live\n\n## Pricing\n"
                    "| Tier | Monthly | Rate |\n|---|---|---|\n| Starter | 5,000 | 2.1% |\n| Growth | 12,000 | 1.8% |\n| Scale | 30,000 | 1.5% |\n\n"
                    "## Timeline\n| Week | Milestone |\n|---|---|\n" + _rows(4, ["Week", "Milestone"]) + "\n## Terms\n- 12 months\n"),
    },
    "meeting_minutes": {
        "prompt": "Create a Word doc with meeting minutes for the weekly risk review: attendees, agenda, decisions, an action "
                  "items table with owner and due date, and next meeting. Keep it to two pages.",
        "min_words": 150,
        "outline": ("# Weekly Risk Review: Minutes\n\n## Attendees\n- Risk lead\n- Engineering\n- Compliance\n\n## Agenda\n1. Fraud rates\n"
                    "2. Chargebacks\n3. Vendor audit\n\n## Decisions\n- Raise velocity limits\n\n## Action items\n"
                    "| Action | Owner | Due |\n|---|---|---|\n" + _rows(4, ["Task", "Owner", "2026-11-0"]) + "\n## Next meeting\nNext Tuesday.\n"),
    },
    "bengali_notice": {
        "prompt": f"একটি Word ডকুমেন্ট (.docx) তৈরি করো: গ্রাহকদের জন্য সার্ভিস রক্ষণাবেক্ষণের নোটিশ - তারিখ, সময়, প্রভাব এবং "
                  f"যোগাযোগের তথ্যসহ।",
        "min_words": 60,
        "outline": (f"# রক্ষণাবেক্ষণ নোটিশ\n\n## বিবরণ\n{BN}।\n\n## সময়সূচি\n| তারিখ | সময় |\n|---|---|\n| ২০ অক্টোবর | রাত ২টা |\n\n"
                    f"## প্রভাব\n- পেমেন্ট সাময়িক বন্ধ\n- {BN}\n\n## যোগাযোগ\n- সাপোর্ট হটলাইন\n"),
    },
}

PDF_PROMPTS = {
    "security_guide": {
        "prompt": "Create a PDF guide on securing a payment gateway integration for merchant developers: key management, "
                  "tokenisation, webhook verification, logging without card data, and an incident checklist.",
        "answers": {"How long should the PDF be?": "Standard (6-8 sections)", "Who is it for?": "Technical team",
                    "Style?": "Step-by-step handbook"},
        "min_pages": 4,
        "outline": ("# Securing Your Gateway Integration\n\nIntro line.\n\n## Summary\n- Keep keys out of code\n- Tokenise\n- Verify webhooks\n\n"
                    + "".join(f"## Section {i}\nParagraph one explains the point in some depth.\n\nParagraph two adds an example.\n\n"
                              "| Item | Why |\n|---|---|\n" + _rows(3, ["Control", "Reason"]) + "\n" for i in range(1, 7))
                    + "## Next steps\n- Review keys\n"),
    },
    "annual_report": {
        "prompt": "Make a PDF annual operations report for 2026: highlights, volume and success-rate table by quarter, top "
                  "incidents, customer support metrics, and 2027 priorities.",
        "answers": {"How long should the PDF be?": "Standard (6-8 sections)", "Who is it for?": "Executives",
                    "Style?": "Formal report"},
        "min_pages": 4,
        "outline": ("# Annual Operations Report 2026\n\nIntro.\n\n## Summary\n- Volume up\n- Success rate steady\n\n## Highlights\n"
                    "Text.\n\n## Volume and success rate\n| Quarter | Transactions | Success |\n|---|---|---|\n| Q1 | 10M | 97.1% |\n"
                    "| Q2 | 12M | 97.4% |\n| Q3 | 13M | 97.6% |\n| Q4 | 15M | 97.9% |\n\n## Top incidents\n- One\n- Two\n\n"
                    "## Support metrics\nText.\n\n## 2027 priorities\n1. A\n2. B\n\n## Next steps\n- Plan\n"),
    },
    "onboarding_handbook": {
        "prompt": "Create a PDF handbook for new support agents at a payment company: how a card payment flows, common decline "
                  "reasons with what to tell the customer, escalation paths, and what never to ask a customer for.",
        "answers": {"How long should the PDF be?": "Detailed (9-12 sections)", "Who is it for?": "General / non-technical",
                    "Style?": "Friendly guide"},
        "min_pages": 5,
        "outline": ("# Support Agent Handbook\n\nWelcome.\n\n## Summary\n- Flow\n- Declines\n- Escalation\n\n## How a payment flows\nText.\n\n"
                    "## Common declines\n| Code | Meaning | Say |\n|---|---|---|\n" + _rows(8, ["Code", "Meaning", "Say"])
                    + "\n## Escalation paths\n1. Tier 2\n2. Risk\n\n## What never to ask for\n- Full card number\n- PIN\n- CVV\n\n## Next steps\n- Shadow a call\n"),
    },
    "bengali_report": {
        "prompt": "বাংলায় একটি PDF রিপোর্ট তৈরি করো: ডিজিটাল পেমেন্ট গ্রহণের সুবিধা, চ্যালেঞ্জ এবং সুপারিশ।",
        "answers": {"How long should the PDF be?": "Short (4-5 sections)", "Who is it for?": "Executives", "Style?": "Formal report"},
        "min_pages": 2,
        "outline": (f"# ডিজিটাল পেমেন্ট প্রতিবেদন\n\nভূমিকা: {BN}।\n\n## সারসংক্ষেপ\n- সুবিধা\n- চ্যালেঞ্জ\n\n## সুবিধা\n{BN}।\n\n"
                    f"## চ্যালেঞ্জ\n| বিষয় | বিবরণ |\n|---|---|\n| নিরাপত্তা | {BN} |\n\n## সুপারিশ\n1. প্রথম\n2. দ্বিতীয়\n\n## পরবর্তী পদক্ষেপ\n- পরিকল্পনা\n"),
    },
    "compliance_checklist": {
        "prompt": "Create a PDF compliance checklist mapping PCI DSS requirement areas to controls, evidence to collect, and "
                  "owners, for an annual self-assessment.",
        "answers": {"How long should the PDF be?": "Short (4-5 sections)", "Who is it for?": "Technical team",
                    "Style?": "Formal report"},
        "min_pages": 3,
        "outline": ("# PCI DSS Self-Assessment Checklist\n\nIntro.\n\n## Summary\n- Scope\n- Evidence\n\n## Requirement areas\n"
                    "| Area | Control | Evidence | Owner |\n|---|---|---|---|\n" + _rows(12, ["Area", "Control", "Evidence", "Owner"])
                    + "\n## Evidence handling\nText.\n\n## Review cadence\nText.\n\n## Next steps\n- Assign owners\n"),
    },
}
