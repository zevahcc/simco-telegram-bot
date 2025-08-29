import os
import logging
import json
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue
)
import httpx
from unidecode import unidecode
import re

# Configuración de Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Constantes y Configuraciones ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("No se ha configurado la variable de entorno TELEGRAM_BOT_TOKEN.")

# Código de administrador
ADMIN_CODE = "e2358e"

# Nueva API para el mercado
SIMCOMPANIES_API_BASE_URL = "https://www.simcompanies.com/api/v3/market/0/"
RESOURCE_API_BASE_URL = "https://api.simcotools.com/v1/realms/0/market/resources/"
ALERTS_FILE = "alerts.json"
LAST_ALERTED_DATETIMES_FILE = "last_alerted_datetimes.json"
STATIC_RESOURCES_FILE = "recursos_estaticos.json"

# Diccionario para almacenar los recursos estáticos (nombre -> ID)
STATIC_RESOURCES = {}

# Nueva Data de Edificios
BUILDING_DATA_FILE = "building_data.txt"
BUILDINGS = []

# --- Funciones de Utility para Persistencia ---
def load_alerts():
    """Carga las alertas desde el archivo JSON."""
    try:
        with open(ALERTS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_alerts(alerts):
    """Guarda las alertas en el archivo JSON."""
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=4)

