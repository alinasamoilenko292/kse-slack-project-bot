"""
Budget recording tools — replaces the Make (Integromat) payment automation.

Flow (mirrors the Make blueprint):
  1. Manager sends/pastes a payment notification text (from 1C or bs-slack-payment).
  2. parse_payment_message() extracts: counterparty_surname, project (1C name), amount, date.
  3. find_project_budget_row() looks in sheet "general" of the master spreadsheet,
     finds the row where column C contains the project 1C-name.
     Returns: project_sheet_name (col B), spreadsheet_id (from col or same master).
  4. find_expense_row() searches that project sheet for a row where col B or C
     contains counterparty_surname.
  5. get_fact_column() finds the header column matching Fact_MM_YY derived from payment date.
  6. write_payment() checks existing cell value — if empty writes amount, else adds to it.

The main entry point for the agent is record_payment().

SETUP REQUIREMENT:
  The Google service account in google_credentials.json must have:
  - Google Sheets API enabled in Google Cloud Console
  - The master spreadsheet (MASTER_SPREADSHEET_ID) shared with the service account email
  - Each project spreadsheet shared with the service account email
  Scope needed: https://www.googleapis.com/auth/spreadsheets
"""
from __future__ import annotations

import os
import re
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# The master budget spreadsheet (contains "general" index sheet)
# From Make blueprint: 1Bu9ld8JiA7DtOgM8UM4WK91pM93Ct6udBZsnUQBBt5E
# ─────────────────────────────────────────────────────────────────────────────
MASTER_SPREADSHEET_ID = os.environ.get(
    "BUDGET_SPREADSHEET_ID",
    "1Bu9ld8JiA7DtOgM8UM4WK91pM93Ct6udBZsnUQBBt5E",
)
GENERAL_SHEET_NAME = "general"


# ── Google Sheets service ─────────────────────────────────────────────────────

