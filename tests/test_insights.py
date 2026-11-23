"""Тесты структурных наблюдений: metrics.json → ``insights``.

``build_insights`` — чистая функция над сериализованными разделами;
наблюдения порождаются только там, где есть числа, а не нули «по умолчанию».
"""

from finanalyst.metrics import build_insights

INSIGHT_KEYS = {"type", "title", "driver", "contribution_pct", "severity", "value"}
SEVERITIES = {"info", "warn"}


def _check_insight(insight: dict, insight_type: str, severity: str) -> None:
    assert set(insight) == INSIGHT_KEYS, insight
    assert insight["type"] == insight_type
    assert insight["severity"] == severity
    assert insight["driver"]
    assert insight["title"]


def portfolio(par30: float | None) -> dict:
    return {"portfolio": {"totals": {"par30_pct": par30}} if par30 is not None else {}}


def unit(ltv_cac: float | None) -> dict:
    return {"unit_economics": {"ltv_cac": ltv_cac} if ltv_cac is not None else {}}


def funnel(rows: list[dict] | None) -> dict:
    return {"funnel": {"rows": rows} if rows is not None else {}}


def macro(anomalies: list, points: int | None) -> dict:
    return {"macro": {"anomalies": anomalies, "points": points}}


def test_high_par30_is_warn():
    sections = portfolio(15.0) | unit(5.0)
    insights = build_insights(sections)
    assert len(insights) == 1
    _check_insight(insights[0], "high_par30", "warn")
    assert insights[0]["value"] == 15.0
    assert insights[0]["contribution_pct"] is None


def test_par30_at_threshold_is_not_an_insight():
    assert build_insights(portfolio(10.0)) == []


def test_par30_missing_is_not_an_insight():
    assert build_insights({"portfolio": {"totals": {}}}) == []


def test_low_ltv_cac_is_warn():
    sections = portfolio(5.0) | unit(2.5)
    insights = build_insights(sections)
    assert len(insights) == 1
    _check_insight(insights[0], "low_ltv_cac", "warn")
    assert insights[0]["value"] == 2.5


def test_ltv_cac_none_is_not_an_insight():
    assert build_insights(unit(None)) == []


def test_funnel_biggest_loss_is_info():
    sections = funnel([
        {"stage": "заявка", "lost": 40.0, "stage_pct": 50.0},
        {"stage": "одобрение", "lost": 10.0, "stage_pct": 75.0},
    ])
    insights = build_insights(sections)
    assert len(insights) == 1
    _check_insight(insights[0], "funnel_biggest_loss", "info")
    assert insights[0]["value"] == "заявка"


def test_funnel_without_lost_is_not_an_insight():
    assert build_insights(funnel([{"stage": "лид", "reached": 100}])) == []


def test_macro_anomalies_are_info():
    sections = macro([{"period_start": "2024-01"} for _ in range(4)], 40)
    insights = build_insights(sections)
    assert len(insights) == 1
    _check_insight(insights[0], "macro_anomalies", "info")
    assert insights[0]["value"] == 4
    assert insights[0]["contribution_pct"] == 10.0


def test_macro_without_anomalies_is_not_an_insight():
    assert build_insights(macro([], 40)) == []


def test_empty_sections_return_empty_insights():
    assert build_insights({}) == []
    assert build_insights({"portfolio": {}, "unit_economics": {}}) == []


def test_combined_sections_give_multiple_insights():
    sections = portfolio(12.0) | unit(2.0) | macro([{"p": 1}], 20)
    insights = build_insights(sections)
    types = [item["type"] for item in insights]
    assert types == ["high_par30", "low_ltv_cac", "macro_anomalies"]
    for insight in insights:
        assert set(insight) == INSIGHT_KEYS
        assert insight["severity"] in {"info", "warn"}
