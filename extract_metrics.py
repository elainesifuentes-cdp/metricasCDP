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

Variables opcionales:
  JIRA_SPRINT_ID        ID numérico de un sprint concreto de Jira. Si se define
                        (o se pasa --sprint-id), se extraen las métricas de ese
                        sprint en vez del sprint activo.

Uso:
  python extract_metrics.py                 # sprint activo (openSprints)
  python extract_metrics.py --sprint-id 42  # sprint puntual por ID
  python extract_metrics.py --list-sprints  # lista los sprints del proyecto y sus IDs
"""

import os
import sys
import json
import argparse
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

# Claves de categoría de status de Jira (independientes del idioma de la cuenta).
# statusCategory.name cambia con el locale ("Done"/"Finalizada", etc.), pero
# statusCategory.key siempre es "new" / "indeterminate" / "done".
DONE_CATEGORY_KEY = "done"
IN_PROGRESS_CATEGORY_KEY = "indeterminate"

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


def get_sprint_name(sprint_id: str) -> str:
    """Devuelve el nombre del sprint a partir de su ID (API Agile de Jira)."""
    try:
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}",
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("name", f"Sprint {sprint_id}")
    except requests.RequestException as exc:
        print(f"AVISO: no se pudo resolver el nombre del sprint {sprint_id}: {exc}",
              file=sys.stderr)
        return f"Sprint {sprint_id}"


def list_project_sprints() -> None:
    """Imprime los sprints (id, estado, nombre) de los boards del proyecto."""
    boards_resp = requests.get(
        f"{JIRA_BASE_URL}/rest/agile/1.0/board",
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        params={"projectKeyOrId": JIRA_PROJECT_KEY},
        timeout=30,
    )
    boards_resp.raise_for_status()
    boards = boards_resp.json().get("values", [])
    if not boards:
        print(f"No se encontraron boards para el proyecto {JIRA_PROJECT_KEY}.")
        return

    for board in boards:
        board_id = board.get("id")
        print(f"\nBoard {board_id} - {board.get('name', '')}")
        start_at = 0
        while True:
            resp = requests.get(
                f"{JIRA_BASE_URL}/rest/agile/1.0/board/{board_id}/sprint",
                auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                headers={"Accept": "application/json"},
                params={"startAt": start_at, "maxResults": 50},
                timeout=30,
            )
            if resp.status_code == 400:
                print("  (el board no soporta sprints)")
                break
            resp.raise_for_status()
            data = resp.json()
            for sprint in data.get("values", []):
                print(f"  id={sprint.get('id'):<8} {sprint.get('state', ''):<8} "
                      f"{sprint.get('name', '')}")
            if data.get("isLast", True):
                break
            start_at += len(data.get("values", []))


def get_sprint_metrics(sprint_id: str | None = None) -> dict:
    # Traemos todos los campos para poder detectar story points sin conocer el ID exacto
    if sprint_id:
        jql = (f'project = {JIRA_PROJECT_KEY} AND sprint = {sprint_id} '
               f'ORDER BY updated DESC')
        sprint_name = get_sprint_name(sprint_id)
    else:
        jql = (f'project = {JIRA_PROJECT_KEY} AND sprint in openSprints() '
               f'ORDER BY updated DESC')
        sprint_name = ""
    print(f"JQL: {jql}")
    issues = jira_search(jql, fields=["*all"])
    print(f"Issues encontrados: {len(issues)}")

    # Diagnóstico: si no hubo resultados con un sprint explícito, probamos sin
    # el filtro de proyecto para ver si los issues del sprint son de otro proyecto.
    if not issues and sprint_id:
        alt_jql = f'sprint = {sprint_id} ORDER BY updated DESC'
        alt_issues = jira_search(alt_jql, fields=["project", "status"])
        print(f"DEBUG sin filtro de proyecto ('{alt_jql}'): {len(alt_issues)} issues")
        proyectos = {}
        for it in alt_issues:
            pk = it.get("fields", {}).get("project", {}).get("key", "?")
            proyectos[pk] = proyectos.get(pk, 0) + 1
        print(f"DEBUG proyectos en el sprint: {proyectos}")

    completed_points = 0.0
    completed_count = 0
    in_progress_count = 0
    blocked_count = 0
    blocked_details = []
    status_breakdown: dict[str, int] = {}

    for idx, issue in enumerate(issues):
        key = issue.get("key")
        f = issue.get("fields", {})

        if idx == 0:
            print(f"DEBUG primer issue {key}: status={json.dumps(f.get('status'), ensure_ascii=False)}")
            print(f"DEBUG campos disponibles: {sorted(f.keys())}")

        # Si no se pasó un sprint explícito, intentamos deducir el nombre del
        # sprint activo desde el campo "sprint" de los issues (customfield con
        # una lista de objetos sprint).
        if not sprint_name:
            for value in f.values():
                if isinstance(value, list) and value and isinstance(value[0], dict) \
                        and "state" in value[0] and "name" in value[0]:
                    active = [s for s in value if s.get("state") == "active"]
                    chosen = active[0] if active else value[-1]
                    sprint_name = chosen.get("name", "")
                    break

        status = f.get("status", {})
        status_name = status.get("name", "")
        status_category = status.get("statusCategory", {}).get("key", "")
        points = find_story_points_field(f)

        status_breakdown[status_name] = status_breakdown.get(status_name, 0) + 1

        if status_name in BLOCKED_STATUSES:
            blocked_count += 1
            summary = f.get("summary", "")
            blocked_details.append(f"{key}: {summary} (status: {status_name})")
        elif status_category == DONE_CATEGORY_KEY:
            completed_count += 1
            completed_points += points
        elif status_category == IN_PROGRESS_CATEGORY_KEY:
            in_progress_count += 1

    print("Desglose por status:", status_breakdown)

    return {
        "sprint_name": sprint_name,
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
             "En progreso", "Bloqueados", "Detalles", "PTO", "Notas", "Sprint"]
        )

    worksheet.append_row(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sprint-id", default=os.environ.get("JIRA_SPRINT_ID"),
                        help="ID numérico de un sprint concreto de Jira. "
                             "Por defecto usa el sprint activo (openSprints).")
    parser.add_argument("--list-sprints", action="store_true",
                        help="Lista los sprints del proyecto con sus IDs y sale.")
    args = parser.parse_args()

    if args.list_sprints:
        list_project_sprints()
        return

    sprint_id = args.sprint_id or None
    if sprint_id:
        print(f"Extrayendo métricas del sprint {sprint_id} "
              f"de {JIRA_PROJECT_KEY} en {JIRA_DOMAIN}...")
    else:
        print(f"Extrayendo métricas del sprint activo de "
              f"{JIRA_PROJECT_KEY} en {JIRA_DOMAIN}...")

    metrics = get_sprint_metrics(sprint_id)

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
        metrics["sprint_name"],
    ]

    print("Fila a escribir:", row)
    write_to_sheet(row)
    print("Listo. Fila agregada a la hoja 'Datos Brutos'.")


if __name__ == "__main__":
    main()