def _get_sheets_service():
    """Build and return a Google Sheets API service (read + write)."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "google_credentials.json")
        creds = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Failed to build Sheets service: {e}")
        return None


# ── Payment text parsing ───────────────────────────────────────────────────────

def parse_payment_message(text: str) -> dict:
    """
    Parse a 1C payment notification message and extract key fields.

    Handles two common formats:
      Format A (structured):
        Контрагент: Прізвище Ім'я По-батькові
        Проєкт: Назва_Проєкту_1С
        Сума документу: 15 000,00 грн
        Дата платежу: 15.03.2026

      Format B (inline/Slack-forwarded):
        "Оплачено | Контрагент Прізвище | Проєкт KSE_MBA | Сума 15000 грн | ..."

    Returns dict with:
        counterparty_surname, project_1c, amount (float), payment_date (str DD.MM.YYYY),
        fact_column (e.g. "Fact_03_26"), raw_text, errors list
    """
    result: dict = {
        "counterparty_surname": None,
        "project_1c": None,
        "amount": None,
        "currency": "UAH",  # "UAH" or "USD"
        "payment_date": None,
        "fact_column": None,
        "errors": [],
    }

    # Counterparty — full name until end of line
    m = re.search(r"Контрагент[:\s]+([^\n\r]+)", text, re.IGNORECASE | re.UNICODE)
    if m:
        result["counterparty_surname"] = m.group(1).strip()
    else:
        result["errors"].append("Не знайдено Контрагент")

    # Project 1C name — full line after Проєкт/Проект
    m = re.search(r"[Пп]ро[єеe]кт[:\s]+([^\n\r,;|]+)", text, re.IGNORECASE | re.UNICODE)
    if m:
        result["project_1c"] = m.group(1).strip()[:120]
    else:
        result["errors"].append("Не знайдено Проєкт")

    # Amount — try UAH first ("Сума документу ... грн"), then USD variants, then any "Сума ... грн"
    # UAH — "Сума документу"
    m = re.search(r"Сума\s+документу[:\s]+([\d\s\xa0.,]+)\s*грн", text, re.IGNORECASE | re.UNICODE)
    if m:
        raw_amount = m.group(1)
        result["currency"] = "UAH"
    else:
        # USD — "Сума документу ... USD/дол/долар"
        m = re.search(
            r"Сума\s+документу[:\s]+([\d\s\xa0.,]+)\s*(?:USD|дол(?:ар(?:ів)?)?\.?)",
            text, re.IGNORECASE | re.UNICODE
        )
        if m:
            raw_amount = m.group(1)
            result["currency"] = "USD"
        else:
            # Generic "Сума ... грн"
            m = re.search(r"Сума[^:\n]*[:\s]+([\d\s\xa0.,]+)\s*грн", text, re.IGNORECASE | re.UNICODE)
            if m:
                raw_amount = m.group(1)
                result["currency"] = "UAH"
            else:
                # Generic "Сума ... USD/дол"
                m = re.search(
                    r"Сума[^:\n]*[:\s]+([\d\s\xa0.,]+)\s*(?:USD|дол(?:ар(?:ів)?)?\.?)",
                    text, re.IGNORECASE | re.UNICODE
                )
                if m:
                    raw_amount = m.group(1)
                    result["currency"] = "USD"

    if m:
        raw_amount = m.group(1)
        clean = re.sub(r"[\s\xa0]", "", raw_amount).replace(",", ".")
        try:
            result["amount"] = float(clean)
        except ValueError:
            result["errors"].append(f"Не вдалося розпарсити суму: {raw_amount!r}")
    else:
        result["errors"].append("Не знайдено Сума документу")

    # Payment date — exact "Дата платежу" first, then fallback
    m = re.search(r"Дата\s+платежу[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})", text, re.IGNORECASE | re.UNICODE)
    if not m:
        m = re.search(r"Дата[^\n:]*[:\s]+(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})", text, re.IGNORECASE | re.UNICODE)
    if m:
        raw_date = m.group(1).replace("-", ".").replace("/", ".")
        parts = raw_date.split(".")
        if len(parts) == 3:
            day, month, year = parts
            if len(year) == 2:
                year = "20" + year
            result["payment_date"] = f"{day.zfill(2)}.{month.zfill(2)}.{year}"
            try:
                dt = datetime.strptime(result["payment_date"], "%d.%m.%Y")
                result["fact_column"] = f"Fact_{dt.month:02d}_{str(dt.year)[2:]}"
            except ValueError:
                result["errors"].append(f"Некоректна дата: {raw_date!r}")
    else:
        result["errors"].append("Не знайдено Дата платежу")

    return result


# ── Spreadsheet helpers ───────────────────────────────────────────────────────

def _get_sheet_values(service, spreadsheet_id: str, range_: str) -> list[list]:
    """Fetch a range of values from a sheet. Returns [] on error."""
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_,
        ).execute()
        return resp.get("values", [])
    except Exception as e:
        logger.error(f"_get_sheet_values failed ({spreadsheet_id} / {range_}): {e}")
        return []


def _get_cell_formula(service, spreadsheet_id: str, cell_address: str) -> str:
    """Read a single cell and return its raw formula (or plain value if no formula)."""
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=cell_address,
            valueRenderOption="FORMULA",
        ).execute()
        vals = resp.get("values", [])
        if vals and vals[0]:
            return str(vals[0][0])
    except Exception as e:
        logger.warning(f"_get_cell_formula failed ({cell_address}): {e}")
    return ""

def _find_row_containing(rows: list[list], col_indices: list[int], keyword: str) -> int | None:
    """
    Return the 0-based row index of the first row where any of the specified
    column indices contain `keyword` (case-insensitive substring match).
    """
    kw = keyword.lower().strip()
    for i, row in enumerate(rows):
        for ci in col_indices:
            cell = row[ci].strip().lower() if ci < len(row) and row[ci] else ""
            if kw in cell:
                return i
    return None


# ── Step 1: find project row in "general" sheet ───────────────────────────────

def find_project_budget_row(project_1c_name: str) -> dict:
    """
    Search the "general" sheet for a row where column C contains project_1c_name.

    Returns:
        {
          "found": bool,
          "row_index": int (0-based),
          "project_sheet_name": str,   # col B of found row
          "spreadsheet_id": str,       # always MASTER_SPREADSHEET_ID
          "row_values": list,
        }
    """
    service = _get_sheets_service()
    if not service:
        return {"found": False, "error": "Sheets service недоступний"}

    rows = _get_sheet_values(service, MASTER_SPREADSHEET_ID, f"{GENERAL_SHEET_NAME}!A:E")
    if not rows:
        return {"found": False, "error": f"Лист '{GENERAL_SHEET_NAME}' порожній або недоступний"}

    # col C = index 2 (0-based)
    row_idx = _find_row_containing(rows, [2], project_1c_name)
    if row_idx is None:
        return {
            "found": False,
            "project_1c_name": project_1c_name,
            "error": f"Проєкт '{project_1c_name}' не знайдено в листі '{GENERAL_SHEET_NAME}'",
            "hint": "Перевір назву в 1С — вона має точно збігатися зі значенням у стовпці C таблиці general.",
        }

    row = rows[row_idx]
    project_sheet_name = row[1].strip() if len(row) > 1 else ""
    # Always use master spreadsheet — all project budgets are tabs in the same file
    spreadsheet_id = MASTER_SPREADSHEET_ID
    import re as _re

    return {
        "found": True,
        "row_index": row_idx,
        "project_sheet_name": project_sheet_name,
        "spreadsheet_id": spreadsheet_id,
        "row_values": row,
    }


# ── Step 2: find expense row and fact column in project sheet ─────────────────

def _resolve_sheet_name(service, spreadsheet_id: str, sheet_name_hint: str) -> str | None:
    """
    Find the real tab name in the spreadsheet that best matches sheet_name_hint.
    Tries exact match first, then 'contains' (case-insensitive).
    Returns the real tab name or None.
    """
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        ).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception as e:
        logger.error(f"_resolve_sheet_name failed: {e}")
        return None

    hint = sheet_name_hint.strip().lower()

    # 1. Exact match
    for tab in tabs:
        if tab.strip().lower() == hint:
            return tab

    # 2. Contains match (e.g. hint="ESG", tab="4 ESG")
    for tab in tabs:
        if hint in tab.strip().lower():
            return tab

    # 3. Tab name contains hint as a word
    for tab in tabs:
        if hint in tab.lower().split():
            return tab

    logger.warning(f"No tab matching '{sheet_name_hint}' in {tabs}")
    return None


def find_expense_row(
    spreadsheet_id: str,
    sheet_name: str,
    counterparty_surname: str,
    fact_column: str,
) -> dict:
    """
    In the project-specific sheet, find the expense row for this counterparty
    and the correct Fact_MM_YY column.

    - If the counterparty appears more than once, uses the FIRST match only.
    - Returns the existing cell value so the caller can decide whether to
      write a plain number or an additive formula (=old+new).

    Returns:
        {
          "found": bool,
          "row_index": int,           # 0-based data row
          "col_index": int,           # 0-based column index of fact_column
          "cell_address": str,        # e.g. "'4 ESG'!V12" (A1 notation)
          "existing_value": float or None,
          "existing_raw": str,        # raw cell content (may be a formula)
          "sheet_row_1based": int,    # for Sheets API (1-based)
          "resolved_sheet_name": str, # actual tab name used
        }
    """
    service = _get_sheets_service()
    if not service:
        return {"found": False, "error": "Sheets service недоступний"}

    # Resolve fuzzy sheet name (e.g. "ESG" → "4 ESG")
    resolved_name = _resolve_sheet_name(service, spreadsheet_id, sheet_name)
    if not resolved_name:
        # List available tabs for user-facing error
        try:
            meta = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties.title",
            ).execute()
            tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        except Exception:
            tabs = []
        return {
            "found": False,
            "error": f"Лист '{sheet_name}' не знайдено у файлі",
            "available_tabs": tabs,
        }

    # Read entire sheet using resolved name
    rows = _get_sheet_values(service, spreadsheet_id, f"'{resolved_name}'!A:AZ")
    if not rows:
        return {"found": False, "error": f"Лист '{resolved_name}' порожній або недоступний"}

    # ── Find header row (contains fact_column header) ─────────────────────────
    header_row_idx = None
    col_idx = None
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            if cell and cell.strip().lower() == fact_column.lower():
                header_row_idx = i
                col_idx = j
                break
        if col_idx is not None:
            break

    if col_idx is None:
        return {
            "found": False,
            "error": f"Стовпець '{fact_column}' не знайдено в листі '{sheet_name}'",
            "hint": f"Очікується заголовок '{fact_column}' в першому рядку-заголовку таблиці.",
        }

    # ── Find expense row (search cols B=1 and C=2 for counterparty surname) ──
    data_rows = rows[header_row_idx + 1:] if header_row_idx is not None else rows[1:]
    data_start = (header_row_idx + 1) if header_row_idx is not None else 1
    row_offset = _find_row_containing(data_rows, [1, 2], counterparty_surname)

    if row_offset is None:
        return {
            "found": False,
            "error": f"Контрагент '{counterparty_surname}' не знайдено в листі '{sheet_name}'",
            "hint": "Перевір прізвище — воно має бути в стовпці B або C таблиці.",
        }

    data_row_idx = data_start + row_offset
    expense_row = rows[data_row_idx]

    # Get existing cell value (raw string — may be a plain number or formula)
    existing_raw = expense_row[col_idx].strip() if col_idx < len(expense_row) else ""
    existing_value: float | None = None
    if existing_raw:
        raw_for_parse = existing_raw.lstrip("=")
        clean = re.sub(r"[\s\xa0\u202f]", "", raw_for_parse).replace(",", ".")
        try:
            existing_value = float(clean)
        except ValueError:
            try:
                existing_value = float(eval(clean, {"__builtins__": {}}))
            except Exception:
                existing_value = None

    # Build A1 address using the RESOLVED sheet name (handles "4 ESG" vs "ESG")
    col_letter = _col_index_to_letter(col_idx)
    sheet_row_1based = data_row_idx + 1  # sheets are 1-indexed
    cell_address = f"'{resolved_name}'!{col_letter}{sheet_row_1based}"

    return {
        "found": True,
        "row_index": data_row_idx,
        "col_index": col_idx,
        "cell_address": cell_address,
        "existing_value": existing_value,
        "existing_raw": existing_raw,
        "sheet_row_1based": sheet_row_1based,
        "resolved_sheet_name": resolved_name,
        "counterparty_row_values": expense_row[:10],
    }


def _col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to A1-notation letter(s). 0→A, 25→Z, 26→AA."""
    result = ""
    idx += 1  # make 1-based
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


