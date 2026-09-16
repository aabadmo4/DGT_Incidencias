import os
import re
import datetime
import requests
from collections import defaultdict
from lxml import etree
from google import genai
from pydub import AudioSegment

# 1. CONFIGURACIÓN DE CREDENCIALES MEDIANTE VARIABLES DE ENTORNO
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def obtener_saludo_y_momento():
    hora = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).hour
    if 6 <= hora < 12:
        return "Buenos días"
    elif 12 <= hora < 20:
        return "Buenas tardes"
    else:
        return "Buenas noches"

def limpiar_y_extraer_detalles(record):
    traducciones_causa = {
        "ROADWORKS": "Obras", "ROADMAINTENANCE": "Mantenimiento",
        "CARRIAGEWAYCLOSURE": "Corte total de calzada", "ACCIDENT": "Accidente",
        "POORWEATHERCONDITIONS": "Meteorología adversa", "SNOW": "Nieve",
        "ICE": "Hielo", "FLOODING": "Inundación", "OBSTRUCTION": "Obstáculo",
        "TRAFFICCONGESTION": "Retención"
    }

    raw_texts = [t.strip() for t in record.xpath('.//text()') if t.strip()]
    municipios, provincia, causas = [], "", []
    
    for t in raw_texts:
        t_clean = t.strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}', t_clean) or re.match(r'^-?\d+\.\d+$', t_clean) or re.match(r'^[A-Z0-9_]{8,}$', t_clean):
            continue
        if t_clean.lower() in ['true', 'false', 'dgt', 'certain', 'active', 'segment', 'positive', 'mandatory', 'anyvehicle']:
            continue

        t_upper = t_clean.upper()
        if t_upper in traducciones_causa:
            causas.append(traducciones_causa[t_upper])
            continue

        if t_clean in ["Teruel", "Zaragoza", "Huesca", "Navarra", "La Rioja"]:
            provincia = t_clean
        elif len(t_clean) > 2 and not t_clean.replace('.', '').isdigit() and t_clean not in ["Aragón", "Comunidad Foral de Navarra"]:
            if t_clean not in municipios:
                municipios.append(t_clean)

    ubicacion_str = f"Entre/En: {', '.join(municipios)}" if municipios else "Tramo local"
    causa_str = f"Incidencia: {', '.join(set(causas))}" if causas else "Afección en la vía"
    return f"{provincia} | {ubicacion_str} | {causa_str}"

def obtener_incidencias_texto():
    url = "https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml"
    headers = {'User-Agent': 'Mozilla/5.0'}
    regiones_mapa = {
        "ARAGÓN": ["ZARAGOZA", "HUESCA", "TERUEL", "ARAGON", "ARAGÓN"],
        "COMUNIDAD FORAL DE NAVARRA": ["NAVARRA", "PAMPLONA"],
        "LA RIOJA": ["RIOJA", "LOGROÑO"]
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        parser = etree.XMLParser(recover=True, encoding='utf-8')
        root = etree.fromstring(response.content, parser=parser)
        incidencias_por_zona = defaultdict(list)

        for record in root.xpath('//*[local-name()="situationRecord"]'):
            roads = record.xpath('.//*[local-name()="roadName"]/text()')
            road_name = roads[0].strip() if roads else "Vía local"
            raw_texts = [t.strip() for t in record.xpath('.//text()') if t.strip()]
            texto_evaluacion = f"{road_name} " + " ".join(raw_texts).upper()

            region_encontrada = None
            for region, terminos in regiones_mapa.items():
                if any(term in texto_evaluacion for term in terminos):
                    region_encontrada = region
                    break

            if region_encontrada:
                resumen = limpiar_y_extraer_detalles(record)
                incidencias_por_zona[region_encontrada].append(f"- Código oficial: {road_name} -> {resumen}")

        texto_resultado = ""
        for reg in ["ARAGÓN", "COMUNIDAD FORAL DE NAVARRA", "LA RIOJA"]:
            if reg in incidencias_por_zona:
                texto_resultado += f"\n--- REGIÓN: {reg} ---\n" + "\n".join(incidencias_por_zona[reg]) + "\n"

        return texto_resultado if texto_resultado else "Sin incidencias."
    except Exception as e:
        return f"Error extrayendo datos: {e}"

def acelerar_audio(archivo_entrada, archivo_salida, velocidad=1.25):
    audio = AudioSegment.from_file(archivo_entrada)
    audio_rapido = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * velocidad)})
    audio_rapido = audio_rapido.set_frame_rate(audio.frame_rate)
    audio_rapido.export(archivo_salida, format="mp3")

def generar_voz_espanol(texto, archivo_salida="boletin_trafico.mp3", velocidad=1.25):
    texto_limpio = re.sub(r'[*#\_]', '', texto)
    partes = re.split(r'(?<=[.?!])\s+', texto_limpio)
    fragmentos = []

    for parte in partes:
        if len(parte) > 180:
            fragmentos.extend(re.split(r'(?<=[,;])\s+', parte))
        else:
            fragmentos.append(parte)

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    bytes_audio_totales = bytearray()

    for fragmento in fragmentos:
        fragmento = fragmento.strip()
        if not fragmento:
            continue
        base_url = "https://translate.google.com/translate_tts"
        params = {"ie": "UTF-8", "q": fragmento, "tl": "es", "client": "tw-ob"}
        res = requests.get(base_url, params=params, headers=headers)
        if res.status_code == 200:
            bytes_audio_totales.extend(res.content)

    if bytes_audio_totales:
        temp_file = "temp_boletin.mp3"
        with open(temp_file, "wb") as f:
            f.write(bytes_audio_totales)
        acelerar_audio(temp_file, archivo_salida, velocidad=velocidad)
        return True
    return False

def enviar_a_telegram(archivo_audio, texto_transcripcion):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio"
    
    caption = f"🎙️ **Boletín de Tráfico DGT**\n\n{texto_transcripcion}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    with open(archivo_audio, "rb") as audio:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
        files = {"audio": audio}
        requests.post(url, data=payload, files=files)

def main():
    datos_trafico = obtener_incidencias_texto()
    if "Sin incidencias" in datos_trafico or "Error" in datos_trafico:
        return

    saludo_dinamico = obtener_saludo_y_momento()
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
    Eres un locutor de radio experto en información de tráfico y tiempo regional. Genera un boletín locutado muy breve y fluido (110-130 palabras) para ser leído en voz alta sobre las incidencias en tiempo real de la DGT para Aragón, Navarra y La Rioja.

    REGLAS DE ESTRUCTURA Y FORMATO:
    1. Saludo dinámico: Comienza obligatoriamente con el saludo "{saludo_dinamico}".
    2. Apunte meteorológico rápido: Tras el saludo, incluye una frase muy breve (10-15 palabras) sobre la situación del tiempo en el valle del Ebro y la zona norte.
    3. Estado del tráfico: Resume de forma continua las incidencias más destacadas por regiones.
    4. Sin marcas visuales: NO uses emojis, asteriscos (*) ni encabezados markdown (##).
    5. Nombres conocidos: Asocia nombres populares a las carreteras (ej. "Autovía de Logroño", "Carretera de Belate", "Ronda de Zaragoza").

    DATOS DGT:
    {datos_trafico}
    """

    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt
    )

    texto_informe = response.text
    archivo_mp3 = "boletin_trafico.mp3"

    if generar_voz_espanol(texto_informe, archivo_mp3, velocidad=1.25):
        enviar_a_telegram(archivo_mp3, texto_informe)

if __name__ == "__main__":
    main()

