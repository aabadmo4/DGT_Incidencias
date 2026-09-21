import os
import re
import time
import datetime
from zoneinfo import ZoneInfo
import requests
from collections import defaultdict
from lxml import etree
from google import genai
from google.genai import errors as genai_errors
from pydub import AudioSegment

# 1. CONFIGURACIÓN DE CREDENCIALES MEDIANTE VARIABLES DE ENTORNO
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]


def obtener_hora_madrid():
    """Devuelve la hora actual en la zona horaria de Madrid (España),
    ajustándose automáticamente a horario de invierno/verano."""
    return datetime.datetime.now(ZoneInfo("Europe/Madrid"))


def obtener_fecha_hora_generacion():
    """Cadena legible en español con la fecha y hora de generación del boletín,
    en hora de Madrid, para anteponerla al propio boletín."""
    ahora = obtener_hora_madrid()
    dia_semana = DIAS_ES[ahora.weekday()]
    mes = MESES_ES[ahora.month - 1]
    return f"{dia_semana} {ahora.day} de {mes}, {ahora.hour:02d}:{ahora.minute:02d} horas"


def obtener_saludo_y_momento():
    hora = obtener_hora_madrid().hour
    if 6 <= hora < 12:
        return "Buenos días"
    elif 12 <= hora < 20:
        return "Buenas tardes"
    else:
        return "Buenas noches"


def extraer_sentido_carril_pk(record):
    """Extrae sentido de circulación, carril(es) afectado(s) y punto kilométrico
    de un situationRecord del XML DATEX II de la DGT."""
    direcciones_cardinales = {
        "NORTHBOUND": "sentido norte", "SOUTHBOUND": "sentido sur",
        "EASTBOUND": "sentido este", "WESTBOUND": "sentido oeste",
        "NORTHEASTBOUND": "sentido noreste", "NORTHWESTBOUND": "sentido noroeste",
        "SOUTHEASTBOUND": "sentido sureste", "SOUTHWESTBOUND": "sentido suroeste",
    }
    sentidos_kilometracion = {
        "POSITIVE": "sentido creciente", "NEGATIVE": "sentido decreciente", "BOTH": "ambos sentidos",
    }
    carriles_traducidos = {
        "RIGHTLANE": "carril derecho", "LEFTLANE": "carril izquierdo", "CENTRALLANE": "carril central",
        "HARDSHOULDER": "arcén", "SHOULDERLANE": "arcén", "BUSLANE": "carril bus",
        "ALLLANESCOMPLETECARRIAGEWAY": "todos los carriles",
    }

    partes_sentido = []
    for tag in ("tpegDirection", "tpegDirectionRoad"):
        for v in record.xpath(f'.//*[local-name()="{tag}"]/text()'):
            v_upper = v.strip().upper()
            if v_upper in direcciones_cardinales and direcciones_cardinales[v_upper] not in partes_sentido:
                partes_sentido.append(direcciones_cardinales[v_upper])
            elif v_upper in sentidos_kilometracion and sentidos_kilometracion[v_upper] not in partes_sentido:
                partes_sentido.append(sentidos_kilometracion[v_upper])
    sentido = " / ".join(partes_sentido) if partes_sentido else None

    carriles = []
    for v in record.xpath('.//*[local-name()="laneUsage"]/text()'):
        v_upper = v.strip().upper()
        if v_upper in carriles_traducidos and carriles_traducidos[v_upper] not in carriles:
            carriles.append(carriles_traducidos[v_upper])

    puntos_km = sorted(set(record.xpath('.//*[local-name()="kilometerPoint"]/text()')))
    if not puntos_km:
        pk_str = None
    elif len(puntos_km) == 1:
        pk_str = f"pk {puntos_km[0]}"
    else:
        pk_str = f"entre pk {puntos_km[0]} y pk {puntos_km[-1]}"

    return sentido, carriles, pk_str


