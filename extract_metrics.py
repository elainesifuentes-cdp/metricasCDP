#!/usr/bin/env python3
"""
extract_metrics.py

Extrae métricas semanales del sprint activo de Jira Cloud (proyecto CDPE)
y las escribe como una nueva fila en la hoja "Datos Brutos" de un Google Sheet.

Métricas extraídas:
  - Puntos completados (story points de issues Done en el sprint activo)
  - Items completados (count)
  - Items en progreso (count)
  - Items bloqueados (count + detalle de razones)
  - PTO (placeholder manual por ahora)

Variables de entorno requeridas (se configuran como GitHub Secrets):
  JIRA_DOMAIN            ej: centraldepasajes.atlassian.net
  JIRA_EMAIL             email de Atlassian usado para autenticar
  JIRA_API_TOKEN         API token generado en id.atlassian.com
  JIRA_PROJECT_KEY       ej: CDPE
  GOOGLE_SHEETS_ID       ID del Google Sheet (de la URL)
  GOOGLE_CREDENTIALS_JSON  contenido completo del JSON de la Service Account
"""

import os
import sys
import json
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials


# ---------------------------------------------------------------------------
# Config desde variables de entorno
# ---------------------------------------------------------------------------

def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: falta la variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return value


JIRA_DOMAIN = get_env("JIRA_DOMAIN")
JIRA_EMAIL = get_env("JIRA_EMAIL")
JIRA_API_TOKEN = get_env("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = get_env("JIRA_PROJECT_KEY")
GOOGLE_SHEETS_ID = get_env("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = get_env("GOOGLE_CREDENTIALS_JSON")

# Nombre de la hoja donde el script escribe cada fila
RAW_SHEET_NAME = "Datos Brutos"

# Status que se consideran "bloqueado" en el workflow de Jira
BLOCKED_STATUSES = {"Bloqueado"}

# Categorías de status de Jira que cuentan como "Done"
DONE_STATUS_CATEGORY = "Done"
IN_PROGRESS_STATUS_CATEGORY = "In Progress"

JIRA_BASE_URL = f"https://{JIRA_DOMAIN}"


# ---------------------------------------------------------------------------
# Jira: obtener issues del sprint activo
# ---------------------------------------------------------------------------

def jira_search(jql: str, fields: list[str]) -> list[dict]:
    """
    Pagina el endpoint 'enhanced JQL search' (POST /rest/api/3/search/jql) y
    devuelve todos los issues que matchean el JQL.

    NOTA: el endpoint viejo (GET /rest/api/3/search) fue deprecado por
    Atlassian y devuelve 410 Gone. Este usa el reemplazo oficial, que pagina
    con nextPageToken en vez de startAt/total.
    """
    issues = []
    next_page_token = None

    while True:
        body = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token or data.get("isLast", True):
            break

    return issues


def find_story_points_field(issue_fields: dict) -> float:
    """
    El campo de Story Points en Jira Cloud es un custom field cuyo ID varía
    por instancia (normalmente customfield_10016, pero no es fijo).
    Buscamos el primer customfield numérico razonable como fallback si
    JIRA_STORY_POINTS_FIELD no está seteado explícitamente.
    """
    explicit_field = os.environ.get("JIRA_STORY_POINTS_FIELD")
    if explicit_field and explicit_field in issue_fields:
        value = issue_fields.get(explicit_field)
        return float(value) if value is not None else 0.0

    for key, value in issue_fields.items():
        if key.startswith("customfield_") and isinstance(value, (int, float)):
            return float(value)

    return 0.0


def get_sprint_metrics() -> dict:
    # Traemos todos los campos para poder detectar story points sin conocer el ID exacto
    jql = f'project = {JIRA_PROJECT_KEY} AND sprint in openSprints() ORDER BY updated DESC'
    issues = jira_search(jql, fields=["*all"])

    completed_points = 0.0
    completed_count = 0
    in_progress_count = 0
    blocked_count = 0
    blocked_details = []

    for issue in issues:
        key = issue.get("key")
        f = issue.get("fields", {})
        status = f.get("status", {})
        status_name = status.get("name", "")
        status_category = status.get("statusCategory", {}).get("name", "")
        points = find_story_points_field(f)

        if status_name in BLOCKED_STATUSES:
            blocked_count += 1
            summary = f.get("summary", "")
            blocked_details.append(f"{key}: {summary} (status: {status_name})")
        elif status_category == DONE_STATUS_CATEGORY:
            completed_count += 1
            completed_points += points
        elif status_category == IN_PROGRESS_STATUS_CATEGORY:
            in_progress_count += 1

    return {
        "completed_points": completed_points,
        "completed_count": completed_count,
        "in_progress_count": in_progress_count,
        "blocked_count": blocked_count,
        "blocked_details": "; ".join(blocked_details) if blocked_details else "",
    }


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def write_to_sheet(row: list) -> None:
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(credentials)

    sheet = client.open_by_key(GOOGLE_SHEETS_ID)
    try:
        worksheet = sheet.worksheet(RAW_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=RAW_SHEET_NAME, rows=1000, cols=10)
        worksheet.append_row(
            ["Timestamp", "Puntos completados", "Items completados",
             "En progreso", "Bloqueados", "Detalles", "PTO", "Notas"]
        )

    worksheet.append_row(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Extrayendo métricas de {JIRA_PROJECT_KEY} en {JIRA_DOMAIN}...")
    metrics = get_sprint_metrics()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # PTO: placeholder manual hasta integrar una fuente real
    pto = os.environ.get("PTO_MANUAL", "")

    row = [
        timestamp,
        metrics["completed_points"],
        metrics["completed_count"],
        metrics["in_progress_count"],
        metrics["blocked_count"],
        metrics["blocked_details"],
        pto,
        "",
    ]

    print("Fila a escribir:", row)
    write_to_sheet(row)
    print("Listo. Fila agregada a la hoja 'Datos Brutos'.")


if __name__ == "__main__":
    main()
