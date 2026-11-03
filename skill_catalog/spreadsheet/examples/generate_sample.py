"""Create deterministic simulated payment data, with intentionally imperfect records."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

rng = random.Random(817)
output = Path(__file__).with_name("sample_transactions.csv")
methods = ["Card", "MFS", "Bank", "Wallet"]
merchants = ["Merchant Alpha", "Merchant Beta", "Merchant Gamma", "Merchant Delta", "Merchant Epsilon"]
regions = ["Dhaka", "Chattogram", "Rajshahi", "Khulna"]
records = []
for i in range(2400):
    d = date(2026, 1, 1) + timedelta(days=rng.randrange(270))
    method = rng.choices(methods, weights=[36, 30, 20, 14])[0]
    status = rng.choices(["SUCCESS", "FAILED", "PENDING"], weights=[88, 9, 3])[0]
    amount = round(30 + rng.gammavariate(2.3, 450), 2)
    if i in (19, 85, 930):
        amount = "" if i != 930 else "BAD-NUMBER"
    note = "=HYPERLINK(\"https://malicious.invalid\",\"click\")" if i == 7 else "Generated sample only"
    records.append([f"TX{i+1:07d}", d.isoformat(), method, amount, status, rng.choice(merchants), rng.choice(regions), note])
records += [records[33][:], records[76][:]]
with output.open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["transaction_id", "transaction_date", "payment_method", "amount", "status", "merchant", "region", "note"])
    w.writerows(records)
print(f"Created {output} ({len(records)} records)")
