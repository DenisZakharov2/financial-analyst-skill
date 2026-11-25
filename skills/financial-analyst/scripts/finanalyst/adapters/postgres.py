"""PostgreSQL-адаптер: чтение таблиц из БД через SQLAlchemy/asyncpg.

Поддерживает:
- Чтение всех известных таблиц (KNOWN_TABLES) по именам
- Параметры подключения через env/конфиг/флаги CLI
- Чанковое чтение для больших таблиц (chunksize)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..core import KNOWN_TABLES, DataLoadError


@dataclass
class PostgresConfig:
    """Конфигурация подключения к PostgreSQL."""

    host: str = "localhost"
    port: int = 5432
    database: str = ""
    user: str = ""
    password: str = ""
    schema: str = "public"
    chunksize: int | None = None

    @property
    def dsn(self) -> str:
        """DSN для SQLAlchemy (sync engine)."""
        return f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    @property
    def async_dsn(self) -> str:
        """DSN для asyncpg (если понадобится асинхронное чтение)."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


def _config_from_env() -> PostgresConfig:
    """Собрать конфиг из переменных окружения."""
    return PostgresConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        database=os.getenv("PGDATABASE", ""),
        user=os.getenv("PGUSER", ""),
        password=os.getenv("PGPASSWORD", ""),
        schema=os.getenv("PGSCHEMA", "public"),
        chunksize=int(os.getenv("PGCHUNKSIZE", "0")) or None,
    )


def _config_from_dict(data: dict) -> PostgresConfig:
    """Собрать конфиг из словаря (YAML-конфиг)."""
    return PostgresConfig(
        host=data.get("host", "localhost"),
        port=data.get("port", 5432),
        database=data.get("database", ""),
        user=data.get("user", ""),
        password=data.get("password", ""),
        schema=data.get("schema", "public"),
        chunksize=data.get("chunksize"),
    )


def create_engine_from_config(config: PostgresConfig) -> Engine:
    """Создать синхронный SQLAlchemy engine."""
    return create_engine(config.dsn, pool_pre_ping=True)


def load_tables_postgres(
    config: PostgresConfig,
    skip_patterns: tuple[str, ...] = (),
) -> dict[str, pd.DataFrame]:
    """Прочитать все известные таблицы из PostgreSQL.

    Возвращает словарь таблиц, как load_tables из core.py.
    Excel-таблицы (macro_survey) в БД не хранятся — их нужно подавать файлами.
    """
    engine = create_engine_from_config(config)
    tables: dict[str, pd.DataFrame] = {}
    errors: list[str] = []

    with engine.connect() as conn:
        # Проверяем схему
        conn.execute(text(f"SET search_path TO {config.schema}"))

        # Получаем список таблиц в схеме
        result = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
            ),
            {"schema": config.schema},
        )
        existing_tables = {row[0].lower() for row in result}

        # Читаем известные таблицы
        for table_name in sorted(KNOWN_TABLES):
            if table_name in skip_patterns:
                continue
            if table_name not in existing_tables:
                continue
            try:
                if config.chunksize:
                    # Чанковое чтение — конкатенируем
                    chunks = pd.read_sql(
                        f'SELECT * FROM "{table_name}"',
                        conn,
                        chunksize=config.chunksize,
                    )
                    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
                else:
                    df = pd.read_sql(f'SELECT * FROM "{table_name}"', conn)
                tables[table_name] = df
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{table_name}: {type(exc).__name__}: {exc}")

    if errors:
        raise DataLoadError(errors, tables)
    return tables


def load_tables_postgres_streaming(
    config: PostgresConfig,
    skip_patterns: tuple[str, ...] = (),
) -> Iterator[dict[str, pd.DataFrame]]:
    """Итеративное чтение таблиц из PostgreSQL чанками.

    Возвращает генератор, выдающий словарь таблиц для каждого чанка.
    Excel-таблицы не поддерживаются (в БД не хранятся).
    """
    if not config.chunksize:
        raise ValueError("chunksize должен быть задан для streaming-режима")

    engine = create_engine_from_config(config)

    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {config.schema}"))

        # Проверяем существующие таблицы
        result = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
            ),
            {"schema": config.schema},
        )
        existing_tables = {row[0].lower() for row in result}

        # Создаем итераторы для каждой таблицы
        readers: dict[str, Iterator[pd.DataFrame]] = {}
        for table_name in sorted(KNOWN_TABLES):
            if any(table_name.startswith(p) for p in skip_patterns):
                continue
            if table_name not in existing_tables:
                continue
            try:
                readers[table_name] = pd.read_sql(
                    f'SELECT * FROM "{table_name}"',
                    conn,
                    chunksize=config.chunksize,
                )
            except Exception as exc:  # noqa: BLE001
                raise DataLoadError([f"{table_name}: {type(exc).__name__}: {exc}"], {})

        # Итеративно выдаем чанки
        active_readers = set(readers.keys())
        while active_readers:
            chunk_tables: dict[str, pd.DataFrame] = {}
            to_remove = set()
            for name in active_readers:
                try:
                    chunk = next(readers[name])
                    chunk_tables[name] = chunk
                except StopIteration:
                    to_remove.add(name)
            active_readers -= to_remove
            if chunk_tables:
                yield chunk_tables


def get_postgres_config(
    cli_args: dict | None = None,
    yaml_config: dict | None = None,
) -> PostgresConfig:
    """Объединить конфиг из приоритетов: CLI > YAML > ENV > defaults.

    Порядок приоритета (позднее перезаписывает более раннее):
    1. defaults
    2. ENV (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSCHEMA, PGCHUNKSIZE)
    3. YAML config (postgres section)
    4. CLI args (--pg-host, --pg-port, --pg-database, --pg-user, --pg-password, --pg-schema, --pg-chunksize)
    """
    config = PostgresConfig()

    # 1. ENV
    env_config = _config_from_env()
    if env_config.host != "localhost":
        config.host = env_config.host
    if env_config.port != 5432:
        config.port = env_config.port
    if env_config.database:
        config.database = env_config.database
    if env_config.user:
        config.user = env_config.user
    if env_config.password:
        config.password = env_config.password
    if env_config.schema != "public":
        config.schema = env_config.schema
    if env_config.chunksize:
        config.chunksize = env_config.chunksize

    # 2. YAML
    if yaml_config and "postgres" in yaml_config:
        yaml_pg = _config_from_dict(yaml_config["postgres"])
        if yaml_pg.host != "localhost":
            config.host = yaml_pg.host
        if yaml_pg.port != 5432:
            config.port = yaml_pg.port
        if yaml_pg.database:
            config.database = yaml_pg.database
        if yaml_pg.user:
            config.user = yaml_pg.user
        if yaml_pg.password:
            config.password = yaml_pg.password
        if yaml_pg.schema != "public":
            config.schema = yaml_pg.schema
        if yaml_pg.chunksize:
            config.chunksize = yaml_pg.chunksize

    # 3. CLI
    if cli_args:
        if cli_args.get("pg_host"):
            config.host = cli_args["pg_host"]
        if cli_args.get("pg_port"):
            config.port = cli_args["pg_port"]
        if cli_args.get("pg_database"):
            config.database = cli_args["pg_database"]
        if cli_args.get("pg_user"):
            config.user = cli_args["pg_user"]
        if cli_args.get("pg_password"):
            config.password = cli_args["pg_password"]
        if cli_args.get("pg_schema"):
            config.schema = cli_args["pg_schema"]
        if cli_args.get("pg_chunksize"):
            config.chunksize = cli_args["pg_chunksize"]

    return config
