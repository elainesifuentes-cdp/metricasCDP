#!/usr/bin/env python3
"""
Rovo Agent: Lee Google Sheet y genera Status Report con Google Gemini.
Envía email desde cuenta corporativa CDPE.

Gemini es GRATIS y estable.
"""

import os
import json
from datetime import datetime
from typing import Dict, List
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.generativeai as genai

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Opcional: nombre (o parte del nombre) del sprint a reportar. Si no se define,
# se usa la última fila cargada en el Sheet (comportamiento por defecto).
REPORT_SPRINT = os.getenv("REPORT_SPRINT", "").strip()

# Email corporativo CDPE
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# ============================================================================
# GOOGLE SHEETS
# ============================================================================

def get_sheets_service():
    """Crea cliente autenticado para Google Sheets."""
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("GOOGLE_CREDENTIALS_JSON no está configurada")
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=credentials)


def _get(metrics: Dict, *keys: str, default: str = "N/A") -> str:
    """Devuelve el primer valor no vacío entre varios nombres de columna posibles."""
    for k in keys:
        v = metrics.get(k)
        if v not in (None, ""):
            return v
    return default


def _row_sprint(headers: List[str], row: List[str]) -> str:
    """Devuelve el valor de la columna 'Sprint' de una fila, si existe."""
    if "Sprint" not in headers:
        return ""
    idx = headers.index("Sprint")
    return row[idx] if idx < len(row) else ""


def read_last_metrics(service) -> Dict:
    """Lee el registro de métricas del Sheet.

    Si REPORT_SPRINT está definido, devuelve la última fila cuyo campo 'Sprint'
    contenga ese texto (case-insensitive). Si no, la última fila cargada.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="'Datos Brutos'!A:I"
    ).execute()

    values = result.get('values', [])

    if len(values) < 2:
        return {}

    headers = values[0]
    data_rows = values[1:]

    if REPORT_SPRINT:
        matches = [r for r in data_rows
                   if REPORT_SPRINT.lower() in _row_sprint(headers, r).lower()]
        if not matches:
            print(f"[✗] No hay filas para el sprint '{REPORT_SPRINT}' en el Sheet")
            return {}
        target_row = matches[-1]
    else:
        target_row = data_rows[-1]

    # Mapear a diccionario
    data = {}
    for i, header in enumerate(headers):
        if i < len(target_row):
            data[header] = target_row[i]
        else:
            data[header] = ""

    return data


def read_historical_metrics(service) -> Dict:
    """Lee histórico de últimas 8 semanas (filtrado por sprint si corresponde)."""
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="'Datos Brutos'!A:I"
    ).execute()

    values = result.get('values', [])
    if len(values) < 2:
        return {"rows": [], "count": 0}

    headers = values[0]
    data_rows = values[1:]

    if REPORT_SPRINT:
        data_rows = [r for r in data_rows
                     if REPORT_SPRINT.lower() in _row_sprint(headers, r).lower()]

    # Encabezado + últimas 8 filas
    historical = [headers] + data_rows[-8:]

    return {
        "rows": historical,
        "count": len(historical) - 1
    }


def _read_tab(service, tab: str, rng: str = "A:Z") -> Dict:
    """Lee una pestaña como {headers, rows}. rows son dicts header->valor."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{tab}'!{rng}"
        ).execute()
    except Exception as exc:  # pestaña inexistente u otro error
        print(f"[!] No se pudo leer la pestaña '{tab}': {str(exc)[:80]}")
        return {"headers": [], "rows": []}

    values = result.get('values', [])
    if len(values) < 2:
        return {"headers": values[0] if values else [], "rows": []}

    headers = values[0]
    rows = []
    for raw in values[1:]:
        rows.append({h: (raw[i] if i < len(raw) else "") for i, h in enumerate(headers)})
    return {"headers": headers, "rows": rows}


def _latest_batch(rows: List[Dict]) -> List[Dict]:
    """Filtra las filas del último Timestamp (y del sprint pedido, si aplica)."""
    if REPORT_SPRINT:
        rows = [r for r in rows if REPORT_SPRINT.lower() in str(r.get("Sprint", "")).lower()]
    if not rows:
        return []
    last_ts = max(r.get("Timestamp", "") for r in rows)
    return [r for r in rows if r.get("Timestamp", "") == last_ts]


def read_per_dev(service) -> List[Dict]:
    data = _read_tab(service, "Velocidad por Dev")
    return _latest_batch(data["rows"])


