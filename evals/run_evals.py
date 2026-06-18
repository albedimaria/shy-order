"""Offline eval of the /scrape restaurant-info extraction.

Runs the PRODUCTION extraction path (the exact functions `/scrape` uses) over a
fixed set of saved restaurant pages with known ground truth, scores each field,
and writes one `eval_runs` row + N `eval_results` rows to Supabase. Reproducible,
offline (no live web fetch), deterministic dataset — the part of the system we
fully own (ElevenLabs owns the audio/agent loop, which we don't fake here).

    .venv/Scripts/python.exe -m evals.run_evals
"""
import json
import re
import subprocess
import time
from pathlib import Path

from bs4 import BeautifulSoup

import main  # reuse production extraction + the service-role Supabase client

ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = Path(__file__).resolve().parent
SUITE = "scrape_extraction"
FIELDS = ("name", "phone_number", "address", "hours")


# --- field scoring (tolerant: real-world formatting varies) -----------------

def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _phone_ok(expected, actual) -> bool:
    if expected is None:                      # page has no phone → expect null
        return not _digits(actual)
    de, da = _digits(expected), _digits(actual)
    return bool(da) and de[-9:] == da[-9:]    # compare significant suffix (ignores +39/0 prefixes)

def _name_ok(expected, actual) -> bool:
    e, a = _norm(expected), _norm(actual)
    return bool(a) and (e in a or a in e)

def _addr_ok(expected, actual) -> bool:
    a = _norm(actual)
    if not a:
        return not expected
    toks = [t for t in re.split(r"[,\s]+", _norm(expected)) if len(t) > 2]
    hits = sum(1 for t in toks if t in a)
    return hits >= max(1, len(toks) // 2)     # majority of street/number/city tokens present

def _hours_ok(expected, actual) -> bool:
    if not expected:
        return not (actual or "").strip()
    return bool((actual or "").strip())       # presence is enough; hours text varies wildly

_FIELD_SCORERS = {
    "name": _name_ok, "phone_number": _phone_ok, "address": _addr_ok, "hours": _hours_ok,
}


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return round(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def run_scenario(sc: dict) -> dict:
    html = (EVALS_DIR / "fixtures" / sc["fixture"]).read_text(encoding="utf-8")
    expected = sc["expected"]
    soup = BeautifulSoup(html, "html.parser")
    t0 = time.perf_counter()
    try:
        actual = main._extract_restaurant_info(main._visible_text(soup), main._tel_hrefs(soup))
    except Exception as e:
        actual = {"_error": str(e)[:200]}
    latency_ms = round((time.perf_counter() - t0) * 1000)

    per_field = {f: _FIELD_SCORERS[f](expected.get(f), actual.get(f)) for f in FIELDS}
    score = round(100 * sum(per_field.values()) / len(FIELDS), 2)
    # "passed" = the booking-critical fields are right (you can't call without these)
    passed = bool(per_field["name"] and per_field["phone_number"])
    return {
        "scenario_id": sc["id"], "name": sc["name"], "passed": passed, "score": score,
        "expected": expected, "actual": actual, "latency_ms": latency_ms,
        "_per_field": per_field,
    }


def main_run() -> None:
    scenarios = json.loads((EVALS_DIR / "scenarios.json").read_text(encoding="utf-8"))
    results = [run_scenario(sc) for sc in scenarios]

    n = len(results)
    n_passed = sum(1 for r in results if r["passed"])
    success_rate = round(100 * n_passed / n, 2) if n else 0.0
    avg_score = round(sum(r["score"] for r in results) / n, 2) if n else 0.0
    lats = sorted(r["latency_ms"] for r in results)
    p50, p95 = _percentile(lats, 50), _percentile(lats, 95)

    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        git_sha = None

    print(f"\n{SUITE} — {n_passed}/{n} passed ({success_rate}%) · avg field score {avg_score}% "
          f"· p50 {p50}ms · p95 {p95}ms · model {main._OPENAI_MODEL}\n")
    for r in results:
        flag = "PASS" if r["passed"] else "FAIL"
        fields = " ".join(f"{f}={'+' if r['_per_field'][f] else '-'}" for f in FIELDS)
        print(f"  [{flag}] {r['scenario_id']:<26} score {r['score']:>5}%  {fields}  ({r['latency_ms']}ms)")

    if not main.supabase_admin:
        print("\n(no Supabase configured — results not persisted)")
        return
    try:
        run = main.supabase_admin.table("eval_runs").insert({
            "git_sha": git_sha, "suite": SUITE, "model": main._OPENAI_MODEL,
            "n_scenarios": n, "n_passed": n_passed, "success_rate": success_rate,
            "avg_score": avg_score, "p50_ms": p50, "p95_ms": p95,
            "notes": "offline; production extraction over saved fixtures",
        }).execute()
        run_id = run.data[0]["id"]
        main.supabase_admin.table("eval_results").insert([{
            "run_id": run_id, "scenario_id": r["scenario_id"], "name": r["name"],
            "passed": r["passed"], "score": r["score"],
            "expected": r["expected"], "actual": r["actual"], "latency_ms": r["latency_ms"],
        } for r in results]).execute()
        print(f"\npersisted: eval_runs#{run_id} + {n} eval_results")
    except Exception as e:
        print(f"\n(persist failed: {e})")


if __name__ == "__main__":
    main_run()
