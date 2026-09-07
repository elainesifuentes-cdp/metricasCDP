#!/usr/bin/env python3
"""
Rovo Agent: Lee Google Sheet y genera Status Report narrativo con Groq.
Envía email desde cuenta corporativa CDPE.

Groq es GRATIS y no requiere pago.
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
from groq import Groq

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Email corporativo CDPE
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# Groq API
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


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


def read_last_metrics(service) -> Dict:
    """Lee el último registro de métricas del Sheet."""
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="'Datos Brutos'!A:H"
    ).execute()
    
    values = result.get('values', [])
    
    if len(values) < 2:
        return {}
    
    # Última fila de datos
    headers = values[0]
    last_row = values[-1]
    
    # Mapear a diccionario
    data = {}
    for i, header in enumerate(headers):
        if i < len(last_row):
            data[header] = last_row[i]
        else:
            data[header] = ""
    
    return data


def read_historical_metrics(service) -> Dict:
    """Lee histórico de últimas 8 semanas."""
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="'Datos Brutos'!A:E"
    ).execute()
    
    values = result.get('values', [])
    
    # Últimas 8 filas (últimas 8 semanas)
    historical = values[-8:] if len(values) > 1 else []
    
    return {
        "rows": historical,
        "count": len(historical) - 1
    }


# ============================================================================
# GROQ - Generar reporte narrativo
# ============================================================================

def generate_status_report_with_groq(last_metrics: Dict, historical: Dict) -> str:
    """
    Usa Groq para generar un reporte narrativo automático.
    Groq es GRATIS: https://groq.com/
    """
    client = Groq(api_key=GROQ_API_KEY)
    
    # Preparar contexto para Groq
    context = f"""
    Eres un PM/Delivery Manager que genera reportes semanales de métricas de desarrollo.
    
    DATOS ESTA SEMANA:
    - Timestamp: {last_metrics.get('Timestamp', 'N/A')}
    - Puntos completados: {last_metrics.get('Puntos Completados', 'N/A')}
    - Items completados: {last_metrics.get('Items Completados', 'N/A')}
    - En progreso: {last_metrics.get('En Progreso', 'N/A')}
    - Bloqueados: {last_metrics.get('Bloqueados', 'N/A')}
    - Detalles bloqueados: {last_metrics.get('Detalles Bloqueados', 'N/A')}
    - PTO: {last_metrics.get('PTO', 'Sin PTO')}
    - Notas: {last_metrics.get('Notas', 'N/A')}
    
    HISTÓRICO (últimas 8 semanas):
    {json.dumps(historical.get('rows', []), ensure_ascii=False, indent=2)}
    
    Genera un reporte ejecutivo en formato EMAIL que:
    1. Resuma el status esta semana (velocity, bloqueados, PTO)
    2. Analice tendencias (¿mejoramos o empeoramos?)
    3. Destaque riesgos y bloqueantes principales
    4. Dé recomendaciones accionables
    5. Sea conciso (máximo 15 líneas)
    6. Usa emojis para visual
    
    Formato:
    📊 STATUS SEMANAL - [FECHA]
    
    [Párrafo 1: Resumen ejecutivo]
    
    ✅ Logros:
    [Puntos clave]
    
    ⚠️ Riesgos:
    [Puntos bloqueados]
    
    🎯 Acciones recomendadas:
    [Puntos accionables]
    
    📈 Tendencia:
    [Análisis histórico]
    """
    
    message = client.messages.create(
        model="mixtral-8x7b-32768",  # Modelo gratuito de Groq
        max_tokens=1024,
        messages=[
            {"role": "user", "content": context}
        ]
    )
    
    return message.choices[0].message.content


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
        Generado automáticamente por Rovo Agent con Groq (gratis)
        """
        part1 = MIMEText(text_body, "plain")
        
        # Parte HTML
        html_formatted = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px;">
              {html_body}
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
    print("[*] Iniciando Rovo Agent - Status Report (Groq)...")
    
    # Validar variables de entorno
    if not all([GOOGLE_SHEETS_ID, GOOGLE_CREDENTIALS_JSON, 
                SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL, GROQ_API_KEY]):
        raise ValueError(
            "Faltan variables de entorno: "
            "GOOGLE_SHEETS_ID, GOOGLE_CREDENTIALS_JSON, "
            "SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL, GROQ_API_KEY"
        )
    
    # Leer datos del Sheet
    print("[*] Leyendo datos del Google Sheet...")
    sheets_service = get_sheets_service()
    last_metrics = read_last_metrics(sheets_service)
    historical = read_historical_metrics(sheets_service)
    
    if not last_metrics:
        print("[✗] No hay datos en el Sheet")
        return
    
    print("[+] Datos leídos exitosamente")
    
    # Generar reporte con Groq
    print("[*] Generando reporte con Groq (gratis)...")
    report_content = generate_status_report_with_groq(last_metrics, historical)
    print("[+] Reporte generado")
    print("\n" + "="*60)
    print(report_content)
    print("="*60 + "\n")
    
    # Enviar email
    subject = f"📊 Status Semanal - Métricas CDPE - {datetime.now().strftime('%d de %B, %Y')}"
    success = send_status_email(subject, report_content, RECIPIENT_EMAIL)
    
    if success:
        print("[✓] Rovo Agent completado exitosamente")
    else:
        print("[✗] Error enviando email")
        return False
    
    return True


if __name__ == "__main__":
    main()