def limpiar_y_extraer_detalles(record):
    traducciones_causa = {
        "ROADWORKS": "Obras", "ROADMAINTENANCE": "Mantenimiento",
        "CARRIAGEWAYCLOSURE": "Corte total de calzada", "ACCIDENT": "Accidente",
        "POORWEATHERCONDITIONS": "Meteorología adversa", "SNOW": "Nieve",
        "ICE": "Hielo", "FLOODING": "Inundación", "OBSTRUCTION": "Obstáculo en la vía",
        "TRAFFICCONGESTION": "Retención"
    }
    valores_estructurales = {
        'unspecifiedcarriageway', 'unknown', 'both', 'negative', 'positive',
        'rightlane', 'leftlane', 'centrallane', 'hardshoulder', 'shoulderlane',
        'buslane', 'alllanescompletecarriageway'
    }

    raw_texts = [t.strip() for t in record.xpath('.//text()') if t.strip()]
    municipios, provincia, causas = [], "", []

    for t in raw_texts:
        t_clean = t.strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}', t_clean) or re.match(r'^-?\d+\.\d+$', t_clean) or re.match(r'^[A-Z0-9_]{8,}$', t_clean):
            continue
        t_lower = t_clean.lower()
        if t_lower in ['true', 'false', 'dgt', 'certain', 'active', 'segment', 'mandatory', 'anyvehicle']:
            continue
        if t_lower in valores_estructurales or t_lower.endswith('bound'):
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

    sentido, carriles, pk_str = extraer_sentido_carril_pk(record)

    ubicacion_str = f"Entre/En: {', '.join(municipios)}" if municipios else "Tramo local"
    causa_str = f"Incidencia: {', '.join(set(causas))}" if causas else "Afección en la vía"

    detalle_extra = []
    if pk_str:
        detalle_extra.append(pk_str.capitalize())
    if sentido:
        detalle_extra.append(f"Sentido: {sentido}")
    if carriles:
        detalle_extra.append(f"Carril(es): {', '.join(carriles)}")

    if provincia == "Zaragoza" and detalle_extra:
        return f"{provincia} | {ubicacion_str} | {causa_str} | " + " | ".join(detalle_extra)
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


def obtener_incidencias_calles_zaragoza(intentos=3, espera_inicial=5):
    """Consulta el dataset abierto 'Incidencias en la Vía Pública' del Ayuntamiento
    de Zaragoza (tipo 1: cortes de tráfico, tipo 2: afecciones importantes/obras/
    desvíos; se excluyen los cortes de agua, tipo 0).

    El servidor de zaragoza.es a veces tarda en responder o no contesta a tiempo
    (timeout de conexión intermitente), así que se reintenta unas pocas veces con
    espera creciente antes de darse por vencido."""
    url = "https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json"
    params = {"srsname": "utm30n", "rows": 30, "q": "tipo.id==1,tipo.id==2"}
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json'
    }

    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()

            if isinstance(data, list):
                registros = data
            elif isinstance(data, dict):
                registros = data.get("result") or data.get("incidencia") or data.get("results") or data.get("items") or []
            else:
                registros = []

            lineas = []
            for item in registros:
                if not isinstance(item, dict):
                    continue

                calle = item.get("calle") or item.get("title") or item.get("nombre") or "Vía no especificada"
                tramo = item.get("tramo") or ""
                motivo = item.get("motivo") or item.get("description") or "Obras/afección en la vía"
                inicio = item.get("inicio") or ""
                fin = item.get("fin") or ""

                # Limpiar la marca de tiempo 'T00:00:00' si viene en los strings
                if fin and 'T' in str(fin):
                    fin = str(fin).split('T')[0]
                if inicio and 'T' in str(inicio):
                    inicio = str(inicio).split('T')[0]

                partes = [str(calle)]
                if tramo:
                    partes.append(str(tramo))
                partes.append(str(motivo))
                if inicio or fin:
                    partes.append(f"(hasta {fin})" if fin else f"(desde {inicio})")

                lineas.append("- " + " | ".join(p for p in partes if p))

            return "\n".join(lineas) if lineas else "Sin incidencias destacadas en la ciudad."

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            ultimo_error = e
            print(f"Aviso: zaragoza.es no responde (intento {intento}/{intentos}): {e}")
            if intento < intentos:
                time.sleep(espera_inicial * intento)
        except Exception as e:
            # Errores no relacionados con conectividad (JSON inválido, HTTP 4xx/5xx
            # persistente, etc.): no merece la pena reintentar.
            return f"No se pudo consultar incidencias municipales de Zaragoza: {e}"

    return f"No se pudo consultar incidencias municipales de Zaragoza tras {intentos} intentos: {ultimo_error}"


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


