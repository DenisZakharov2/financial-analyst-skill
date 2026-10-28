"""Графики matplotlib и интерактивный HTML-дашборд (Plotly, офлайн)."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # без дисплея
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PALETTE = {
    "navy": "#1f2937",
    "slate": "#374151",
    "grid": "#e5e7eb",
    "positive": "#10b981",
    "negative": "#ef4444",
    "warning": "#f59e0b",
    "neutral": "#3b82f6",
    "white": "#ffffff",
    "muted": "#6b7280",
}

SERIES_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444",
                 "#0891b2", "#65a30d", "#db2777"]

CHART_TITLES = {
    "portfolio": "Структура портфеля по типам продуктов",
    "funnel": "Воронка: доля дошедших до этапа",
    "macro": "Динамика показателя",
    "anomalies": "Аномалии показателя",
    "forecast": "Прогноз: базовый сценарий и границы",
    "correlation": "Корреляционная матрица",
}


def setup_matplotlib() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.labelsize": 10,
        "axes.edgecolor": PALETTE["grid"],
        "axes.labelcolor": PALETTE["slate"],
        "axes.titlecolor": PALETTE["navy"],
        "xtick.color": PALETTE["muted"],
        "ytick.color": PALETTE["muted"],
        "grid.color": PALETTE["grid"],
        "grid.alpha": 0.7,
        "figure.facecolor": PALETTE["white"],
        "savefig.facecolor": PALETTE["white"],
    })


def _empty(path: Path) -> None:
    if path.exists():
        path.unlink()


def chart_portfolio(rows: pd.DataFrame, path: Path) -> Path | None:
    if rows is None or rows.empty or "total_balance" not in rows:
        _empty(path)
        return None
    data = rows.dropna(subset=["total_balance"]).sort_values("total_balance")
    if data.empty or not data["total_balance"].abs().sum():
        _empty(path)
        return None

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.barh(data["product_type"], data["total_balance"] / 1e6, color=PALETTE["neutral"], height=0.6)
    for y, value in enumerate(data["total_balance"] / 1e6):
        ax.text(value, y, f" {value:,.1f}", va="center", fontsize=9, color=PALETTE["slate"])
    ax.set_xlabel("Остаток, млн руб.")
    ax.set_title(CHART_TITLES["portfolio"])
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_funnel(rows: pd.DataFrame, path: Path) -> Path | None:
    if rows is None or rows.empty:
        _empty(path)
        return None
    fig, ax = plt.subplots(figsize=(9, 4.5))
    share = rows["from_start_pct"].astype(float)
    ax.bar(rows["stage"], share, color=PALETTE["neutral"], width=0.6)
    for x, value in enumerate(share):
        if not np.isnan(value):
            ax.text(x, value + 1.5, f"{value:.1f}%", ha="center", fontsize=9, color=PALETTE["slate"])
    ax.set_ylim(0, max(105, float(np.nanmax(share)) + 10))
    ax.set_ylabel("% от числа лидов")
    ax.set_title(CHART_TITLES["funnel"])
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_series(df: pd.DataFrame, value_col: str, period_col: str, path: Path,
                 title: str = CHART_TITLES["macro"], label: str = "") -> Path | None:
    if df is None or df.empty or value_col not in df:
        _empty(path)
        return None
    data = df.dropna(subset=[period_col, value_col])
    if data.empty:
        _empty(path)
        return None

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(data[period_col], data[value_col], color=PALETTE["neutral"],
            linewidth=2.2, marker="o", markersize=3.5, label=label or None)
    if len(data) >= 6:
        window = max(3, min(12, len(data) // 4))
        ax.plot(data[period_col], data[value_col].rolling(window, min_periods=1).mean(),
                color=PALETTE["warning"], linewidth=1.6, linestyle="--",
                label=f"Скользящее среднее, {window} п.")
    ax.set_title(f"{title}{(' — ' + label) if label else ''}")
    ax.set_ylabel("Значение")
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    if label or len(data) >= 6:
        ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_anomalies(anomalies: pd.DataFrame, path: Path, label: str = "") -> Path | None:
    if anomalies is None or anomalies.empty or "_z" not in anomalies:
        _empty(path)
        return None
    data = anomalies.sort_values("_z")
    labels = (data["period_start"].dt.strftime("%Y-%m-%d")
              if pd.api.types.is_datetime64_any_dtype(data["period_start"])
              else data["period_start"].astype(str))
    fig, ax = plt.subplots(figsize=(10, 4.5))
    colors = [PALETTE["negative"] if value > 0 else PALETTE["positive"] for value in data["_z"]]
    ax.barh(labels, data["_z"], color=colors, height=0.6)
    ax.axvline(3, color=PALETTE["negative"], linestyle="--", linewidth=1)
    ax.axvline(-3, color=PALETTE["negative"], linestyle="--", linewidth=1)
    ax.set_xlabel("Робастный Z-score")
    ax.set_title(f"{CHART_TITLES['anomalies']}{(' — ' + label) if label else ''}")
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_forecast(forecast: dict, path: Path) -> Path | None:
    if not forecast or not forecast.get("available"):
        _empty(path)
        return None
    periods = forecast["periods"]
    scenarios = forecast["scenarios"]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    base = scenarios.get("базовый", [])
    ax.plot(periods, base, color=PALETTE["neutral"], linewidth=2.4, marker="o", label="Базовый")
    if "оптимистичный" in scenarios:
        ax.plot(periods, scenarios["оптимистичный"], color=PALETTE["positive"],
                linewidth=1.4, linestyle="--", label="Оптимистичный")
    if "пессимистичный" in scenarios:
        ax.plot(periods, scenarios["пессимистичный"], color=PALETTE["negative"],
                linewidth=1.4, linestyle="--", label="Пессимистичный")
    ax.fill_between(range(len(periods)), scenarios.get("пессимистичный", base),
                    scenarios.get("оптимистичный", base), color=PALETTE["neutral"], alpha=0.08)
    ax.set_title(CHART_TITLES["forecast"])
    ax.set_ylabel("Прогнозное значение")
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_correlation(corr: pd.DataFrame, path: Path) -> Path | None:
    if corr is None or corr.empty or len(corr) < 2:
        _empty(path)
        return None
    fig, ax = plt.subplots(figsize=(8, 6.5))
    image = ax.imshow(corr.values, cmap="RdYlBu_r", vmin=-1, vmax=1, aspect="auto")
    fig.colorbar(image, ax=ax, label="Коэффициент корреляции")
    ax.set_xticks(range(len(corr.columns)), corr.columns, rotation=45, ha="right", fontsize=9)
    ax.set_yticks(range(len(corr.index)), corr.index, fontsize=9)
    for i in range(len(corr.index)):
        for j in range(len(corr.columns)):
            value = corr.iloc[i, j]
            if pd.notna(value):
                ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=8,
                        color="white" if abs(value) > 0.5 else "black")
    ax.set_title(CHART_TITLES["correlation"])
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------
# Дашборд
# --------------------------------------------------------------------------

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script>__PLOTLY__</script>
<style>
  :root { --navy:#1f2937; --slate:#374151; --muted:#6b7280; --line:#e5e7eb;
          --bg:#f8f9fa; --card:#ffffff; --accent:#3b82f6; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--navy);
         font-family:"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  .wrap { max-width:1400px; margin:0 auto; }
  h1 { font-size:26px; margin:0 0 6px; }
  .meta { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .filters { display:flex; flex-wrap:wrap; gap:16px; align-items:end;
             background:var(--card); border:1px solid var(--line); border-radius:10px;
             padding:16px; margin-bottom:20px; }
  .field { display:flex; flex-direction:column; gap:6px; min-width:220px; }
  .field label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  select, input[type=range] { font:inherit; padding:8px 10px; border:1px solid var(--line);
             border-radius:8px; background:#fff; color:var(--navy); }
  input[type=range] { padding:0; accent-color:var(--accent); }
  .hint { font-size:12px; color:var(--muted); }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; }
  .card h2 { font-size:16px; margin:0 0 12px; color:var(--slate); }
  .full { grid-column:1 / -1; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; background:#f3f4f6; color:var(--slate); font-weight:600;
       padding:10px; border-bottom:1px solid var(--line); }
  td { padding:9px 10px; border-bottom:1px solid var(--line); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:#f9fafb; }
  #loading { position:fixed; inset:0; background:rgba(255,255,255,.92); display:flex;
             align-items:center; justify-content:center; z-index:10; font-size:14px; color:var(--muted); }
  .spinner { width:32px; height:32px; border:3px solid var(--line); border-top-color:var(--accent);
             border-radius:50%; animation:spin .8s linear infinite; margin-right:12px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (max-width:1024px) { .grid { grid-template-columns:1fr; } body { padding:14px; } }
  @media (max-width:640px) { h1 { font-size:20px; } .field { min-width:100%; } }
</style>
</head>
<body>
<div id="loading"><div class="spinner"></div>Строим графики…</div>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="meta">Показатель: <b>__INDICATOR__</b> · период: <span id="period-range"></span> · сформировано __GENERATED__</div>

  <div class="filters">
    <div class="field">
      <label for="industry">Отрасль</label>
      <select id="industry"></select>
    </div>
    <div class="field">
      <label for="window">Окно наблюдения: <span id="window-value"></span> периодов</label>
      <input type="range" id="window" min="3" step="1">
    </div>
    <div class="hint">График ниже перестраивается по выбранной отрасли и окну.</div>
  </div>

  <div class="grid">
    <div class="card full"><h2>Динамика показателя</h2><div id="chart-dynamics"></div></div>
    <div class="card"><h2>Z-score по выбранной отрасли</h2><div id="chart-zscore"></div></div>
    <div class="card"><h2>Сводка по отраслям</h2><div id="summary"></div></div>
  </div>
</div>
<script>
const DATA = __DATA__;

function fmt(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 2});
}

function init() {
  const select = document.getElementById("industry");
  DATA.industries.forEach(name => {
    const option = document.createElement("option");
    option.value = name; option.textContent = name;
    select.appendChild(option);
  });

  const slider = document.getElementById("window");
  const maxWindow = DATA.periods.length;
  const startWindow = Math.min(24, maxWindow);
  slider.max = maxWindow;
  slider.value = startWindow;
  document.getElementById("window-value").textContent = startWindow;
  document.getElementById("period-range").textContent =
    DATA.periods[0] + " — " + DATA.periods[DATA.periods.length - 1];

  const traces = DATA.industries.map((name, index) => ({
    x: DATA.periods,
    y: DATA.series[name],
    type: "scatter",
    mode: "lines+markers",
    name: name,
    visible: index === 0,
    line: {width: 2.2},
    marker: {size: 4},
    hovertemplate: "%{x}<br>" + name + ": %{y:.2f}<extra></extra>"
  }));

  const layout = {
    margin: {l: 50, r: 20, t: 10, b: 70},
    height: 460,
    hovermode: "x unified",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "Segoe UI, Roboto, Arial, sans-serif", size: 12, color: "#374151"},
    xaxis: {range: [DATA.periods[Math.max(0, DATA.periods.length - Math.min(24, DATA.periods.length))], DATA.periods[DATA.periods.length - 1]], gridcolor: "#eef1f4"},
    yaxis: {gridcolor: "#eef1f4", zerolinecolor: "#d1d5db"},
    legend: {orientation: "h", y: -0.25}
  };

  Plotly.newPlot("chart-dynamics", traces, layout, {displayModeBar: false, responsive: true});

  function applyWindow() {
    const size = Number(slider.value);
    document.getElementById("window-value").textContent = size;
    const start = Math.max(0, DATA.periods.length - size);
    Plotly.relayout("chart-dynamics", {
      "xaxis.range": [DATA.periods[start], DATA.periods[DATA.periods.length - 1]]
    });
  }
  slider.addEventListener("input", applyWindow);

  select.addEventListener("change", () => {
    const index = DATA.industries.indexOf(select.value);
    Plotly.restyle("chart-dynamics", {visible: DATA.industries.map((_, i) => i === index)});
    renderZScore(select.value);
  });

  function renderZScore(industry) {
    const points = DATA.anomalies.filter(item => item.industry === industry);
    const values = DATA.zseries[industry] || [];
    const trace = {
      x: DATA.periods,
      y: values,
      type: "bar",
      marker: {color: values.map(v => (v >= 0 ? "#ef4444" : "#10b981"))},
      hovertemplate: "%{x}<br>Z = %{y:.2f}<extra></extra>"
    };
    const zlayout = {
      margin: {l: 50, r: 20, t: 10, b: 60},
      height: 360,
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: {family: "Segoe UI, Roboto, Arial, sans-serif", size: 12, color: "#374151"},
      xaxis: {gridcolor: "#eef1f4"},
      yaxis: {gridcolor: "#eef1f4", range: [-4.5, 4.5]},
      shapes: [
        {type: "line", x0: 0, x1: 1, xref: "paper", y0: 3, y1: 3,
         line: {color: "#ef4444", width: 1, dash: "dash"}},
        {type: "line", x0: 0, x1: 1, xref: "paper", y0: -3, y1: -3,
         line: {color: "#ef4444", width: 1, dash: "dash"}}
      ]
    };
    Plotly.react("chart-zscore", [trace], zlayout, {displayModeBar: false, responsive: true});
    document.getElementById("chart-zscore").setAttribute("data-anomalies", points.length);
  }

  renderZScore(DATA.industries[0]);
  applyWindow();

  const rows = DATA.summary.map(item => `
    <tr><td>${item.industry}</td>
        <td class="num">${fmt(item.last)}</td>
        <td class="num">${fmt(item.mean)}</td>
        <td class="num">${fmt(item.min)}</td>
        <td class="num">${fmt(item.max)}</td>
        <td class="num">${item.anomalies}</td></tr>`).join("");
  document.getElementById("summary").innerHTML = `
    <table><thead><tr><th>Отрасль</th><th>Последнее</th><th>Среднее</th>
    <th>Мин</th><th>Макс</th><th>Аномалий</th></tr></thead><tbody>${rows}</tbody></table>`;

  document.getElementById("loading").style.display = "none";
}

window.addEventListener("load", init);
</script>
</body>
</html>
"""