def read_missing_times(service) -> List[Dict]:
    data = _read_tab(service, "Tareas sin tiempo")
    return _latest_batch(data["rows"])


def format_per_dev(rows: List[Dict]) -> str:
    if not rows:
        return "Sin datos de velocidad por desarrollador."
    lineas = []
    for r in rows:
        lineas.append(
            f"- {r.get('Desarrollador', '?')}: "
            f"plan {r.get('Horas planificadas', '?')}h / "
            f"completado {r.get('Horas completadas', '?')}h "
            f"(var {r.get('Varianza h', '?')}h, {r.get('Varianza %', '?')}%), "
            f"items {r.get('Items completados', '?')}/{r.get('Items totales', '?')}, "
            f"sin estimación: {r.get('Tareas sin estimación', '0')}"
        )
    return "\n".join(lineas)


def format_missing_times(rows: List[Dict]) -> str:
    if not rows:
        return "✅ Todas las tareas del sprint tienen estimación y registro de tiempo."
    por_persona: Dict[str, List[str]] = {}
    for r in rows:
        persona = r.get("Responsable", "Sin asignar")
        por_persona.setdefault(persona, []).append(
            f"{r.get('Issue', '?')} — {r.get('Problema', '?')}"
        )
    lineas = [f"⚠️ {len(rows)} tareas con datos de tiempo faltantes:"]
    for persona, items in sorted(por_persona.items()):
        lineas.append(f"\n{persona}:")
        lineas.extend(f"  - {it}" for it in items)
    return "\n".join(lineas)


# ============================================================================
# GEMINI - Generar reporte narrativo
# ============================================================================

def generate_status_report_with_gemini(last_metrics: Dict, historical: Dict,
                                       per_dev: List[Dict],
                                       missing_times: List[Dict]) -> str:
    """
    Usa Google Gemini para generar un reporte narrativo automático.
    Gemini es GRATIS: https://ai.google.dev/
    """
    per_dev_txt = format_per_dev(per_dev)
    missing_txt = format_missing_times(missing_times)
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")  # Modelo gratis de Gemini

        # Preparar contexto para Gemini
        context = f"""
        Eres un PM/Delivery Manager que genera reportes semanales de métricas de desarrollo.

        DATOS ESTA SEMANA:
        - Sprint: {_get(last_metrics, 'Sprint')}
        - Timestamp: {_get(last_metrics, 'Timestamp')}
        - Horas completadas (estimación original): {_get(last_metrics, 'Horas completadas', 'Horas Completadas', 'Puntos Completados', 'Puntos completados')}
        - Items completados: {_get(last_metrics, 'Items Completados', 'Items completados')}
        - En progreso: {_get(last_metrics, 'En Progreso', 'En progreso')}
        - Bloqueados: {_get(last_metrics, 'Bloqueados')}
        - Detalles bloqueados: {_get(last_metrics, 'Detalles Bloqueados', 'Detalles')}
        - PTO: {_get(last_metrics, 'PTO', default='Sin PTO')}
        - Notas: {_get(last_metrics, 'Notas')}

        VELOCIDAD POR DESARROLLADOR (horas de estimación original):
        {per_dev_txt}

        TAREAS SIN DATOS DE TIEMPO:
        {missing_txt}

        Genera un reporte ejecutivo conciso con:
        1. Resumen de avance del equipo (horas completadas vs planificadas + items) + bloqueados
        2. Velocidad por desarrollador: destacá quién quedó por debajo/encima de lo planificado
        3. Calidad de datos: mencioná las tareas sin estimación / sin registro de tiempo y a quién pedírselas
        4. Riesgos y recomendaciones

        Formato:
        📊 STATUS SEMANAL

        ✅ Avance del equipo: [...]
        👤 Por desarrollador: [...]
        🧹 Datos a completar: [...]
        ⚠️ Riesgos: [...]
        🎯 Acciones: [...]

        Usa emojis y sé conciso. No inventes números: usá solo los datos provistos.
        """

        response = model.generate_content(context)
        return response.text

    except Exception as e:
        print(f"[!] Gemini falló ({str(e)[:50]}...), usando template simple")
        return generate_simple_report(last_metrics, historical, per_dev, missing_times)