def generar_contenido_con_reintentos(client, model, contents, intentos=4, espera_inicial=15):
    """Llama a Gemini tolerando el error 503 UNAVAILABLE (modelo saturado),
    reintentando hasta 'intentos' veces con espera creciente."""
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except genai_errors.ServerError as e:
            ultimo_error = e
            print(f"Aviso: Gemini no disponible (intento {intento}/{intentos}): {e}")
            if intento < intentos:
                time.sleep(espera_inicial * intento)
    raise ultimo_error


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

    datos_ciudad_zaragoza = obtener_incidencias_calles_zaragoza()

    saludo_dinamico = obtener_saludo_y_momento()
    fecha_hora_str = obtener_fecha_hora_generacion()

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
Eres un locutor de radio experto en información de tráfico y tiempo regional. Genera un boletín locutado fluido (240-300 palabras) para ser leído en voz alta.

ORDEN SECUENCIAL OBLIGATORIO DE LA LOCUCIÓN:

1. INTRODUCCIÓN Y TIEMPO (Obligatorio al inicio):
   - Empieza con el saludo exacto "{saludo_dinamico}". No menciones ni inventes tú la fecha o la hora: ya se añaden aparte, antes de tu texto.
   - Incluye inmediatamente un apunte meteorológico rápido (10-15 palabras) sobre la situación del tiempo en el valle del Ebro y la zona norte.

2. INCIDENCIAS URBANAS EN ZARAGOZA CIUDAD (Ayuntamiento):
   - Justo después de la meteorología, pasa a informar sobre las calles de la ciudad de Zaragoza.
   - Revisa TODAS las incidencias urbanas proporcionadas en "DATOS AYUNTAMIENTO DE ZARAGOZA".
   - Ofrece un panorama representativo: menciona los cortes y obras más destacados por el nombre exacto de la calle o avenida que aparece en los datos, sin omitir puntos relevantes. Si hay obras menores secundarias, puedes agruparlas de forma natural.
   - Si el texto de esos datos indica que no hay incidencias o que hubo un error, salta a la siguiente sección en silencio sin mencionarlo.

3. CARRETERAS INTERURBANAS DE DGT (Zaragoza provincia y resto):
   - Dedica la parte principal de la información interurbana a las carreteras de la provincia de Zaragoza. Incluye TODAS las incidencias (retenciones, obras, obstáculos) indicando sentido, carril o punto kilométrico cuando existan en los datos.
   - Termina con un breve repaso ágil a las carreteras del resto de zonas (Huesca, Teruel, Navarra y La Rioja).

REGLAS DE FORMATO:
- NO uses emojis, asteriscos (*) ni encabezados markdown (##).
- Redacta con estilo de radio: lenguaje natural, articulado y fluido para locución en directo.
- Nombres conocidos: Asocia nombres populares a las carreteras (ej. "Autovía de Logroño", "Carretera de Belate", "Ronda de Zaragoza").

DATOS AYUNTAMIENTO DE ZARAGOZA (calles de la ciudad):
{datos_ciudad_zaragoza}

DATOS DGT (carreteras interurbanas):
{datos_trafico}
"""

    try:
        response = generar_contenido_con_reintentos(client, model='gemini-3.6-flash', contents=prompt)
    except genai_errors.ServerError as e:
        print(f"Gemini siguió sin estar disponible tras varios reintentos: {e}")
        return

    texto_generado = response.text.strip()
    texto_informe = f"Boletín de tráfico actualizado el {fecha_hora_str}. {texto_generado}"

    archivo_mp3 = "boletin_trafico.mp3"
    if generar_voz_espanol(texto_informe, archivo_mp3, velocidad=1.25):
        enviar_a_telegram(archivo_mp3, texto_informe)


if __name__ == "__main__":
    main()
