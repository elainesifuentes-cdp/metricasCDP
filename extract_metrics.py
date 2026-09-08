#!/usr/bin/env python3
"""
extract_metrics.py

Extrae métricas semanales del sprint activo de Jira Cloud (proyecto CDPE)
y las escribe como una nueva fila en la hoja "Datos Brutos" de un Google Sheet.

Métricas extraídas:
  - Horas completadas (suma de estimación original de issues Done en el sprint)
  - Items completados (count)
  - Items en progreso (count)
  - Items bloqueados (count + detalle de razones)
  - PTO (placeholder manual por ahora)

Hojas que escribe en el Google Sheet:
  - "Datos Brutos"       : una fila por corrida con el resumen del sprint
  - "Velocidad por Dev"  : una fila por (corrida, desarrollador) con horas
                           planificadas vs completadas y varianza
  - "Tareas sin tiempo"  : una fila por issue del sprint al que le falta la
                           estimación original o el registro de tiempo trabajado

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

# Nombres de las hojas donde el script escribe
RAW_SHEET_NAME = "Datos Brutos"
PER_DEV_SHEET_NAME = "Velocidad por Dev"
MISSING_TIMES_SHEET_NAME = "Tareas sin tiempo"

# Status que se consideran "bloqueado" en el workflow de Jira
BLOCKED_STATUSES = {"Bloqueado"}

# Claves de categoría de status de Jira (independientes del idioma de la cuenta).
# statusCategory.name cambia con el locale ("Done"/"Finalizada", etc.), pero
# statusCategory.key siempre es "new" / "indeterminate" / "done".
DONE_CATEGORY_KEY = "done"
IN_PROGRESS_CATEGORY_KEY = "indeterminate"

# Issues que caen en categoría "done" pero NO representan trabajo entregado
# (cancelados / descartados). Se excluyen del conteo de completados.
# Se compara contra el nombre del status Y contra el de la resolución.
# Configurable con la env var EXCLUDED_STATUSES (lista separada por comas).
_DEFAULT_EXCLUDED_DONE = [
    "won't do", "wont do", "no se hará", "no se hara", "no se realizará",
    "cancelado", "cancelada", "descartado", "descartada",
    "rechazado", "rechazada", "duplicado", "duplicada", "declined",
]
EXCLUDED_DONE_STATUSES = {
    s.strip().lower()
    for s in os.environ.get("EXCLUDED_STATUSES", ",".join(_DEFAULT_EXCLUDED_DONE)).split(",")
    if s.strip()
}

# Tipos de issue que NO se listan como "tarea sin tiempo" (los épicas nunca
# llevan estimación). Configurable con MISSING_TIMES_EXCLUDE_TYPES.
MISSING_TIMES_EXCLUDE_TYPES = {
    s.strip().lower()
    for s in os.environ.get("MISSING_TIMES_EXCLUDE_TYPES", "epic,épica,epica").split(",")
    if s.strip()
}

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


def get_original_estimate_hours(issue_fields: dict) -> float:
    """
    Devuelve la estimación original del issue en horas.

    En Jira Cloud la estimación original es el campo estándar
    `timeoriginalestimate` (en segundos). Como fallback se usa
    `timetracking.originalEstimateSeconds`.
    """
    seconds = issue_fields.get("timeoriginalestimate")
    if not isinstance(seconds, (int, float)):
        seconds = issue_fields.get("timetracking", {}).get("originalEstimateSeconds")
    if not isinstance(seconds, (int, float)):
        return 0.0
    return round(seconds / 3600, 2)


def get_time_spent_hours(issue_fields: dict) -> float:
    """Tiempo trabajado registrado (worklogs) en horas. 0 si no hay registro."""
    seconds = issue_fields.get("timespent")
    if not isinstance(seconds, (int, float)):
        seconds = issue_fields.get("timetracking", {}).get("timeSpentSeconds")
    if not isinstance(seconds, (int, float)):
        return 0.0
    return round(seconds / 3600, 2)


def get_assignee_name(issue_fields: dict) -> str:
    assignee = issue_fields.get("assignee") or {}
    return assignee.get("displayName") or "Sin asignar"


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
    # Traemos todos los campos (incluye la estimación original de tiempo)
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

    completed_hours = 0.0
    completed_count = 0
    completed_with_estimate = 0
    completed_without_estimate = []
    discarded_count = 0
    discarded_breakdown: dict[str, int] = {}
    in_progress_count = 0
    blocked_count = 0
    blocked_details = []
    status_breakdown: dict[str, int] = {}

    # Acumuladores por desarrollador y lista de issues con datos de tiempo faltantes
    def _new_dev() -> dict:
        return {"items_total": 0, "items_done": 0,
                "plan_hours": 0.0, "done_hours": 0.0, "sin_estimacion": 0}

    per_dev: dict[str, dict] = {}
    missing_times: list[dict] = []

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
        resolution_name = (f.get("resolution") or {}).get("name", "")
        summary = f.get("summary", "")
        assignee = get_assignee_name(f)
        issue_type = (f.get("issuetype") or {}).get("name", "")
        est_hours = get_original_estimate_hours(f)
        spent_hours = get_time_spent_hours(f)

        status_breakdown[status_name] = status_breakdown.get(status_name, 0) + 1

        is_discarded = (
            status_name.lower() in EXCLUDED_DONE_STATUSES
            or resolution_name.lower() in EXCLUDED_DONE_STATUSES
        )
        is_done = status_category == DONE_CATEGORY_KEY and not is_discarded

        # ----- Acumulado por desarrollador -----
        if not is_discarded:
            dev = per_dev.setdefault(assignee, _new_dev())
            dev["items_total"] += 1
            dev["plan_hours"] += est_hours
            if est_hours == 0:
                dev["sin_estimacion"] += 1
            if is_done:
                dev["items_done"] += 1
                dev["done_hours"] += est_hours

        # ----- Datos de tiempo faltantes (solo issues vigentes, no descartados) -----
        if not is_discarded and issue_type.lower() not in MISSING_TIMES_EXCLUDE_TYPES:
            if est_hours == 0:
                missing_times.append({
                    "key": key, "summary": summary, "assignee": assignee,
                    "problema": "Falta estimación original",
                })
            if is_done and spent_hours == 0:
                missing_times.append({
                    "key": key, "summary": summary, "assignee": assignee,
                    "problema": "Completada sin registrar tiempo",
                })

        # ----- Conteos globales -----
        if status_name in BLOCKED_STATUSES:
            blocked_count += 1
            blocked_details.append(f"{key}: {summary} (status: {status_name})")
        elif status_category == DONE_CATEGORY_KEY and is_discarded:
            discarded_count += 1
            etiqueta = resolution_name or status_name
            discarded_breakdown[etiqueta] = discarded_breakdown.get(etiqueta, 0) + 1
        elif is_done:
            completed_count += 1
            completed_hours += est_hours
            if est_hours > 0:
                completed_with_estimate += 1
            else:
                completed_without_estimate.append(key)
        elif status_category == IN_PROGRESS_CATEGORY_KEY:
            in_progress_count += 1

    # Redondeo final de acumuladores por dev + varianza
    for dev, d in per_dev.items():
        d["plan_hours"] = round(d["plan_hours"], 2)
        d["done_hours"] = round(d["done_hours"], 2)
        d["varianza_h"] = round(d["done_hours"] - d["plan_hours"], 2)
        d["varianza_pct"] = (round(d["done_hours"] / d["plan_hours"] * 100 - 100, 1)
                             if d["plan_hours"] else "")

    print("Desglose por status:", status_breakdown)
    if discarded_count:
        print(f"Descartados (no cuentan como completados): {discarded_count} "
              f"{discarded_breakdown}")
    print(f"Completados con estimación original: {completed_with_estimate}/"
          f"{completed_count}  (suma = {completed_hours} h)")
    if completed_without_estimate:
        muestra = ", ".join(completed_without_estimate[:15])
        print(f"Completados SIN estimación ({len(completed_without_estimate)}): {muestra}"
              + (" ..." if len(completed_without_estimate) > 15 else ""))
    print(f"Tareas con datos de tiempo faltantes: {len(missing_times)}")
    print("Velocidad por dev (plan_h -> done_h):")
    for dev, d in sorted(per_dev.items(), key=lambda kv: -kv[1]["done_hours"]):
        print(f"  {dev}: {d['plan_hours']} -> {d['done_hours']} h "
              f"(var {d['varianza_h']} h), items {d['items_done']}/{d['items_total']}, "
              f"sin estimación {d['sin_estimacion']}")

    return {
        "sprint_name": sprint_name,
        "completed_hours": completed_hours,
        "completed_count": completed_count,
        "in_progress_count": in_progress_count,
        "blocked_count": blocked_count,
        "blocked_details": "; ".join(blocked_details) if blocked_details else "",
        "per_dev": per_dev,
        "missing_times": missing_times,
    }


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def open_spreadsheet():
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(credentials)
    return client.open_by_key(GOOGLE_SHEETS_ID)


def _get_or_create_ws(sheet, name: str, header: list[str]):
    try:
        return sheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=name, rows=1000, cols=max(10, len(header)))
        ws.append_row(header)
        return ws


def write_summary_row(sheet, row: list) -> None:
    ws = _get_or_create_ws(sheet, RAW_SHEET_NAME,
        ["Timestamp", "Horas completadas", "Items completados",
         "En progreso", "Bloqueados", "Detalles", "PTO", "Notas", "Sprint"])
    ws.append_row(row)


def write_per_dev_rows(sheet, timestamp: str, sprint_name: str,
                       per_dev: dict) -> None:
    ws = _get_or_create_ws(sheet, PER_DEV_SHEET_NAME,
        ["Timestamp", "Sprint", "Desarrollador", "Items totales",
         "Items completados", "Horas planificadas", "Horas completadas",
         "Varianza h", "Varianza %", "Tareas sin estimación"])

    filas = []
    tot = {"items_total": 0, "items_done": 0, "plan_hours": 0.0,
           "done_hours": 0.0, "sin_estimacion": 0}
    for dev, d in sorted(per_dev.items(), key=lambda kv: -kv[1]["done_hours"]):
        filas.append([timestamp, sprint_name, dev, d["items_total"],
                      d["items_done"], d["plan_hours"], d["done_hours"],
                      d["varianza_h"], d["varianza_pct"], d["sin_estimacion"]])
        for k in tot:
            tot[k] += d[k]

    tot_plan = round(tot["plan_hours"], 2)
    tot_done = round(tot["done_hours"], 2)
    filas.append([timestamp, sprint_name, "— EQUIPO —", tot["items_total"],
                  tot["items_done"], tot_plan, tot_done,
                  round(tot_done - tot_plan, 2),
                  round(tot_done / tot_plan * 100 - 100, 1) if tot_plan else "",
                  tot["sin_estimacion"]])

    if filas:
        ws.append_rows(filas)


def write_missing_times_rows(sheet, timestamp: str, sprint_name: str,
                             missing: list[dict]) -> None:
    ws = _get_or_create_ws(sheet, MISSING_TIMES_SHEET_NAME,
        ["Timestamp", "Sprint", "Issue", "Resumen", "Responsable", "Problema"])
    filas = [[timestamp, sprint_name, m["key"], m["summary"],
              m["assignee"], m["problema"]] for m in missing]
    if filas:
        ws.append_rows(filas)


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
        metrics["completed_hours"],
        metrics["completed_count"],
        metrics["in_progress_count"],
        metrics["blocked_count"],
        metrics["blocked_details"],
        pto,
        "",
        metrics["sprint_name"],
    ]

    print("Fila a escribir:", row)
    sheet = open_spreadsheet()
    write_summary_row(sheet, row)
    print(f"Listo. Fila agregada a '{RAW_SHEET_NAME}'.")

    write_per_dev_rows(sheet, timestamp, metrics["sprint_name"], metrics["per_dev"])
    print(f"'{PER_DEV_SHEET_NAME}': {len(metrics['per_dev'])} devs + total.")

    write_missing_times_rows(sheet, timestamp, metrics["sprint_name"],
                             metrics["missing_times"])
    print(f"'{MISSING_TIMES_SHEET_NAME}': {len(metrics['missing_times'])} filas.")


if __name__ == "__main__":
    main()
