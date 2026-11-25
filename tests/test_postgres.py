"""Интеграционные тесты PostgreSQL-адаптера.

Проверяют работу с реальной БД через SQLAlchemy/psycopg.
Запускаются в CI с PostgreSQL service container.
Локально пропускаются, если нет доступной БД (pytest -m postgres).
"""

from __future__ import annotations

import os

import pytest

# Пропускаем если нет postgres зависимостей
pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from finanalyst.adapters import postgres
from finanalyst.core import KNOWN_TABLES

# Маркер для интеграционных тестов PostgreSQL
# Запускаются в CI, локально пропускаются если нет БД
postgres_only = pytest.mark.skipif(
    not os.getenv("PGHOST") and not os.getenv("CI"),
    reason="PostgreSQL integration tests require running database (set PGHOST or run in CI)"
)


@pytest.fixture(scope="session")
def pg_config() -> postgres.PostgresConfig:
    """Конфигурация подключения к тестовой БД (из env или дефолты CI)."""
    return postgres.PostgresConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        database=os.getenv("PGDATABASE", "testdb"),
        user=os.getenv("PGUSER", "testuser"),
        password=os.getenv("PGPASSWORD", "testpass"),
        schema=os.getenv("PGSCHEMA", "public"),
    )


@pytest.fixture(scope="session")
def pg_engine(pg_config):
    """SQLAlchemy engine для тестов."""
    engine = postgres.create_engine_from_config(pg_config)
    yield engine
    engine.dispose()


@postgres_only
def test_postgres_connection(pg_engine):
    """Базовое соединение с БД работает."""
    with pg_engine.connect() as conn:
        result = conn.execute("SELECT 1")
        assert result.scalar() == 1


@postgres_only
def test_postgres_list_tables(pg_config, pg_engine):
    """Видим созданные тестовые таблицы."""
    with pg_engine.connect() as conn:
        conn.execute("SET search_path TO public")
        result = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        tables = {row[0] for row in result}

    # Все таблицы из sample должны быть загружены
    expected = {"portfolio", "clients", "products", "finance", "funnel_events", "sales"}
    assert expected.issubset(tables), f"Не хватает таблиц: {expected - tables}"


@postgres_only
def test_load_tables_postgres_basic(pg_config):
    """Полная загрузка всех известных таблиц."""
    tables = postgres.load_tables_postgres(pg_config)

    # Проверяем что загрузились все 6 таблиц
    assert len(tables) == 6
    assert set(tables.keys()) == {"portfolio", "clients", "products", "finance", "funnel_events", "sales"}

    # Проверяем что данные не пустые
    for name, df in tables.items():
        assert not df.empty, f"Таблица {name} пуста"
        assert len(df) > 0, f"Таблица {name} имеет 0 строк"


@postgres_only
def test_load_tables_postgres_skip_patterns(pg_config):
    """Пропуск таблиц по skip_patterns."""
    tables = postgres.load_tables_postgres(pg_config, skip_patterns=("portfolio",))
    assert "portfolio" not in tables
    assert len(tables) == 5


@postgres_only
def test_load_tables_postgres_chunksize(pg_config):
    """Чанковое чтение (chunksize) работает."""
    pg_config.chunksize = 10
    tables = postgres.load_tables_postgres(pg_config)

    assert len(tables) == 6
    for name, df in tables.items():
        assert not df.empty


@postgres_only
def test_load_tables_postgres_streaming(pg_config):
    """Streaming-чтение через генератор."""
    pg_config.chunksize = 10
    chunks = list(postgres.load_tables_postgres_streaming(pg_config))

    assert len(chunks) > 0
    # Каждый чанк — словарь таблиц
    for chunk in chunks:
        assert isinstance(chunk, dict)
        for name, df in chunk.items():
            assert isinstance(df.columns.tolist(), list)


@postgres_only
def test_load_tables_postgres_streaming_aggregates(pg_config):
    """Streaming-чтение даёт те же данные что и полная загрузка."""
    pg_config.chunksize = 10

    # Полная загрузка
    full_tables = postgres.load_tables_postgres(pg_config)

    # Streaming: агрегируем чанки
    stream_tables = {}
    for chunk in postgres.load_tables_postgres_streaming(pg_config):
        for name, df in chunk.items():
            if name in stream_tables:
                import pandas as pd
                stream_tables[name] = pd.concat([stream_tables[name], df], ignore_index=True)
            else:
                stream_tables[name] = df

    # Сравниваем
    assert set(full_tables.keys()) == set(stream_tables.keys())
    for name in full_tables:
        import pandas as pd
        pd.testing.assert_frame_equal(
            full_tables[name].sort_values(full_tables[name].columns[0]).reset_index(drop=True),
            stream_tables[name].sort_values(stream_tables[name].columns[0]).reset_index(drop=True),
            check_dtype=False,
        )


