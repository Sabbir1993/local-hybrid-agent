"""Evaluate the Laya routing classifier on labelled examples and suggest thresholds.

    python scripts/eval_classifier.py                 # print accuracy, confusion, thresholds
    python scripts/eval_classifier.py --json out.json # also write the report
    python scripts/eval_classifier.py --min-precision 0.95

Reads tests/data/classifier_eval.jsonl ({"q": question, "text": ..., "label": ...} per line).
For each question it reports overall accuracy, a confusion table, and the lowest confidence
threshold at which the confident predictions reach --min-precision (default 0.95) together
with the share of examples still answered at that threshold. Put the numbers in
config/app.json -> router.classifier.thresholds. The checkpoint's own confidences are
uncalibrated, so this - not the defaults - is what makes a threshold trustworthy.

Exit status is 1 when a question cannot reach the precision bar at any threshold, so it can gate
enabling `router.classifier.mode = "active"`.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "tests", "data", "classifier_eval.jsonl")


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    return rows


def suggest_threshold(preds, min_precision):
    """Lowest threshold whose confident predictions are >= min_precision correct.
    preds: [(confidence, correct)]. -> (threshold, coverage, precision) or None."""
    best = None
    for thr in [x / 100 for x in range(50, 100)]:
        kept = [(c, ok) for c, ok in preds if c >= thr]
        if not kept:
            continue
        prec = sum(1 for _c, ok in kept if ok) / len(kept)
        if prec >= min_precision:
            best = (thr, len(kept) / len(preds), prec)
            break
    return best


def evaluate(rows, classify, min_precision=0.95):
    """Per question: accuracy, confusion, and a threshold picked on the even-indexed examples
    and then checked on the odd-indexed ones it never saw (a threshold tuned and judged on the
    same examples would always look good)."""
    from core.small_model import classifier as clf
    report = {}
    for question in clf.QUESTIONS:
        items = [r for r in rows if r["q"] == question]
        if not items:
            continue
        preds, confusion, lat = [], defaultdict(Counter), []
        failures = 0
        for i, r in enumerate(items):
            t0 = time.time()
            res = classify(question, r["text"])
            lat.append(time.time() - t0)
            if res is None:
                failures += 1
                continue
            ok = res["label"] == r["label"]
            preds.append((i, res["confidence"], ok))
            confusion[r["label"]][res["label"]] += 1
        acc = (sum(1 for _i, _c, ok in preds if ok) / len(preds)) if preds else 0.0
        tune = [(c, ok) for i, c, ok in preds if i % 2 == 0]
        hold = [(c, ok) for i, c, ok in preds if i % 2 == 1]
        sug = suggest_threshold(tune, min_precision)
        hold_prec = hold_cov = None
        if sug is not None:
            kept = [(c, ok) for c, ok in hold if c >= sug[0]]
            if kept:
                hold_prec = sum(1 for _c, ok in kept if ok) / len(kept)
                hold_cov = len(kept) / len(hold)
        report[question] = {
            "examples": len(items), "answered": len(preds), "failed": failures,
            "accuracy": round(acc, 3),
            "confusion": {k: dict(v) for k, v in confusion.items()},
            "suggested_threshold": None if sug is None else round(sug[0], 2),
            "tune_precision": None if sug is None else round(sug[2], 3),
            "holdout_precision": None if hold_prec is None else round(hold_prec, 3),
            "holdout_coverage": None if hold_cov is None else round(hold_cov, 3),
            "avg_latency_s": round(sum(lat) / len(lat), 3) if lat else None,
        }
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-precision", type=float, default=0.95)
    args = ap.parse_args()

    from core.small_model import APP_CONFIG, classifier as clf
    # evaluate with the classifier switched on, whatever the live config says
    APP_CONFIG.setdefault("router", {}).setdefault("classifier", {}).update(
        {"enabled": True, "timeout_s": 60})
    clf.warm()
    rows = load(args.data)
    report = evaluate(rows, lambda q, t: clf.classify(q, t, timeout_s=60), args.min_precision)

    ok = True
    for q, r in report.items():
        print(f"\n== {q}: {r['examples']} examples, accuracy {r['accuracy']:.1%}, "
              f"avg {r['avg_latency_s']}s/call, {r['failed']} failed")
        for true, row in sorted(r["confusion"].items()):
            print(f"   true {true:<10} -> " + ", ".join(f"{k}:{v}" for k, v in sorted(row.items())))
        if r["suggested_threshold"] is None:
            ok = False
            print(f"   NO threshold reaches {args.min_precision:.0%} precision: do not use this question actively")
        elif r["holdout_precision"] is None or r["holdout_precision"] < args.min_precision - 0.05:
            ok = False
            print(f"   threshold {r['suggested_threshold']} does NOT hold on unseen examples "
                  f"(holdout precision {r['holdout_precision']}): do not use this question actively")
        else:
            print(f"   threshold {r['suggested_threshold']}: precision {r['tune_precision']:.1%} when tuned, "
                  f"{r['holdout_precision']:.1%} on unseen examples ({r['holdout_coverage']:.0%} of them answered)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
