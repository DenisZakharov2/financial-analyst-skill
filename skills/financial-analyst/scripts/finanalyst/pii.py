"""Детекция персональных данных (ПДн) и сквозная HMAC-псевдонимизация ключей.

Модуль предназначен для пакета `finanalyst` (skills/financial-analyst/scripts/finanalyst/pii.py).
Не изменяет числовые финансовые показатели (balance, amount, interest_income и др.)
и сохраняет ссылочную целостность внешних ключей (client_id, account_id, application_id)
между всеми таблицами выгрузки.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Literal

import pandas as pd

PII_COLUMN_NAMES: frozenset[str] = frozenset(
    {
        "fio",
        "full_name",
        "client_name",
        "name",
        "first_name",
        "last_name",
        "middle_name",
        "patronymic",
        "passport",
        "passport_number",
        "passport_series",
        "snils",
        "inn",
        "phone",
        "phone_number",
        "mobile",
        "email",
        "e_mail",
        "card_number",
        "pan",
        "address",
        "home_address",
        "birth_date",
        "dob",
    }
)

JOIN_KEY_COLUMNS: frozenset[str] = frozenset(
    {
        "client_id",
        "account_id",
        "application_id",
        "deal_id",
        "event_id",
        "transaction_id",
        "manager_id",
        "campaign_id",
    }
)

SAFE_CODE_COLUMNS: frozenset[str] = frozenset(
    {
        "product_code",
        "product_type",
        "segment",
        "region",
        "acquisition_channel",
        "channel",
        "stage",
        "funnel_stage",
        "status",
        "result",
        "currency",
        "par_bucket",
        "gender",
        "period",
        "open_date",
        "close_date",
        "registration_date",
        "application_date",
        "stage_date",
        "event_date",
    }
)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RU_RE = re.compile(
    r"^(?:\+7|7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}$"
)
FIO_RU_RE = re.compile(
    r"^[А-ЯЁ][а-яёА-ЯЁ]+(?:-[А-ЯЁ][а-яёА-ЯЁ]+)?\s+"
    r"[А-ЯЁ][а-яёА-ЯЁ]+\s+"
    r"[А-ЯЁ][а-яёА-ЯЁ]+$"
)
DIGITS_ONLY_RE = re.compile(r"\D+")

# Пороги детекции ПДн по содержимому колонки (доля совпадений в выборке).
PII_CONTENT_THRESHOLD = 0.05
FIO_CONTENT_THRESHOLD = 0.10
SNILS_CONTENT_THRESHOLD = 0.10
CHECKSUM_CONTENT_THRESHOLD = 0.25


def _pii_threshold(sample_size: int) -> int:
    """Порог для колонок с надёжным признаком (email, телефон).

    Совпадение шаблона почти не бывает случайным, поэтому достаточно 5 %
    выборки; на маленькой выборке хватает одного совпадения, на большой
    порог ограничен тремя строками.
    """
    if sample_size <= 0:
        return 1
    return max(1, min(3, int(PII_CONTENT_THRESHOLD * sample_size)))


def _fio_threshold(sample_size: int) -> int:
    """Порог для ФИО: сигнал слабее (три слова могут быть техническим кодом)."""
    if sample_size <= 0:
        return 2
    return max(2, min(3, int(FIO_CONTENT_THRESHOLD * sample_size)))


def _snils_threshold(sample_size: int) -> int:
    """Порог для СНИЛС: контрольное число проходит примерно 1 % случайных номеров."""
    if sample_size <= 0:
        return 2
    return max(2, min(3, int(SNILS_CONTENT_THRESHOLD * sample_size)))


def _checksum_threshold(sample_size: int) -> int:
    """Порог для карт по Луну и ИНН.

    Контрольные разряды проходят около 10 % случайных чисел, поэтому порог
    5 % здесь давал бы ложные срабатывания на технических идентификаторах
    (16-значные номера операций, 10-значные номера договоров) — колонка была
    бы молча удалена при обезличивании. Порог 25 % надёжно отделяет настоящие
    карты и ИНН от случайно подходящих номеров.
    """
    if sample_size <= 0:
        return 3
    return max(3, int(-(-CHECKSUM_CONTENT_THRESHOLD * sample_size // 1)))


@dataclass(frozen=True)
class PIIFinding:
    """Запись об обнаруженном признаке ПДн без сохранения сырых персональных значений."""

    table: str
    column: str
    pii_type: str
    matched_rows: int
    action: str  # "blocked" | "dropped" | "hmac_pseudonymized"

    def to_dict(self) -> dict[str, str | int]:
        return {
            "table": self.table,
            "column": self.column,
            "pii_type": self.pii_type,
            "matched_rows": int(self.matched_rows),
            "action": self.action,
        }


def is_valid_inn(value: str) -> bool:
    """Проверка ИНН физлица (12 цифр) или юрлица (10 цифр) по контрольным разрядам ФНС."""
    digits = DIGITS_ONLY_RE.sub("", value)
    if len(digits) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        checksum = sum(int(d) * w for d, w in zip(digits[:9], weights)) % 11 % 10
        return checksum == int(digits[9]) and digits != "0000000000"
    if len(digits) == 12:
        w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        c1 = sum(int(d) * w for d, w in zip(digits[:10], w1)) % 11 % 10
        c2 = sum(int(d) * w for d, w in zip(digits[:11], w2)) % 11 % 10
        return c1 == int(digits[10]) and c2 == int(digits[11]) and digits != "000000000000"
    return False


def is_valid_snils(value: str) -> bool:
    """Проверка 11-значного СНИЛС по контрольному числу ПФР/СФР.

    Дополнительно проверяется правило СФР: в первых девяти разрядах номера
    не может быть трёх одинаковых цифр подряд (``111-111-111``). Без этой
    проверки технические 11-значные идентификаторы с корректной контрольной
    суммой проходили бы как СНИЛС.
    """
    digits = DIGITS_ONLY_RE.sub("", value)
    if len(digits) != 11 or digits[:9] <= "001001998":
        return False
    if re.search(r"(\d)\1\1", digits[:9]):
        return False
    total = sum(int(digits[i]) * (9 - i) for i in range(9))
    if total < 100:
        check = total
    elif total in (100, 101):
        check = 0
    else:
        check = total % 101
        if check == 100:
            check = 0
    return check == int(digits[9:11])


def is_valid_luhn_card(value: str) -> bool:
    """Проверка 16-значного номера банковской карты по алгоритму Луна."""
    digits = DIGITS_ONLY_RE.sub("", value)
    if len(digits) != 16 or len(set(digits)) == 1:
        return False
    total = 0
    for idx, ch in enumerate(reversed(digits)):
        num = int(ch)
        if idx % 2 == 1:
            num *= 2
            if num > 9:
                num -= 9
        total += num
    return total % 10 == 0


def _detect_series_pii_type(series: pd.Series, col_name: str) -> tuple[str | None, int]:
    """Определить тип ПДн в колонке по имени и содержимому строковых значений."""
    norm_col = col_name.strip().lower()
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if non_null.empty:
        if norm_col in PII_COLUMN_NAMES:
            return f"column_name:{norm_col}", 0
        return None, 0

    if norm_col in PII_COLUMN_NAMES:
        return f"column_name:{norm_col}", int(len(non_null))

    if norm_col in SAFE_CODE_COLUMNS or norm_col in JOIN_KEY_COLUMNS:
        return None, 0

    if not (
        pd.api.types.is_object_dtype(series.dtype)
        or pd.api.types.is_string_dtype(series.dtype)
    ):
        return None, 0

    sample = non_null.head(500)
    # Порог по содержимому: прежние 30 % (50 % для ИНН) пропускали колонки,
    # где ПДн заполнены у 5–20 % клиентов. Для email и телефона (надёжный
    # шаблон) достаточно 5 % выборки; для ФИО, СНИЛС, карт и ИНН пороги выше —
    # см. функции _pii_threshold, _fio_threshold, _snils_threshold,
    # _checksum_threshold.
    # Avoid .map() which triggers segfault in pandas 2.2.3/numpy 2.1.3
    email_hits = int(sum(bool(EMAIL_RE.match(x)) for x in sample))
    if email_hits >= _pii_threshold(len(sample)):
        return "email", email_hits

    phone_hits = int(sum(bool(PHONE_RU_RE.match(x)) for x in sample))
    if phone_hits >= _pii_threshold(len(sample)):
        return "phone_ru", phone_hits

    # ФИО: сигнал слабее (два слова могут быть техническим кодом), поэтому порог
    # выше — 10 % совпадений в выборке.
    fio_hits = int(sum(bool(FIO_RU_RE.match(x)) for x in sample))
    if fio_hits >= _fio_threshold(len(sample)):
        return "fio_ru", fio_hits

    card_hits = int(sum(is_valid_luhn_card(x) for x in sample))
    if card_hits >= _checksum_threshold(len(sample)):
        return "bank_card_luhn", card_hits

    snils_hits = int(sum(is_valid_snils(x) for x in sample))
    if snils_hits >= _snils_threshold(len(sample)):
        return "snils_checksum", snils_hits

    inn_hits = int(sum(is_valid_inn(x) for x in sample))
    if inn_hits >= _checksum_threshold(len(sample)):
        return "inn_checksum", inn_hits

    return None, 0


def scan_pii(tables: dict[str, pd.DataFrame]) -> list[PIIFinding]:
    """Просканировать все таблицы на наличие колонок с прямыми ПДн."""
    findings: list[PIIFinding] = []
    for table_name, df in tables.items():
        if df is None or df.empty:
            continue
        for col in df.columns:
            pii_type, matched = _detect_series_pii_type(df[col], str(col))
            if pii_type is not None:
                findings.append(
                    PIIFinding(
                        table=table_name,
                        column=str(col),
                        pii_type=pii_type,
                        matched_rows=matched,
                        action="detected",
                    )
                )
    return findings


def _hmac_token(value: object, key_bytes: bytes, prefix: str) -> object:
    """Детерминированный HMAC-токен для идентификатора с сохранением пустых значений."""
    if pd.isna(value):
        return value
    raw = str(value).strip()
    if not raw:
        return value
    digest = hmac.new(key_bytes, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def sanitize_tables(
    tables: dict[str, pd.DataFrame],
    mode: Literal["strict", "redact", "off"] = "strict",
    secret_key: str | None = None,
    pseudonymize_keys: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict]:
    """Проверить и обезличить таблицы без искажения финансовых показателей.

    Режимы (`mode`):
    - `strict`: при обнаружении прямых ПДн выбрасывает `ValueError` (для Quality Gate fail).
    - `redact`: удаляет колонки с прямыми ПДн и псевдонимизирует `JOIN_KEY_COLUMNS`
      единым ключом HMAC-SHA256 во всех таблицах (внешние ключи полностью сохраняются).
    - `off`: возвращает таблицы без изменений.
    """
    if mode not in ("strict", "redact", "off"):
        raise ValueError(f"Неизвестный режим обезличивания: {mode}")

    if mode == "off":
        return tables, {
            "mode": "off",
            "pii_found": False,
            "findings": [],
            "pseudonymized_columns": [],
            "assumption": "Проверка и обезличивание ПДн отключены (--pii-mode=off).",
        }

    findings = scan_pii(tables)
    if mode == "strict" and findings:
        details = ", ".join(f"{f.table}.{f.column} ({f.pii_type})" for f in findings)
        raise ValueError(
            f"Контроль качества ПДн не пройден (pii_columns_absent): обнаружены ПДн в {details}"
        )

    raw_key = secret_key or os.environ.get("FINANALYST_PII_KEY") or secrets.token_hex(32)
    key_bytes = raw_key.encode("utf-8")

    prefix_map = {
        "client_id": "CL",
        "account_id": "AC",
        "application_id": "AP",
        "deal_id": "DL",
        "event_id": "EV",
        "transaction_id": "TX",
        "manager_id": "MG",
        "campaign_id": "CP",
    }

    sanitized: dict[str, pd.DataFrame] = {}
    applied_findings: list[PIIFinding] = []
    pseudonymized_cols: list[str] = []

    pii_by_table: dict[str, set[str]] = {}
    for item in findings:
        pii_by_table.setdefault(item.table, set()).add(item.column)
        applied_findings.append(
            PIIFinding(
                table=item.table,
                column=item.column,
                pii_type=item.pii_type,
                matched_rows=item.matched_rows,
                action="dropped",
            )
        )

    for table_name, df in tables.items():
        if df is None:
            sanitized[table_name] = df
            continue
        out_df = df.copy()
        drop_cols = [c for c in out_df.columns if str(c) in pii_by_table.get(table_name, set())]
        if drop_cols:
            out_df = out_df.drop(columns=drop_cols)

        if mode == "redact" and pseudonymize_keys:
            for col in out_df.columns:
                norm_col = str(col).strip().lower()
                if norm_col in JOIN_KEY_COLUMNS:
                    prefix = prefix_map.get(norm_col, "ID")
                    # Avoid .map() which triggers segfault in pandas 2.2.3/numpy 2.1.3
                    out_df[col] = [_hmac_token(v, key_bytes, prefix) for v in out_df[col]]
                    pseudonymized_cols.append(f"{table_name}.{col}")

        sanitized[table_name] = out_df

    return sanitized, {
        "mode": mode,
        "pii_found": bool(findings),
        "findings": [f.to_dict() for f in applied_findings],
        "pseudonymized_columns": sorted(set(pseudonymized_cols)),
        "assumption": (
            "Прямые ПДн удалены на уровне колонок, идентификаторы заменены сквозным HMAC-SHA256 "
            "с сохранением связей между таблицами; числовые финансовые поля не изменялись."
            if mode == "redact"
            else "Входной контроль ПДн пройден: запрещённые колонки отсутствуют."
        ),
    }
