"""Run: python test_field_engine.py   (or: pytest test_field_engine.py)

Checks the engine against test_vectors.json. The EM83 cases were produced by
PropMon's own code, so passing them proves this copy rates exactly like
PropMon. Run it again after adapting the engine for the Pi, and after taking
a new rating_model.json together with its matching test_vectors.json."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import field_engine as fe

HERE = os.path.dirname(os.path.abspath(__file__))


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _check(case, lat, lon, tz, model, scale="dx"):
    hist = [(_t(t), kp) for t, kp in case["kp_history"]]
    got = fe.rate_all(case["sfi"], case["k_index"], hist, _t(case["when_utc"]), lat, lon, tz, model, scale)
    exp = case["expected"]
    assert got["penalty"] == exp["penalty"], (case["when_utc"], got["penalty"], exp["penalty"])
    assert got["bands"] == exp["bands"], (case["when_utc"], got["bands"], exp["bands"])


def test_vectors():
    model = fe.load_model()
    vec = json.load(open(os.path.join(HERE, "test_vectors.json"), encoding="utf-8"))
    assert vec["model_version"] == model["model_version"], "test vectors are for a different model_version"
    em = vec["em83"]
    for c in em["cases"]:
        _check(c, em["lat"], em["lon"], em["tz"], model)
    for c in vec["elsewhere"]:
        _check(c, c["lat"], c["lon"], c["tz"], model)


def test_field_day_vectors():
    model = fe.load_model()
    vec = json.load(open(os.path.join(HERE, "test_vectors.json"), encoding="utf-8"))
    fd = vec["field_day"]
    assert fd["version"] == model["field_day"]["version"], "field_day vectors are for a different version"
    em = vec["em83"]
    for c in fd["em83"]:
        _check(c, em["lat"], em["lon"], em["tz"], model, "field_day")
    for c in fd["elsewhere"]:
        _check(c, c["lat"], c["lon"], c["tz"], model, "field_day")
    r = fe.rate_all(120, 1, [], _t("2026-01-24T18:00:00Z"), 33.45, -82.19, "America/New_York", model, "field_day")
    assert r["scale"] == "field_day" and [b["band"] for b in r["bands"]] == fe.FIELD_DAY_BANDS
    six = r["bands"][-1]
    assert six["status"] == "poor" and six["note"]


def test_bad_scale():
    try:
        fe.rate_all(120, 1, [], _t("2026-11-17T15:00:00Z"), 33.45, -82.19, "America/New_York", fe.load_model(), "contest")
    except ValueError:
        return
    raise AssertionError("unknown scale accepted")


def test_unavailable():
    r = fe.rate_all(None, 2, [], _t("2026-11-17T15:00:00Z"), 33.45, -82.19, "America/New_York", fe.load_model())
    assert r["available"] is False and r["bands"] == []
    r = fe.rate_all(120, None, [], _t("2026-11-17T15:00:00Z"), 33.45, -82.19, "America/New_York", fe.load_model(), "field_day")
    assert r["available"] is False and r["bands"] == [] and r["scale"] == "field_day"


if __name__ == "__main__":
    m = fe.load_model()
    vec = json.load(open(os.path.join(HERE, "test_vectors.json"), encoding="utf-8"))
    test_vectors()
    test_field_day_vectors()
    n = len(vec["em83"]["cases"]) + len(vec["elsewhere"])
    nf = len(vec["field_day"]["em83"]) + len(vec["field_day"]["elsewhere"])
    test_bad_scale()
    test_unavailable()
    print(f"OK: {n} dx vectors match (model {m['model_version']}); "
          f"{nf} field_day vectors match (scale {m['field_day']['version']})")