# ── Step 3: write payment to cell ─────────────────────────────────────────────

def write_payment_to_cell(
    spreadsheet_id: str,
    cell_address: str,
    amount: float,
    existing_value: float | None,
    existing_raw: str = "",
) -> dict:
    """
    Write payment amount to cell.
    - Reads the actual cell formula first (valueRenderOption=FORMULA).
    - If cell is empty → write plain number.
    - If cell has any content → append +amount to whatever is there.
      Result is always a formula, e.g. =2440+1500 or =A1*0.2+1500.
    """
    service = _get_sheets_service()
    if not service:
        return {"ok": False, "error": "Sheets service недоступний"}

    # Always re-read the cell with FORMULA render to get exact current content
    current = _get_cell_formula(service, spreadsheet_id, cell_address).strip()

    amount_str = str(int(amount)) if amount == int(amount) else str(amount)

    if current:
        # Append to existing content (plain number or formula)
        base = current.lstrip("=")
        cell_value = f"={base}+{amount_str}"
        action = "added"
    else:
        # Empty cell — write plain number
        cell_value = int(amount) if amount == int(amount) else amount
        action = "written"

    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_address,
            valueInputOption="USER_ENTERED",
            body={"values": [[cell_value]]},
        ).execute()
        return {"ok": True, "cell_value_written": str(cell_value), "cell": cell_address, "action": action}
    except Exception as e:
        logger.error(f"write_payment_to_cell failed ({cell_address}): {e}")
        return {"ok": False, "error": str(e), "cell": cell_address}