def generate_simple_report(last_metrics: Dict, historical: Dict,
                           per_dev: List[Dict] = None,
                           missing_times: List[Dict] = None) -> str:
    """Template simple si Gemini falla."""
    return f"""
📊 STATUS SEMANAL - {last_metrics.get('Timestamp', 'Esta semana')}

✅ Logros:
- {_get(last_metrics, 'Horas completadas', 'Horas Completadas', 'Puntos Completados', 'Puntos completados')} horas completadas (estimación original)
- {_get(last_metrics, 'Items Completados', 'Items completados')} items completados

⚠️ Estado:
- En progreso: {_get(last_metrics, 'En Progreso', 'En progreso')} items
- Bloqueados: {_get(last_metrics, 'Bloqueados')} items
- PTO: {_get(last_metrics, 'PTO', default='Sin PTO')}

👤 Velocidad por desarrollador:
{format_per_dev(per_dev or [])}

🧹 Tareas sin datos de tiempo:
{format_missing_times(missing_times or [])}

📝 Notas: {last_metrics.get('Notas', 'Sin comentarios')}

---
Reporte generado automáticamente
"""


# ============================================================================
# EMAIL
# ============================================================================

def send_status_email(subject: str, html_body: str, recipient: str) -> bool:
    """Envía email con el reporte."""
    try:
        # Crear mensaje
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = recipient
        
        # Parte texto plano (fallback)
        text_body = f"""
        Status Semanal - Métricas CDPE
        
        {html_body}
        
        ---
        Generado automáticamente por Rovo Agent con Gemini (gratis)
        """
        part1 = MIMEText(text_body, "plain")
        
        # Parte HTML
        html_formatted = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px;">
              <pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">{html_body}</pre>
              <hr style="margin: 20px 0; border: none; border-top: 1px solid #ddd;">
              <p style="font-size: 12px; color: #999;">
                Generado automáticamente por Rovo Agent | {datetime.now().strftime('%Y-%m-%d %H:%M')}
              </p>
            </div>
          </body>
        </html>
        """
        part2 = MIMEText(html_formatted, "html")
        
        # Agregar partes (HTML es preferida)
        msg.attach(part1)
        msg.attach(part2)
        
        # Conectar al servidor SMTP y enviar
        print(f"[*] Conectando a {SMTP_SERVER}:{SMTP_PORT}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            print(f"[*] Autenticando como {SENDER_EMAIL}...")
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            print(f"[*] Enviando email a {recipient}...")
            server.send_message(msg)
        
        print("[✓] Email enviado exitosamente")
        return True
        
    except Exception as e:
        print(f"[✗] Error enviando email: {e}")
        return False


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Orquesta lectura de Sheet, generación de reporte y envío de email."""
    print("[*] Iniciando Rovo Agent - Status Report (Gemini)...")
    
    # Validar variables de entorno
    if not all([GOOGLE_SHEETS_ID, GOOGLE_CREDENTIALS_JSON, 
                SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL, GEMINI_API_KEY]):
        raise ValueError(
            "Faltan variables de entorno: "
            "GOOGLE_SHEETS_ID, GOOGLE_CREDENTIALS_JSON, "
            "SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL, GEMINI_API_KEY"
        )
    
    # Leer datos del Sheet
    print("[*] Leyendo datos del Google Sheet...")
    sheets_service = get_sheets_service()
    last_metrics = read_last_metrics(sheets_service)
    historical = read_historical_metrics(sheets_service)
    per_dev = read_per_dev(sheets_service)
    missing_times = read_missing_times(sheets_service)

    if not last_metrics:
        print("[✗] No hay datos en el Sheet")
        return

    print(f"[+] Datos leídos: {len(per_dev)} devs, {len(missing_times)} tareas sin tiempo")

    # Generar reporte con Gemini
    print("[*] Generando reporte con Gemini (gratis)...")
    report_content = generate_status_report_with_gemini(
        last_metrics, historical, per_dev, missing_times)
    print("[+] Reporte generado")
    print("\n" + "="*60)
    print(report_content)
    print("="*60 + "\n")
    
    # Enviar email
    sprint_label = last_metrics.get('Sprint', '') or REPORT_SPRINT
    sprint_suffix = f" - {sprint_label}" if sprint_label else ""
    subject = (f"📊 Status Semanal - Métricas CDPE{sprint_suffix} - "
               f"{datetime.now().strftime('%d de %B, %Y')}")
    success = send_status_email(subject, report_content, RECIPIENT_EMAIL)
    
    if success:
        print("[✓] Rovo Agent completado exitosamente")
    else:
        print("[✗] Error enviando email")
        return False
    
    return True


if __name__ == "__main__":
    main()
