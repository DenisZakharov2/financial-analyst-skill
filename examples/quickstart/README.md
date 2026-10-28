# Quickstart

Пять минут от клонирования до готового отчёта.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python skills/financial-analyst/scripts/analyze.py \
  --data-dir skills/financial-analyst/assets/sample \
  --out ./reports \
  --strict
```

Что появится в `./reports`:

```
financial_analyst_report.docx
financial_analyst_report.pdf
financial_analyst_report.xlsx
financial_analyst_report.pptx
financial_analyst_report.rtf
report.html
metrics.json
metrics.csv
charts/portfolio.png
charts/funnel.png
```

Проверьте:

1. `metrics.json` → `sections.portfolio.totals.par30_pct` — число, не `null`.
2. `sections.funnel.rows` → `from_start_pct` убывает.
3. PDF открывается, текст по-русски.
4. XLSX: числа имеют числовой формат, их можно суммировать.

Свой набор данных: положите CSV с именами из
`skills/financial-analyst/references/data-dictionary.md` в отдельный каталог
и передайте его в `--data-dir`.

Ожидаемый вывод `--strict`:

```
Контроль качества: 12 проверок, 0 fail, 0 warn
Готово: ...
```