# ── Main entry point: full record_payment flow ────────────────────────────────

def record_payment(
    project_1c_name: str,
    counterparty_surname: str,
    amount: float,
    payment_date: str,
) -> dict:
    """
    Full payment recording flow (mirrors Make blueprint):
      1. Find project row in "general" sheet → get project_sheet_name
      2. Find expense row in project sheet by counterparty_surname
      3. Find Fact_MM_YY column from payment_date
      4. Write or add amount

    Args:
        project_1c_name:      Project name as it appears in 1C (col C of general sheet)
        counterparty_surname: Counterparty surname (first word, used to find expense row)
        amount:               Payment amount (float)
        payment_date:         Date in format DD.MM.YYYY

    Returns result dict with all intermediate steps and final status.
    """
    result: dict = {
        "project_1c_name": project_1c_name,
        "counterparty_surname": counterparty_surname,
        "amount": amount,
        "payment_date": payment_date,
    }

    # Derive fact_column from date
    try:
        dt = datetime.strptime(payment_date, "%d.%m.%Y")
        fact_column = f"Fact_{dt.month:02d}_{str(dt.year)[2:]}"
    except ValueError:
        return {**result, "ok": False, "error": f"Некоректна дата: {payment_date!r}"}

    result["fact_column"] = fact_column

    # Step 1: find project in general sheet
    proj_lookup = find_project_budget_row(project_1c_name)
    result["project_lookup"] = proj_lookup
    if not proj_lookup.get("found"):
        return {**result, "ok": False, "step_failed": "project_lookup"}

    project_sheet_name = proj_lookup["project_sheet_name"]
    spreadsheet_id = proj_lookup["spreadsheet_id"]
    result["project_sheet_name"] = project_sheet_name
    result["spreadsheet_id"] = spreadsheet_id

    # Step 2: find expense row + fact column
    expense_lookup = find_expense_row(
        spreadsheet_id, project_sheet_name, counterparty_surname, fact_column
    )
    result["expense_lookup"] = expense_lookup
    if not expense_lookup.get("found"):
        return {**result, "ok": False, "step_failed": "expense_lookup"}

    # Step 3: write payment
    write_result = write_payment_to_cell(
        spreadsheet_id,
        expense_lookup["cell_address"],
        amount,
        expense_lookup["existing_value"],
        expense_lookup.get("existing_raw", ""),
    )
    result["write_result"] = write_result
    result["ok"] = write_result.get("ok", False)

    return result