def test_postgres_config_from_env(pg_config):
    """Конфиг корректно собирается из переменных окружения."""
    assert pg_config.host == "localhost"
    assert pg_config.port == 5432
    assert pg_config.database == "testdb"
    assert pg_config.user == "testuser"
    assert pg_config.password == "testpass"
    assert pg_config.schema == "public"


def test_postgres_config_from_dict():
    """Конфиг корректно собирается из словаря (YAML)."""
    data = {
        "host": "db.example.com",
        "port": 5433,
        "database": "mydb",
        "user": "myuser",
        "password": "secret",
        "schema": "analytics",
        "chunksize": 5000,
    }
    config = postgres._config_from_dict(data)

    assert config.host == "db.example.com"
    assert config.port == 5433
    assert config.database == "mydb"
    assert config.user == "myuser"
    assert config.password == "secret"
    assert config.schema == "analytics"
    assert config.chunksize == 5000


def test_get_postgres_config_priority(pg_config):
    """Приоритет конфига: CLI > YAML > ENV > defaults."""
    # ENV уже задан в fixture pg_config
    cli_args = {
        "pg_host": "cli-host",
        "pg_port": 5434,
        "pg_database": "cli-db",
        "pg_user": "cli-user",
        "pg_password": "cli-pass",
        "pg_schema": "cli-schema",
        "pg_chunksize": 9999,
    }
    yaml_config = {
        "host": "yaml-host",
        "port": 5435,
        "database": "yaml-db",
        "user": "yaml-user",
        "password": "yaml-pass",
        "schema": "yaml-schema",
        "chunksize": 8888,
    }

    result = postgres.get_postgres_config(cli_args=cli_args, yaml_config=yaml_config)

    # CLI побеждает
    assert result.host == "cli-host"
    assert result.port == 5434
    assert result.database == "cli-db"
    assert result.user == "cli-user"
    assert result.password == "cli-pass"
    assert result.schema == "cli-schema"
    assert result.chunksize == 9999


def test_get_postgres_config_yaml_over_env():
    """YAML побеждает ENV когда CLI не задан."""
    # Сбрасываем ENV (используем дефолты)
    import os
    old_env = {k: os.environ.get(k) for k in ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSCHEMA", "PGCHUNKSIZE"]}
    for k in old_env:
        if k in os.environ:
            del os.environ[k]

    try:
        # Создаем чистый конфиг без фикстуры (чтобы не подхватывал ENV)
        # yaml_config ожидает ключ "postgres" внутри
        result = postgres.get_postgres_config(
            cli_args={},
            yaml_config={
                "postgres": {
                    "host": "yaml-host",
                    "port": 5435,
                    "database": "yaml-db",
                    "user": "yaml-user",
                    "password": "yaml-pass",
                    "schema": "yaml-schema",
                    "chunksize": 8888,
                }
            },
        )

        assert result.host == "yaml-host"
        assert result.port == 5435
        assert result.database == "yaml-db"
        assert result.user == "yaml-user"
        assert result.password == "yaml-pass"
        assert result.schema == "yaml-schema"
        assert result.chunksize == 8888
    finally:
        # Восстанавливаем ENV
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v


def test_postgres_dsn_properties(pg_config):
    """DSN строки формируются корректно."""
    dsn = pg_config.dsn
    async_dsn = pg_config.async_dsn

    assert "postgresql+psycopg://" in dsn
    assert "testuser:testpass@localhost:5432/testdb" in dsn
    assert "postgresql://testuser:testpass@localhost:5432/testdb" in async_dsn


@postgres_only
def test_postgres_known_tables_loaded(pg_config):
    """Все таблицы из KNOWN_TABLES, которые есть в БД, загружаются."""
    tables = postgres.load_tables_postgres(pg_config)

    engine = postgres.create_engine_from_config(pg_config)
    with engine.connect() as conn:
        conn.execute("SET search_path TO public")
        result = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        existing = {row[0].lower() for row in result}
    engine.dispose()

    # Все известные таблицы, которые существуют в БД, должны быть загружаться
    for known in KNOWN_TABLES:
        if known in existing:
            assert known in tables, f"Известная таблица {known} не загрузилась"
        # macro_survey в БД нет — это нормально (только в файлах)
