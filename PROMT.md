Репозиторий: /home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill/ Ветка: main, чистая. Текущая версия: 2.1.5 (ветка, тег v2.1.5 и GitHub Release созданы). Все тесты зелёные (205 passed), ruff, mypy, check\_skill.py — чисто.

Задача сессии

Реализовать пункт 1 из плана развития: migration matrices + vintage analysis для портфеля. Новый функционал: матрица переходов PAR-бакетов между двумя срезами портфеля + винтажный анализ (баланс и доля клиентов с просрочкой по месяцам открытия).

Данные в репозитории (уже есть, nuevas зависимости не нужны)

skills/financial-analyst/assets/sample/portfolio.csv: account\_id, client\_id, product\_code, open\_date, close\_date, balance, status, dpd, par\_bucket, currency skills/financial-analyst/assets/sample/clients.csv: client\_id, registration\_date (и другие колонки) Образец оформления соглашений: skills/financial-analyst/references/cohorts.md Образец контрактов метрик: skills/financial-analyst/references/metric-contracts.md Образец CLI-флагов и стадий конвейера: skills/financial-analyst/scripts/finanalyst/cli.py (функции *stage*\*, конвейер run()) Образец тестов флагов: tests/test\_cli\_extensions.py, tests/test\_cli\_full\_combinations.py Стиль: ponytail

Минимальные изменения. Не добавлять слои, абстракции с одной реализацией, не резать валидацию и безопасность. Тесты после каждого шага. Не переходить к следующему шагу, пока предыдущий не зелёный. ruff check . — чисто. mypy --ignore-missing-imports skills/financial-analyst/scripts/finanalyst — 0 проблем. Чистый diff, CHANGELOG.md обновлён под новую версию (раздел unreleased или 2.1.6). Версия пакета оставляем без изменений до релиза (беймп — отдельный шаг). План шагов

finanalyst/migration.py — новая чистая функция migration\_matrix(base, target, buckets): строки = PAR-бакет базового среза, столбцы = бакет целевого среза, значения = доля клиентов (или баланса) в процентах. BUCKETS по умолчанию: current, 30+, 60+, 90+. finanalyst/migration.py — функция vintage\_analysis(portfolio, clients, as\_of): срез по месяцам open\_date — баланс, количество счетов, доля с dpd ≥ 30 / 60 / 90 на дату as\_of. Краевые случаи по metric-contracts.md: пустая таблица → предупреждение и пустой раздел (не код 2, если не требуется иначе); balance = 0 → null; отсутствующие колонки → предупреждение, а не падение. CLI: флаги --migration и --vintage в build\_parser(), стадии \_stage\_migration / \_stage\_vintage в run(), разделы migration и vintage в metrics.json. Тесты: tests/test\_migration.py (сумма по строкам матрицы = 100 %, пустой портфель → пустой раздел + предупреждение), tests/test\_vintage.py (сумма балансов по винтажам ≈ total balance portfolio). references/migration.md — соглашения: какие бакеты, как обрабатывать закрытые счета, календарь (по образцу references/cohorts.md). references/metric-contracts.md — строки для новых метрик (зерно, числитель, знаменатель, краевой случай, источник). Финал: ruff check ., mypy, pytest -q, scripts/check\_skill.py — всё зелёное. CHANGELOG.md — раздел unreleased / 2.1.6. Коммит. Критерии готовности (Definition of Done)

migration\_matrix и vintage\_analysis импортируются и работают на assets/sample. CLI-флаги --migration и --vintage не ломают дефолтный запуск (без флагов набор разделов прежний). metrics.json содержит разделы migration и vintage при соответствующих флагах. Новые тесты проходят, старые не сломались (205+ тестов). ruff, mypy, check\_skill.py — чисто. Команды для старта

cd "/home/anony/Documents/Личное/Проект приложения для аналитика/financial-analyst-skill" source .venv/bin/activate pytest -x -q ruff check . mypy --ignore-missing-imports skills/financial-analyst/scripts/finanalyst Не делать сейчас

Чанковое чтение (сломает тождества PVM/bridge/когорт). YAML-конфиг, PostgreSQL-адаптер — это пункты 2 и 3, отдельные сессии. Беймп версии и релиз — отдельный шаг после реализации.