# ── Diagnostic: show raw general-sheet row ────────────────────────────────────

def debug_budget_lookup(project_1c_name: str) -> dict:
    """
    Show everything found in the 'general' sheet for this project —
    all column values, row number, and what spreadsheet_id was resolved.
    Use this when record_payment fails to find the project sheet.
    """
    service = _get_sheets_service()
    if not service:
        return {"error": "Sheets service недоступний"}

    # Read up to column J to capture all possible ID columns
    rows = _get_sheet_values(service, MASTER_SPREADSHEET_ID, f"{GENERAL_SHEET_NAME}!A:J")
    if not rows:
        return {"error": f"Лист '{GENERAL_SHEET_NAME}' порожній або недоступний"}

    kw = project_1c_name.lower().strip()
    matches = []
    for i, row in enumerate(rows):
        row_text = " | ".join(str(c) for c in row)
        if kw in row_text.lower():
            matches.append({
                "row_1based": i + 1,
                "columns": {chr(65 + j): row[j] for j in range(len(row))},
                "raw": row,
            })

    if not matches:
        return {
            "found": False,
            "project_1c_name": project_1c_name,
            "total_rows_scanned": len(rows),
            "hint": "Проєкт не знайдено. Перевір точну назву в 1С — вона має бути в колонці C листа 'general'.",
        }

    # Also try to list sheet names in master spreadsheet
    sheet_names = []
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=MASTER_SPREADSHEET_ID,
            fields="sheets.properties.title",
        ).execute()
        sheet_names = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:
        pass

    return {
        "found": True,
        "matches": matches,
        "master_spreadsheet_id": MASTER_SPREADSHEET_ID,
        "sheets_in_master_file": sheet_names,
        "hint": (
            "Колонка B = назва листа проєкту, колонка D = ID окремого файлу бюджету (якщо є). "
            "Якщо колонка D порожня — бот шукає лист у master-файлі."
        ),
    }


# ── Currency helpers ──────────────────────────────────────────────────────────