def build_dashboard(survey: pd.DataFrame, indicator: str, path: Path,
                    title: str = "Интерактивный дашборд") -> Path | None:
    """Собрать офлайн-дашборд: фильтр по отрасли + окно наблюдения."""
    if survey is None or survey.empty:
        return None
    data = survey[survey["indicator"] == indicator].dropna(subset=["period_start", "value"])
    if data.empty:
        return None

    wide = (data.pivot_table(index="period_start", columns="industry", values="value", aggfunc="first")
                .sort_index())
    periods = [p.strftime("%Y-%m") if hasattr(p, "strftime") else str(p) for p in wide.index]
    industries = [str(c) for c in wide.columns]

    series = {str(c): [None if pd.isna(v) else round(float(v), 3) for v in wide[c]] for c in wide.columns}

    zseries = {}
    anomalies = []
    for industry in industries:
        values = wide[industry]
        clean = values.dropna()
        if len(clean) < 3:
            zseries[industry] = [None] * len(values)
            continue
        if len(clean) < 50:
            median = clean.median()
            mad = (clean - median).abs().median()
            z = (0.6745 * (values - median) / mad) if mad else values * 0
        else:
            z = (values - clean.mean()) / clean.std(ddof=0)
        zseries[industry] = [None if pd.isna(v) else round(float(v), 3) for v in z]
        for period, value, score in zip(periods, values, z):
            if pd.notna(score) and abs(score) > 3:
                anomalies.append({"industry": industry, "period": period,
                                  "value": round(float(value), 3), "z": round(float(score), 3)})

    summary = []
    for industry in industries:
        values = wide[industry].dropna()
        summary.append({
            "industry": industry,
            "last": round(float(values.iloc[-1]), 3) if len(values) else None,
            "mean": round(float(values.mean()), 3) if len(values) else None,
            "min": round(float(values.min()), 3) if len(values) else None,
            "max": round(float(values.max()), 3) if len(values) else None,
            "anomalies": sum(1 for a in anomalies if a["industry"] == industry),
        })

    payload = {
        "indicator": indicator,
        "periods": periods,
        "industries": industries,
        "series": series,
        "zseries": zseries,
        "anomalies": anomalies,
        "summary": summary,
    }

    from plotly.offline import get_plotlyjs

    html = (DASHBOARD_TEMPLATE
            .replace("__PLOTLY__", get_plotlyjs())
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False))
            .replace("__TITLE__", title)
            .replace("__INDICATOR__", indicator)
            .replace("__GENERATED__", pd.Timestamp.now().strftime("%d.%m.%Y %H:%M")))
    path.write_text(html, encoding="utf-8")
    return path