def load_last_alerted_datetimes():
    """Carga los últimos datetimes/posted alertados desde el archivo JSON."""
    try:
        with open(LAST_ALERTED_DATETIMES_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_last_alerted_datetimes(datetimes):
    """Guarda los últimos datetimes/posted alertados en el archivo JSON."""
    with open(LAST_ALERTED_DATETIMES_FILE, 'w') as f:
        json.dump(datetimes, f, indent=4)

def load_static_resources():
    """
    Carga los recursos estáticos desde el archivo JSON.
    Se espera que el JSON sea un diccionario { "Nombre del Recurso": ID }.
    """
    global STATIC_RESOURCES
    try:
        if not os.path.exists(STATIC_RESOURCES_FILE):
            logger.warning(f"Archivo de recursos estáticos '{STATIC_RESOURCES_FILE}' no encontrado. Las búsquedas de nombres no funcionarán.")
            return
        with open(STATIC_RESOURCES_FILE, 'r', encoding='utf-8') as f:
            STATIC_RESOURCES = json.load(f)
        logger.info(f"Recursos estáticos cargados exitosamente desde {STATIC_RESOURCES_FILE}.")
    except json.JSONDecodeError:
        logger.error(f"Error al decodificar JSON en '{STATIC_RESOURCES_FILE}'. Asegúrate de que el formato sea correcto.")
        STATIC_RESOURCES = {}
    except Exception as e:
        logger.error(f"Error inesperado al cargar recursos estáticos: {e}", exc_info=True)
        STATIC_RESOURCES = {}

def load_building_data():
    """
    Carga la data de edificios desde el archivo de texto BUILDING_DATA_FILE
    y la formatea en la lista global BUILDINGS.
    """
    global BUILDINGS
    try:
        with open(BUILDING_DATA_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        for line in lines:
            parts = [p.strip() for p in line.split(',')]
            if len(parts) == 3:
                building_name, bd, time = parts
                BUILDINGS.append({
                    "building": building_name,
                    "bd": bd,
                    "time": int(time)
                })
        logger.info(f"Datos de {len(BUILDINGS)} edificios cargados exitosamente desde {BUILDING_DATA_FILE}.")
    except FileNotFoundError:
        logger.error(f"El archivo de datos de edificios '{BUILDING_DATA_FILE}' no se encontró.")
        BUILDINGS = []
    except Exception as e:
        logger.error(f"Error al cargar la data de edificios: {e}", exc_info=True)
        BUILDINGS = []

# Cargar alertas y datetimes al iniciar el bot
alerts = load_alerts()
last_alerted_datetimes = load_last_alerted_datetimes()
load_static_resources()
load_building_data()

# --- Funciones de Utility ---
def escape_markdown_v2(text: str) -> str:
    """
    Escapa caracteres especiales para Telegram MarkdownV2 para evitar errores de parseo.
    Se ha corregido la lógica para que escape correctamente todos los caracteres.
    """
    escape_chars_strict = r'_*[]()~`>#+-=|{}.!'
    for char in escape_chars_strict:
        text = text.replace(char, f'\\{char}')
    return text

def find_building_by_query(query: str) -> list:
    """
    Busca edificios en la lista global BUILDINGS por su 'bd' o por una cadena de consulta en su nombre.
    La búsqueda por nombre es insensible a mayúsculas/minúsculas y tildes.
    Retorna una lista de diccionarios de las coincidencias.
    """
    matches = []
    normalized_query = unidecode(query).lower()
    
    # Búsqueda por BD (identificador)
    for building in BUILDINGS:
        if building['bd'].lower() == normalized_query:
            matches.append(building)
            # Si encuentra una coincidencia exacta por BD, la retorna inmediatamente
            return matches

    # Búsqueda por nombre si no se encontró por BD
    for building in BUILDINGS:
        normalized_name = unidecode(building['building']).lower()
        if normalized_query in normalized_name:
            matches.append(building)
            
    return matches

def calculate_building_time(level: int, base_time: int) -> int:
    """Calcula el tiempo de construcción total basado en el nivel y el tiempo base."""
    if level <= 2:
        return base_time
    else:
        return (level - 1) * base_time

def search_resources_by_query(query: str) -> list:
    """Busca recursos por nombre en la lista estática, ignorando mayúsculas y tildes."""
    matches = []
    normalized_query = unidecode(query).lower()
    for name, resource_id in STATIC_RESOURCES.items():
        normalized_name = unidecode(name).lower()
        if normalized_query in normalized_name:
            matches.append((name, resource_id))
    return matches

# --- Comandos del Bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje de bienvenida cuando se inicia el bot."""
    await update.message.reply_text(
        "¡Hola! Soy tu bot de alertas de precios de SimcoTools.\n"
        "Usa /help para ver los comandos disponibles."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los comandos disponibles."""
    help_message_raw = (
        "Comandos disponibles:\n"
        "**/alert <price objetivo> <resourceId> [quality] [name]**\n"
        "Crea una nueva alerta de precio\n"
        "price objetivo: El precio máximo al que deseas comprar\n"
        "resourceId: El ID del recurso número entero\n"
        "quality (opcional): La calidad mínima del recurso 0-12\n"
        "name (opcional): Un nombre para tu alerta\n\n"
        "**/edit <id> <campo> <nuevo_valor>**\n"
        "Edita una alerta existente por su ID\n"
        "campo: target_price, quality o name\n"
        "nuevo_valor: El nuevo valor para el campo\n\n"
        "**/status**\n"
        "Muestra el estado actual del bot\n\n"
        "**/alerts**\n"
        "Muestra todas tus alertas activas\n\n"
        "**/delete <id1> [id2 ... id5]**\n"
        "Elimina una o varias alertas por sus IDs hasta 5 a la vez\n\n"
        "**/deleteall**\n"
        "Elimina todas las alertas\n"
        "**Sin argumentos**: Elimina **todas tus propias** alertas para usuarios normales\n\n"
        "**/price <resourceId> [quality]**\n"
        "Muestra el precio actual del mercado para un recurso\n\n"
        "**/resource <resourceId> [quality]**\n"
        "Muestra información detallada sobre un recurso y sus precios del último día\n\n"
        "**/findid <nombre_del_recurso>**\n"
        "Busca el ID de un recurso por su nombre mínimo 3 letras, insensible a mayúsculas tildes\n\n"
        "**/bdname <nombre_edificio>**\n"
        "Busca un edificio por su nombre. Mínimo 3 letras, insensible a mayúsculas y tildes.\n\n"
        "**/bdtime <bd/nombre> <nivel> <hora_inicio>**\n"
        "Calcula la hora de finalización de una construcción. El primer parámetro puede ser el 'bd' o el nombre del edificio.\n\n"
        "**/bdstart <bd/nombre> <nivel> <hora_fin>**\n"
        "Calcula la hora de inicio de una construcción. El primer parámetro puede ser el 'bd' o el nombre del edificio.\n\n"
        "**/help**\n"
        "Muestra esta ayuda."
    )
    # Aquí se corrige el problema de la función de escape
    escaped_message = escape_markdown_v2(help_message_raw)
    await update.message.reply_markdown_v2(escaped_message)

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los comandos disponibles solo para administradores."""
    args = context.args
    if not args or len(args) != 1 or args[0] != ADMIN_CODE:
        await update.message.reply_text("Permiso denegado. Para ver los comandos de administrador, usa `/admin_help <código_de_administrador>`.")
        return
    admin_help_message_raw = (
        "Comandos de Administrador:\n\n"
        "**/alerts <admin_code>**\n"
        "Muestra **todas las alertas** activas del bot.\n\n"
        "**/delete <id1> [id2 ... id5] <admin_code>**\n"
        "Elimina una o varias alertas por sus IDs hasta 5 a la vez\n"
        "El `admin_code` debe ser el último argumento para eliminar alertas de *cualquier* usuario\n\n"
        "**/deleteall <admin_code> [user_id]**\n"
        "Elimina todas las alertas del bot.\n"
        "Si se proporciona solo el admin_code: Elimina **todas las alertas del bot** incluyendo las de todos los usuarios\n"
        "Si se proporciona el admin_code y un user_id: Elimina todas las alertas de ese user_id específico."
    )
    escaped_message = escape_markdown_v2(admin_help_message_raw)
    await update.message.reply_markdown_v2(escaped_message)

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Crea una nueva alerta de precio.
    Uso: /alert <price objetivo> <resourceId> [quality] [name]
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/alert 0.5 123 5 MiAlerta` o `/alert 0.5 123`"
        )
        return
    try:
        target_price = float(args[0])
        resource_id = int(args[1])
        quality = None
        name = None
        remaining_args = args[2:]
        if remaining_args:
            try:
                potential_quality = int(remaining_args[0])
                if 0 <= potential_quality <= 12:
                    quality = potential_quality
                    if len(remaining_args) > 1:
                        name = " ".join(remaining_args[1:])
                else:
                    raise ValueError("La calidad debe estar entre 0 y 12.")
            except ValueError:
                name = " ".join(remaining_args)
        alert_id = 1
        if alerts:
            alert_id = max(alert['id'] for alert in alerts) + 1
        new_alert = {
            "id": alert_id,
            "user_id": update.effective_user.id,
            "target_price": target_price,
            "resource_id": resource_id,
            "quality": quality,
            "name": name if name else f"Alerta #{alert_id}"
        }
        alerts.append(new_alert)
        save_alerts(alerts)
        quality_str = f"Quality: {quality}" if quality is not None else "Todas las calidades"
        name_str = f"Nombre: {name}" if name else ""
        await update.message.reply_text(
            f"✅ Alerta creada con éxito:\n\n"
            f"ID: {alert_id}\n"
            f"Resource ID: {resource_id}\n"
            f"Precio Objetivo: {target_price}\n"
            f"{quality_str}\n"
            f"{name_str}"
        )
    except ValueError as e:
        await update.message.reply_text(f"Error en los parámetros: {e}")
    except Exception as e:
        logger.error(f"Error al crear alerta: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al crear la alerta.")

async def edit_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Edita una alerta existente.
    Uso: /edit <id> <campo> <nuevo_valor>
    Campos posibles: target_price, quality, name
    """
    global alerts
    global last_alerted_datetimes
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/edit 1 target_price 0.45` o `/edit 2 name MiNuevaAlerta`"
        )
        return
    try:
        alert_id_to_edit = int(args[0])
        field_to_edit = args[1].lower()
        new_value = " ".join(args[2:])
        user_id = update.effective_user.id
        found_alert = None
        for alert_data in alerts:
            if alert_data['id'] == alert_id_to_edit and alert_data['user_id'] == user_id:
                found_alert = alert_data
                break
        if not found_alert:
            await update.message.reply_text(f"No se encontró una alerta con ID {alert_id_to_edit} o no tienes permiso para editarla.")
            return
        original_value = found_alert.get(field_to_edit, 'N/A')
        if field_to_edit == "target_price":
            try:
                found_alert['target_price'] = float(new_value)
                message = f"Precio objetivo de la alerta ID {alert_id_to_edit} actualizado de {original_value} a {found_alert['target_price']}."
            except ValueError:
                await update.message.reply_text("El precio objetivo debe ser un número válido.")
                return
        elif field_to_edit == "quality":
            try:
                new_quality = int(new_value)
                if not (0 <= new_quality <= 12):
                    await update.message.reply_text("La calidad debe estar entre 0 y 12.")
                    return
                found_alert['quality'] = new_quality
                message = f"Calidad de la alerta ID {alert_id_to_edit} actualizada de {original_value} a {found_alert['quality']}."
            except ValueError:
                await update.message.reply_text("La calidad debe ser un número entero válido.")
                return
        elif field_to_edit == "name":
            found_alert['name'] = new_value
            message = f"Nombre de la alerta ID {alert_id_to_edit} actualizado de '{original_value}' a '{found_alert['name']}'."
        else:
            await update.message.reply_text(f"Campo '{field_to_edit}' no válido para editar. Los campos posibles son: `target_price`, `quality`, `name`.")
            return
        save_alerts(alerts)
        await update.message.reply_text(f"✅ {message}")
    except ValueError as e:
        await update.message.reply_text(f"Error en los parámetros: {e}")
    except Exception as e:
        logger.error(f"Error al editar alerta: {e}")
        await update.message.reply_text("Ocurrió un error al editar la alerta.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el estado actual del bot."""
    num_alerts = len(alerts)
    await update.message.reply_text(
        f"Bot de SimcoTools activo.\n"
        f"Alertas activas: {num_alerts}"
    )

async def show_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Muestra las alertas activas.
    Si se proporciona el ADMIN_CODE, muestra todas las alertas.
    Uso: /alerts [admin_code]
    """
    user_id = update.effective_user.id
    is_admin = False
    if context.args and len(context.args) == 1 and context.args[0] == ADMIN_CODE:
        is_admin = True
        alerts_to_show = alerts
        message_title = "Todas las alertas activas (ADMIN):\n\n"
    else:
        alerts_to_show = [a for a in alerts if a['user_id'] == user_id]
        message_title = "Tus alertas activas:\n\n"
    if not alerts_to_show:
        if is_admin:
            await update.message.reply_text("No hay alertas activas en el bot.")
        else:
            await update.message.reply_text("No tienes alertas activas.")
        return
    try:
        # Se asegura que el título del mensaje esté escapado
        message = escape_markdown_v2(message_title)
        for alert_data in alerts_to_show:
            quality_info = f"Quality >= {alert_data['quality']}" if alert_data['quality'] is not None else "Todas las calidades"
            
            # Se asegura que toda la cadena esté escapada
            user_id_info = escape_markdown_v2(f"User ID: `{alert_data['user_id']}`\n") if is_admin else ""
            
            name_str = escape_markdown_v2(str(alert_data['name']))
            target_price_str = escape_markdown_v2(f"{alert_data['target_price']:.3f}")
            quality_info_str = escape_markdown_v2(quality_info)
            
            message += (
                f"ID: {alert_data['id']}\n"
                f"Nombre: {name_str}\n"
                f"Resource ID: {alert_data['resource_id']}\n"
                f"Precio Objetivo: {target_price_str}\n"
                f"{quality_info_str}\n"
                f"{user_id_info}"
                # Aquí se escapa correctamente la línea de separación
                f"\\-\\-\\-\n"
            )
        await update.message.reply_markdown_v2(message)
    except Exception as e:
        logger.error(f"Error al mostrar alertas: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al intentar mostrar las alertas.")

async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Elimina una o varias alertas por sus IDs.
    Uso: /delete <id1> [id2 ... id5] [admin_code]
    """
    global alerts
    global last_alerted_datetimes
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/delete 1` o `/delete 1 2 3` o `/delete 5 2358`\n"
            "Puedes eliminar hasta 5 alertas a la vez."
        )
        return
    user_id = update.effective_user.id
    is_admin = False
    alert_ids_to_delete = []
    if len(args) > 1 and args[-1] == ADMIN_CODE:
        is_admin = True
        id_args = args[:-1]
    else:
        id_args = args
    if not (1 <= len(id_args) <= 5):
        await update.message.reply_text("Puedes eliminar entre 1 y 5 alertas a la vez.")
        return
    for arg_id in id_args:
        try:
            alert_ids_to_delete.append(int(arg_id))
        except ValueError:
            await update.message.reply_text(f"'{arg_id}' no es un ID de alerta válido. Los IDs deben ser números enteros.")
            return
    deleted_count = 0
    not_found_or_no_permission = []
    initial_alerts_state = list(alerts)
    updated_alerts = []
    for alert_id in alert_ids_to_delete:
        found_and_deleted = False
        for alert_data in initial_alerts_state:
            if alert_data['id'] == alert_id:
                if is_admin or alert_data['user_id'] == user_id:
                    deleted_count += 1
                    found_and_deleted = True
                    alert_key = f"{alert_data['user_id']}-{alert_id}"
                    if alert_key in last_alerted_datetimes:
                        del last_alerted_datetimes[alert_key]
                else:
                    not_found_or_no_permission.append(f"ID {alert_id} (sin permiso)")
                break
        if not found_and_deleted and alert_id not in [a['id'] for a in initial_alerts_state]:
            not_found_or_no_permission.append(f"ID {alert_id} (no encontrada)")
    final_alerts_list = []
    for alert_data in initial_alerts_state:
        if alert_data['id'] in alert_ids_to_delete:
            if is_admin or alert_data['user_id'] == user_id:
                pass
            else:
                final_alerts_list.append(alert_data)
        else:
            final_alerts_list.append(alert_data)
    alerts[:] = final_alerts_list
    save_alerts(alerts)
    save_last_alerted_datetimes(last_alerted_datetimes)
    response_messages = []
    if deleted_count > 0:
        response_messages.append(f"✅ Se eliminaron {deleted_count} alerta(s) con éxito.")
    if not_found_or_no_permission:
        response_messages.append(f"⚠️ Las siguientes alertas no se encontraron o no tienes permiso para eliminarlas: {', '.join(not_found_or_no_permission)}.")
    if not response_messages:
        await update.message.reply_text("No se realizó ninguna eliminación.")
    else:
        await update.message.reply_text("\n".join(response_messages))

async def delete_all_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Elimina todas las alertas del bot o todas las alertas de un usuario específico.
    Uso: /deleteall [admin_code] [user_id]
    """
    global alerts
    global last_alerted_datetimes
    args = context.args
    user_id = update.effective_user.id
    if not args:
        deleted_count = 0
        deleted_alert_ids = set()
        initial_alerts_copy = list(alerts)
        alerts[:] = [
            alert_data for alert_data in initial_alerts_copy
            if alert_data['user_id'] != user_id
        ]
        deleted_count = len(initial_alerts_copy) - len(alerts)
        original_alert_ids_of_user = {a['id'] for a in initial_alerts_copy if a['user_id'] == user_id}
        remaining_alert_ids_of_user = {a['id'] for a in alerts if a['user_id'] == user_id}
        deleted_alert_ids_for_user = original_alert_ids_of_user - remaining_alert_ids_of_user
        if deleted_count > 0:
            save_alerts(alerts)
            keys_to_remove = []
            for key in last_alerted_datetimes:
                parts = key.split('-')
                if len(parts) == 2 and int(parts[0]) == user_id and int(parts[1]) in deleted_alert_ids_for_user:
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                del last_alerted_datetimes[key]
            save_last_alerted_datetimes(last_alerted_datetimes)
            await update.message.reply_text(f"✅ Se eliminaron {deleted_count} alerta(s) tuyas.")
        else:
            await update.message.reply_text("No tienes alertas activas para eliminar.")
        return
    if args[0] != ADMIN_CODE:
        await update.message.reply_text("Permiso denegado. Solo los administradores con el código correcto pueden usar este comando con argumentos.")
        return
    is_admin = True
    try:
        user_id_to_delete_alerts_for = None
        if len(args) == 2:
            try:
                user_id_to_delete_alerts_for = int(args[1])
            except ValueError:
                await update.message.reply_text("El ID de usuario debe ser un número entero válido.")
                return
        initial_len = len(alerts)
        deleted_count = 0
        deleted_alert_ids = set()
        temp_alerts_list = list(alerts)
        if user_id_to_delete_alerts_for is not None:
            alerts[:] = [
                alert_data for alert_data in temp_alerts_list
                if alert_data['user_id'] != user_id_to_delete_alerts_for
            ]
            deleted_count = initial_len - len(alerts)
            deleted_alert_ids.update(
                a['id'] for a in temp_alerts_list
                if a['user_id'] == user_id_to_delete_alerts_for
            )
            message_suffix = f" para el usuario ID {user_id_to_delete_alerts_for}"
        else:
            deleted_count = len(alerts)
            deleted_alert_ids.update(alert['id'] for alert in alerts)
            alerts.clear()
            message_suffix = " del bot"
        if deleted_count > 0:
            save_alerts(alerts)
            keys_to_remove = []
            for key in last_alerted_datetimes:
                parts = key.split('-')
                if len(parts) == 2:
                    try:
                        alert_id_in_key = int(parts[1])
                        if alert_id_in_key in deleted_alert_ids or (user_id_to_delete_alerts_for is None and int(parts[0]) not in {a['user_id'] for a in alerts}):
                            keys_to_remove.append(key)
                        elif user_id_to_delete_alerts_for is not None and int(parts[0]) == user_id_to_delete_alerts_for:
                             keys_to_remove.append(key)
                    except ValueError:
                        pass
            for key in keys_to_remove:
                del last_alerted_datetimes[key]
            save_last_alerted_datetimes(last_alerted_datetimes)
            await update.message.reply_text(f"✅ Se eliminaron {deleted_count} alerta(s){message_suffix}.")
        else:
            if user_id_to_delete_alerts_for:
                await update.message.reply_text(f"No se encontraron alertas para el usuario ID {user_id_to_delete_alerts_for}.")
            else:
                await update.message.reply_text("No hay alertas activas en el bot para eliminar.")
    except Exception as e:
        logger.error(f"Error al eliminar todas las alertas: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al intentar eliminar las alertas.")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Obtiene el precio actual del mercado para un resourceId y opcionalmente quality.
    Uso: /price <resourceId> [quality]
    """
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/price 123` o `/price 123 5`"
        )
        return
    try:
        resource_id = int(args[0])
        quality_filter = None
        if len(args) > 1:
            quality_filter = int(args[1])
            if not (0 <= quality_filter <= 12):
                raise ValueError("La calidad debe estar entre 0 y 12.")
        api_url = f"{SIMCOMPANIES_API_BASE_URL}{resource_id}/"
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url)
            response.raise_for_status()
            market_data = response.json()
            found_prices = []
            for item in market_data:
                if item['kind'] == resource_id:
                    if quality_filter is None or item['quality'] >= quality_filter:
                        found_prices.append(item)
            if found_prices:
                message = f"Precios actuales para Resource ID {resource_id}"
                if quality_filter is not None:
                    message += f" (Quality >= {quality_filter})"
                message += ":\n"
                found_prices.sort(key=lambda x: x['quality'])
                displayed_qualities = set()
                for item in found_prices:
                    if quality_filter is None or item['quality'] >= quality_filter:
                        if item['quality'] not in displayed_qualities:
                            posted_time = datetime.fromisoformat(item['posted'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
                            message += (
                                f"- Quality {item['quality']}: {item['price']} "
                                f"(Cantidad: {item['quantity']:,}, Empresa: {item['seller']['company']}, Publicado: {posted_time})\n"
                            )
                            displayed_qualities.add(item['quality'])
                # La siguiente sección ha sido corregida
                if quality_filter is not None and quality_filter not in displayed_qualities and found_prices:
                    found_exact_or_higher_quality = [
                        item for item in found_prices
                        if item['quality'] >= quality_filter
                    ]
                    if not found_exact_or_higher_quality:
                         await update.message.reply_text(f"No se encontraron precios para Resource ID {resource_id} con calidad >= {quality_filter}.")
                         return
                await update.message.reply_text(message)
            else:
                await update.message.reply_text(f"No se encontraron precios para Resource ID {resource_id}")
    except ValueError as e:
        await update.message.reply_text(f"Error en los parámetros: {e}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await update.message.reply_text(f"Resource ID {resource_id} no encontrado en la API. Por favor, verifica el ID.")
        else:
            await update.message.reply_text(f"Error al obtener datos de la API: {e.response.status_code}")
        logger.error(f"Error HTTP al obtener precios: {e}")
    except Exception as e:
        logger.error(f"Error al obtener precio: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al obtener el precio actual.")

async def get_resource_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Obtiene y muestra información detallada de un recurso usando su resourceId y opcionalmente quality.
    Uso: /resource <resourceId> [quality]
    """
    args = context.args
    if not args or len(args) < 1 or len(args) > 2:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/resource 1` o `/resource 1 0`"
        )
        return
    try:
        resource_id = int(args[0])
        if not (1 <= resource_id <= 200):
            await update.message.reply_text("El `resourceId` debe ser un número entero entre 1 y 200.")
            return
        quality_filter = None
        if len(args) == 2:
            try:
                quality_filter = int(args[1])
                if not (0 <= quality_filter <= 12):
                    raise ValueError("La calidad debe estar entre 0 y 12.")
            except ValueError:
                await update.message.reply_text("La calidad debe ser un número entero entre 0 y 12.")
                return
        full_resource_api_url = f"{RESOURCE_API_BASE_URL}{resource_id}"
        async with httpx.AsyncClient() as client:
            response = await client.get(full_resource_api_url)
            response.raise_for_status()
            data = response.json()
            resource_name = data['resource']['resourceName']
            summaries_by_quality = data['resource']['summariesByQuality']
            message = escape_markdown_v2(f"📊 Información del Recurso: *{resource_name}* (ID: {resource_id})\n")
            if quality_filter is not None:
                message += escape_markdown_v2(f"Para Calidad: {quality_filter}\n\n")
            else:
                message += "\n"
            found_summaries = []
            if summaries_by_quality:
                for summary in summaries_by_quality:
                    if quality_filter is None or summary['quality'] == quality_filter:
                        found_summaries.append(summary)
            if found_summaries:
                for summary in found_summaries:
                    quality = summary['quality']
                    last_day_candlestick = summary.get('lastDayCandlestick')
                    message += escape_markdown_v2(f"➡️ Calidad: `{quality}`\n")
                    if last_day_candlestick:
                        open_price = last_day_candlestick.get('open', 'N/A')
                        low_price = last_day_candlestick.get('low', 'N/A')
                        high_price = last_day_candlestick.get('high', 'N/A')
                        close_price = last_day_candlestick.get('close', 'N/A')
                        volume = last_day_candlestick.get('volume', 'N/A')
                        vwap = last_day_candlestick.get('vwap', 'N/A')
                        open_str = f"{open_price:.3f}" if isinstance(open_price, (int, float)) else str(open_price)
                        low_str = f"{low_price:.3f}" if isinstance(low_price, (int, float)) else str(low_price)
                        high_str = f"{high_price:.3f}" if isinstance(high_price, (int, float)) else str(high_str)
                        close_str = f"{close_price:.3f}" if isinstance(close_price, (int, float)) else str(close_str)
                        volume_str = f"{volume:,}" if isinstance(volume, (int, float)) else str(volume)
                        vwap_str = f"{vwap:.3f}" if isinstance(vwap, (int, float)) else str(vwap)
                        message += escape_markdown_v2(
                            f"  Apertura: {open_str}\n"
                            f"  Mínimo: {low_str}\n"
                            f"  Máximo: {high_str}\n"
                            f"  Cierre: {close_str}\n"
                            f"  Volumen: {volume_str}\n"
                            f"  VWAP: {vwap_str}\n"
                        )
                    else:
                        message += escape_markdown_v2("  Datos del último día no disponibles.\n")
                    message += "\n"
                await update.message.reply_markdown_v2(message)
            else:
                if quality_filter is not None:
                    await update.message.reply_text(f"No se encontraron datos para el Resource ID {resource_id} con calidad {quality_filter}.")
                else:
                    await update.message.reply_text(f"No se encontraron datos de mercado para el Resource ID {resource_id}.")
    except ValueError:
        await update.message.reply_text("El `resourceId` debe ser un número entero válido.")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await update.message.reply_text(f"Resource ID {resource_id} no encontrado en la API. Por favor, verifica el ID.")
        else:
            await update.message.reply_text(f"Error al obtener datos de la API de recursos: {e.response.status_code}")
        logger.error(f"Error HTTP al obtener información del recurso: {e}")
    except KeyError as e:
        await update.message.reply_text(f"Error al procesar los datos del recurso. Faltan datos esperados: {e}")
        logger.error(f"Error de clave en la respuesta de la API de recursos: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Error inesperado al obtener información del recurso: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error inesperado al obtener la información del recurso. Por favor, inténtalo de nuevo más tarde.")

async def find_resource_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Busca el ID de un recurso por su nombre.
    Uso: /findid <nombre_del_recurso>
    Requiere al menos 3 letras, ignora mayúsculas/minúsculas y tildes.
    """
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `/findid <nombre_del_recurso>`\nPor favor, ingresa al menos 3 letras del nombre del recurso.")
        return
    search_query = " ".join(args)
    if len(search_query) < 3:
        await update.message.reply_text("Por favor, ingresa al menos 3 letras para la búsqueda del recurso.")
        return
    if not STATIC_RESOURCES:
        await update.message.reply_text("Lo siento, la lista de recursos estáticos no está disponible. Por favor, informa al administrador del bot.")
        return
    matches = search_resources_by_query(search_query)
    if matches:
        escaped_search_query = escape_markdown_v2(search_query)
        message = f"Coincidencias encontradas para '{escaped_search_query}':\n\n"
        for name, resource_id in matches:
            # CORRECCIÓN: Escapa el nombre antes de agregarlo al mensaje
            escaped_name = escape_markdown_v2(name)
            message += f"\\- **{escaped_name}** \\(ID: `{resource_id}`\\)\n"
        
        if len(matches) > 10:
            message += escape_markdown_v2(f"\nSe encontraron {len(matches)} coincidencias. Mostrando las primeras 10. Por favor, sé más específico.")
            lines = message.split('\n')
            if len(lines) > 13:
                message = '\n'.join(lines[:13]) + "\n" + escape_markdown_v2(f"Se encontraron {len(matches)} coincidencias. Mostrando las primeras 10. Por favor, sé más específico.")
        
        await update.message.reply_markdown_v2(message)
    else:
        await update.message.reply_text(f"No se encontraron recursos que coincidan con '{search_query}'.")

# --- Nuevos Comandos de Edificios ---

async def bdname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Busca el nombre de un edificio y su BD.
    Uso: /bdname <nombre_edificio>
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/bdname fabrica`\n"
            "Por favor, ingresa al menos 3 letras del nombre del edificio."
        )
        return
    
    query = " ".join(args)
    if len(query) < 3:
        await update.message.reply_text(
            "La búsqueda requiere un mínimo de 3 letras."
        )
        return

    matches = find_building_by_query(query)
    
    if not matches:
        await update.message.reply_text(
            f"No se encontraron coincidencias para '{query}'. Por favor, intenta de nuevo."
        )
        return
    
    message = f"Coincidencias encontradas para '{query}':\n\n"
    for building in matches:
        message += f"- {building['building']}, bd: {building['bd']}\n"
    
    await update.message.reply_text(message)

async def bdtime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Calcula el tiempo de finalización de una construcción.
    Uso: /bdtime <bd/nombre> <nivel> <hora_inicio>
    """
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/bdtime 6 2 17:00` o `/bdtime fabrica de bebidas 2 17:00`"
        )
        return
    
    try:
        level = int(args[-2])
        start_time_str = args[-1]
        query = " ".join(args[:-2])
        
        if not re.match(r'^\d{1,2}:\d{2}$', start_time_str):
            await update.message.reply_text("El formato de la hora de inicio es incorrecto. Debe ser HH:MM (e.g., 17:00).")
            return
            
        start_hour, start_minute = map(int, start_time_str.split(':'))
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
            await update.message.reply_text("La hora de inicio no es válida.")
            return

        matches = find_building_by_query(query)
        if not matches:
            await update.message.reply_text(f"No se encontró un edificio que coincida con '{query}'.")
            return
        
        if len(matches) > 1:
            await update.message.reply_text(
                "Existe más de una coincidencia para la búsqueda. Por favor, sé más específico."
            )
            return
            
        building = matches[0]
        base_time = building['time']
        
        total_time = calculate_building_time(level, base_time)
        
        current_time = datetime.now()
        start_datetime = current_time.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end_datetime = start_datetime + timedelta(hours=total_time)
        
        day_str = "hoy"
        if end_datetime.date() > current_time.date():
            day_str = end_datetime.strftime("%A").lower()
        
        message = (
            f"{building['building']}\n"
            f"bd: {building['bd']}\n"
            f"nivel: {level}\n"
            f"tiempo: {total_time}hr\n"
            f"finaliza: {day_str} {end_datetime.strftime('%H:%M')}"
        )
        
        await update.message.reply_text(message)

    except (ValueError, IndexError):
        await update.message.reply_text("Uso incorrecto. Asegúrate de proporcionar el bd/nombre, nivel (número entero) y hora de inicio (HH:MM).")
    except Exception as e:
        logger.error(f"Error en el comando bdtime: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al procesar tu solicitud.")

async def bdstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Calcula el tiempo de inicio de una construcción.
    Uso: /bdstart <bd/nombre> <nivel> <hora_fin>
    """
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/bdstart 1 3 11:00` o `/bdstart fabrica de automotores 3 11:00`"
        )
        return
        
    try:
        level = int(args[-2])
        end_time_str = args[-1]
        query = " ".join(args[:-2])

        if not re.match(r'^\d{1,2}:\d{2}$', end_time_str):
            await update.message.reply_text("El formato de la hora de finalización es incorrecto. Debe ser HH:MM (e.g., 11:00).")
            return

        end_hour, end_minute = map(int, end_time_str.split(':'))
        if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
            await update.message.reply_text("La hora de finalización no es válida.")
            return

        matches = find_building_by_query(query)
        if not matches:
            await update.message.reply_text(f"No se encontró un edificio que coincida con '{query}'.")
            return
        
        if len(matches) > 1:
            await update.message.reply_text(
                "Existe más de una coincidencia para la búsqueda. Por favor, sé más específico."
            )
            return

        building = matches[0]
        base_time = building['time']
        
        total_time = calculate_building_time(level, base_time)
        
        end_datetime = datetime.now().replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        start_datetime = end_datetime - timedelta(hours=total_time)
        
        message = (
            f"{building['building']}\n"
            f"bd: {building['bd']}\n"
            f"nivel: {level}\n"
            f"tiempo: {total_time}hr\n"
            f"iniciar: {start_datetime.strftime('%H:%M')}"
        )
        
        await update.message.reply_text(message)

    except (ValueError, IndexError):
        await update.message.reply_text("Uso incorrecto. Asegúrate de proporcionar el bd/nombre, nivel (número entero) y hora de finalización (HH:MM).")
    except Exception as e:
        logger.error(f"Error en el comando bdstart: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al procesar tu solicitud.")

# --- Lógica de Verificación de Alertas (Job del Bot) ---
async def check_prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Función que se ejecuta cada 30 segundos para verificar los precios."""
    logger.info("Iniciando verificación de precios...")
    if not alerts:
        logger.info("No hay alertas activas para verificar.")
        return
    try:
        async with httpx.AsyncClient() as client:
            for alert_data in list(alerts):
                user_id = alert_data['user_id']
                alert_id = alert_data['id']
                target_price = alert_data['target_price']
                resource_id = alert_data['resource_id']
                quality_filter = alert_data['quality']
                alert_name = alert_data['name']
                alert_key = f"{user_id}-{alert_id}"
                api_url = f"{SIMCOMPANIES_API_BASE_URL}{resource_id}/"
                try:
                    response = await client.get(api_url)
                    response.raise_for_status()
                    market_data = response.json()
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.warning(f"Resource ID {resource_id} no encontrado en la API para la alerta {alert_id}. Se saltará esta alerta.")
                        continue
                    else:
                        logger.error(f"Error HTTP al obtener precios para Resource ID {resource_id}: {e}")
                        continue
                except Exception as e:
                    logger.error(f"Error inesperado al obtener precios para Resource ID {resource_id}: {e}", exc_info=True)
                    continue
                best_offer = None
                for item in market_data:
                    if item['kind'] == resource_id:
                        if quality_filter is None or item['quality'] >= quality_filter:
                            best_offer = item
                            break
                if best_offer:
                    current_price = best_offer['price']
                    current_posted_str = best_offer['posted']
                    current_posted = datetime.fromisoformat(current_posted_str.replace('Z', '+00:00'))
                    last_alert_posted_str = last_alerted_datetimes.get(alert_key)
                    last_alert_posted = None
                    if last_alert_posted_str:
                        last_alert_posted = datetime.fromisoformat(last_alert_posted_str.replace('Z', '+00:00'))
                    if current_price <= target_price:
                        if last_alert_posted is None or current_posted > last_alert_posted:
                            message_raw = (
                                f"🚨 ¡ALERTA DE PRECIO! 🚨\n\n"
                                f"Alerta: {alert_name}\n"
                                f"Resource ID: {resource_id}\n"
                                f"Calidad: {best_offer['quality']}\n"
                                f"Precio Actual: {current_price} (Objetivo: {target_price})\n"
                                f"Cantidad: {best_offer['quantity']:,}\n"
                                f"Empresa: {best_offer['seller']['company']}\n"
                                f"Última publicación: {current_posted.strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                            await context.bot.send_message(chat_id=user_id, text=escape_markdown_v2(message_raw), parse_mode="MarkdownV2")
                            last_alerted_datetimes[alert_key] = current_posted_str
                            save_last_alerted_datetimes(last_alerted_datetimes)
                else:
                    pass
    except Exception as e:
        logger.error(f"Error general en la verificación de precios: {e}", exc_info=True)

def main() -> None:
    """Función principal para ejecutar el bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin_help", admin_help))
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("edit", edit_alert))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("alerts", show_alerts))
    application.add_handler(CommandHandler("delete", delete_alert))
    application.add_handler(CommandHandler("deleteall", delete_all_alerts))
    application.add_handler(CommandHandler("price", get_price))
    application.add_handler(CommandHandler("resource", get_resource_info))
    application.add_handler(CommandHandler("findid", find_resource_id))
    
    application.add_handler(CommandHandler("bdname", bdname))
    application.add_handler(CommandHandler("bdtime", bdtime))
    application.add_handler(CommandHandler("bdstart", bdstart))

    job_queue: JobQueue = application.job_queue
    job_queue.run_repeating(check_prices_job, interval=310, first=10)
    logger.info("Bot de SimcoTools iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