def get_nbu_exchange_rate(payment_date: str, currency: str = "USD") -> dict:
    """
    Fetch the official NBU exchange rate for a given date from bank.gov.ua.

    Args:
        payment_date: Date in DD.MM.YYYY format (e.g. "15.03.2026")
        currency:     Currency code, e.g. "USD"

    Returns:
        {"ok": True, "rate": float, "date": "YYYYMMDD", "currency": "USD"}
        or {"ok": False, "error": "..."}
    """
    # Convert DD.MM.YYYY → YYYYMMDD for NBU API
    try:
        dt = datetime.strptime(payment_date, "%d.%m.%Y")
        date_str = dt.strftime("%Y%m%d")
    except ValueError:
        return {"ok": False, "error": f"Некоректна дата: {payment_date!r}"}

    url = (
        f"https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
        f"?valcode={currency.upper()}&date={date_str}&json"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data and isinstance(data, list) and "rate" in data[0]:
            rate = float(data[0]["rate"])
            return {
                "ok": True,
                "rate": rate,
                "date": date_str,
                "currency": currency.upper(),
                "source": "NBU",
            }
        return {"ok": False, "error": f"НБУ не повернув курс для {currency} на {payment_date}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Помилка з'єднання з НБУ: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"Не вдалося отримати курс: {e}"}


def get_table_exchange_rate(project_1c_name: str) -> dict:
    """
    Read the exchange rate ("Курс за НБУ") stored in the project budget sheet.
    Looks in the project sheet (found via general sheet) for a cell in col A or B
    that contains "Курс" or "курс", and returns the value from the adjacent cell.

    Returns:
        {"ok": True, "rate": float, "cell_address": "...", "source": "table"}
        or {"ok": False, "error": "..."}
    """
    # First resolve the project sheet
    proj = find_project_budget_row(project_1c_name)
    if not proj.get("found"):
        return {"ok": False, "error": proj.get("error", "Проєкт не знайдено")}

    spreadsheet_id = proj["spreadsheet_id"]
    sheet_name = proj["project_sheet_name"]

    service = _get_sheets_service()
    if not service:
        return {"ok": False, "error": "Sheets service недоступний"}

    resolved_name = _resolve_sheet_name(service, spreadsheet_id, sheet_name)
    if not resolved_name:
        return {"ok": False, "error": f"Лист '{sheet_name}' не знайдено"}

    # Read cols A:C of the project sheet
    rows = _get_sheet_values(service, spreadsheet_id, f"'{resolved_name}'!A:C")
    if not rows:
        return {"ok": False, "error": f"Лист '{resolved_name}' порожній"}

    for i, row in enumerate(rows):
        for ci in range(min(2, len(row))):  # check cols A and B
            cell_val = row[ci].strip() if row[ci] else ""
            if re.search(r"курс", cell_val, re.IGNORECASE):
                # Adjacent cell: if col A matched → col B (index 1); if col B → col C (index 2)
                adjacent_idx = ci + 1
                adjacent_val = row[adjacent_idx].strip() if adjacent_idx < len(row) and row[adjacent_idx] else ""
                if adjacent_val:
                    clean = re.sub(r"[\s\xa0]", "", adjacent_val).replace(",", ".")
                    try:
                        rate = float(clean)
                        col_letter = _col_index_to_letter(adjacent_idx)
                        return {
                            "ok": True,
                            "rate": rate,
                            "cell_address": f"'{resolved_name}'!{col_letter}{i+1}",
                            "label_found": cell_val,
                            "source": "table",
                        }
                    except ValueError:
                        continue

    return {
        "ok": False,
        "error": f"Поле 'Курс за НБУ' не знайдено в листі '{resolved_name}'",
        "hint": "Перевір що в колонці A або B є рядок зі словом 'Курс' і значенням поруч.",
    }


# ── Combined parse + auto rate resolution ────────────────────────────────────

def parse_payment_with_rate(text: str) -> dict:
    """
    Parse a payment notification message.
    For UAH: returns amount_uah == amount directly.
    For USD: returns amount_usd and needs_rate_choice=True so the agent
             can ask the user which source to use (table vs NBU),
             then call get_table_exchange_rate or get_nbu_exchange_rate separately.

    Returns parse_payment_message result enriched with:
        needs_rate_choice  bool   — True when currency is USD and rate must be fetched
        amount_uah         float | None  — filled only for UAH payments
    """
    result = parse_payment_message(text)

    if result.get("currency") != "USD":
        result["needs_rate_choice"] = False
        result["amount_uah"] = result.get("amount")
    else:
        result["needs_rate_choice"] = True
        result["amount_uah"] = None  # will be calculated after user chooses rate source

    return result


# ── Tool definitions for Claude agent ─────────────────────────────────────────

BUDGET_TOOL_DEFINITIONS = [
    {
        "name": "debug_budget_lookup",
        "description": (
            "Show the raw row data from the 'general' sheet for a given project 1C name. "
            "Use this when record_payment fails with 'лист порожній або недоступний' — "
            "it reveals all column values and available sheet names so you can diagnose "
            "whether the project sheet is in a separate spreadsheet file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_1c_name": {
                    "type": "string",
                    "description": "Project 1C name to look up in the general sheet"
                }
            },
            "required": ["project_1c_name"],
        },
    },
    {
        "name": "parse_payment_with_rate",
        "description": (
            "Parse a 1C payment notification and automatically resolve the exchange rate. "
            "Extracts: counterparty, project 1C name, amount, currency, date, fact column. "
            "If currency is USD: tries to find the rate in the project budget sheet first, "
            "then falls back to the NBU API for the payment date. "
            "Returns rate_uah, amount_uah, and rate_source ('UAH'|'table'|'nbu'|'not_found'). "
            "Use this as the ONLY entry point when manager sends a payment notification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The full text of the payment notification message"
                }
            },
            "required": ["text"],
        },
    },
    {
        "name": "record_payment",
        "description": (
            "Record a payment into the budget spreadsheet. "
            "Finds the project row in the 'general' index sheet, then finds the "
            "counterparty expense row in the project-specific sheet, then writes "
            "or adds the amount to the correct Fact_MM_YY cell. "
            "Always show parsed data to the manager and ask for confirmation before calling this. "
            "The 'amount' must be in UAH — convert from USD first if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_1c_name": {
                    "type": "string",
                    "description": "Project name exactly as in 1C (used to search in 'general' sheet col C)"
                },
                "counterparty_surname": {
                    "type": "string",
                    "description": "Counterparty surname (first word) for finding the expense row"
                },
                "amount": {
                    "type": "number",
                    "description": "Payment amount in UAH (float). If original was in USD, convert first."
                },
                "payment_date": {
                    "type": "string",
                    "description": "Payment date in format DD.MM.YYYY (e.g. '15.03.2026')"
                },
            },
            "required": ["project_1c_name", "counterparty_surname", "amount", "payment_date"],
        },
    },
    {
        "name": "get_nbu_exchange_rate",
        "description": (
            "Fetch the official NBU (National Bank of Ukraine) exchange rate for a specific date. "
            "Use when the payment is in USD and the manager chose to use the NBU rate for the payment date. "
            "Returns the UAH/USD rate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_date": {
                    "type": "string",
                    "description": "Payment date in DD.MM.YYYY format (e.g. '15.03.2026')"
                },
                "currency": {
                    "type": "string",
                    "description": "Currency code, e.g. 'USD'",
                    "default": "USD",
                },
            },
            "required": ["payment_date"],
        },
    },
    {
        "name": "get_table_exchange_rate",
        "description": (
            "Read the exchange rate stored in the project budget sheet (column A or B, "
            "cell labeled 'Курс за НБУ' or similar). "
            "Use when the payment is in USD and the manager chose to use the rate from the spreadsheet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_1c_name": {
                    "type": "string",
                    "description": "Project 1C name (used to find the project sheet)"
                },
            },
            "required": ["project_1c_name"],
        },
    },
]


def execute_budget_tool(name: str, inputs: dict) -> str:
    try:
        if name == "debug_budget_lookup":
            result = debug_budget_lookup(**inputs)
        elif name == "parse_payment_with_rate":
            result = parse_payment_with_rate(**inputs)
        elif name == "parse_payment_message":
            result = parse_payment_message(**inputs)  # internal, kept for direct use
        elif name == "record_payment":
            result = record_payment(**inputs)
        elif name == "get_nbu_exchange_rate":
            result = get_nbu_exchange_rate(**inputs)
        elif name == "get_table_exchange_rate":
            result = get_table_exchange_rate(**inputs)
        else:
            result = {"error": f"Unknown budget tool: {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"execute_budget_tool error ({name}): {e}", exc_info=True)
        return json.dumps({"error": str(e)})
