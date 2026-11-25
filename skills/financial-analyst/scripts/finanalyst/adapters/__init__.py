"""Адаптеры источников: приводят выгрузку к таблицам словаря данных."""

from . import cbr_survey, client_csv, postgres

__all__ = ["cbr_survey", "client_csv", "postgres"]
