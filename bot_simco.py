import os
import logging
import json
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue
)
import httpx
from unidecode import unidecode # Importar la librería para manejar tildes

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
ADMIN_CODE = "2358" # El código que los administradores deben usar

API_URL = "https://api.simcotools.com/v1/realms/0/market/prices"
RESOURCE_API_BASE_URL = "https://api.simcotools.com/v1/realms/0/market/resources/"
ALERTS_FILE = "alerts.json"
LAST_ALERTED_DATETIMES_FILE = "last_alerted_datetimes.json"
STATIC_RESOURCES_FILE = "recursos_estaticos.json" # Archivo JSON con la lista estática

# Diccionario para almacenar los recursos estáticos (nombre -> ID)
STATIC_RESOURCES = {}

# --- Funciones de Utilidad para Persistencia ---

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
    """Carga los últimos datetimes alertados desde el archivo JSON."""
    try:
        with open(LAST_ALERTED_DATETIMES_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_last_alerted_datetimes(datetimes):
    """Guarda los últimos datetimes alertados en el archivo JSON."""
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
        STATIC_RESOURCES = {} # Reset to empty to prevent issues
    except Exception as e:
        logger.error(f"Error inesperado al cargar recursos estáticos: {e}", exc_info=True)
        STATIC_RESOURCES = {} # Reset to empty to prevent issues


# Cargar alertas y datetimes al iniciar el bot
alerts = load_alerts()
last_alerted_datetimes = load_last_alerted_datetimes()
load_static_resources() # Cargar los recursos estáticos al inicio del bot

# --- Helper function for MarkdownV2 escaping (used only where truly needed) ---
def escape_markdown_v2(text: str) -> str:
    """Escapa caracteres especiales para Telegram MarkdownV2 que podrían romper el formato."""
    escape_chars_strict = r'_*[]()~`>#+-=|{}.!' 
    translator = str.maketrans({char: '\\' + char for char in escape_chars_strict})
    return text.translate(translator)

# --- Nueva función de búsqueda de recursos ---
def search_resources_by_query(query: str) -> list[tuple[str, int]]:
    """
    Busca recursos en STATIC_RESOURCES por una cadena de consulta.
    Ignora mayúsculas/minúsculas y tildes.
    Retorna una lista de tuplas (nombre, id) de las coincidencias.
    """
    matches = []
    # Normaliza la consulta de búsqueda: a minúsculas y sin tildes
    normalized_query = unidecode(query).lower()

    for name, resource_id in STATIC_RESOURCES.items():
        # Normaliza el nombre del recurso de la lista: a minúsculas y sin tildes
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

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los comandos disponibles."""
    help_message = (
        "Comandos disponibles:\n"
        "**/alert \\<price objetivo\\> \\<resourceId\\> \\[quality\\] \\[name\\]**\n"
        "\\- Crea una nueva alerta de precio\\.\n"
        "\\- \\`price objetivo\\`: El precio máximo al que deseas comprar\\.\n"
        "\\- \\`resourceId\\`: El ID del recurso \\(número entero\\)\\.\n"
        "\\- \\`quality\\` \\(opcional\\): La calidad mínima del recurso \\(0\\-12\\)\\.\n"
        "\\- \\`name\\` \\(opcional\\): Un nombre para tu alerta\\.\n\n"
        "**/edit \\<id\\> \\<campo\\> \\<nuevo_valor\\>**\n"
        "\\- Edita una alerta existente por su ID\\.\n"
        "\\- \\`campo\\`: `target_price`, `quality` o `name`\\.\n"
        "\\- \\`nuevo_valor\\`: El nuevo valor para el campo\\.\n\n"
        "**/status**\n"
        "\\- Muestra el estado actual del bot\\.\n\n"
        "**/alerts \\[admin_code\\]**\n"
        "\\- Muestra todas tus alertas activas\\.\n"
        "\\- Si eres administrador y usas el `admin_code`, muestra todas las alertas del bot\\.\n\n"
        "**/delete \\<id\\> \\[admin_code\\]**\n"
        "\\- Elimina una alerta por su ID\\.\n"
        "\\- Si eres administrador y usas el `admin_code`, puedes eliminar la alerta de cualquier usuario\\.\n\n"
        "**/deleteall \\[admin_code\\] \\[user_id\\]**\n"
        "\\- \\(Solo Admin\\) Elimina todas las alertas del bot\\.\n"
        "\\- Si se proporciona un `user_id`, elimina todas las alertas de ese usuario\\.\n\n"
        "**/price \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra el precio actual del mercado para un recurso\\.\n\n"
        "**/resource \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra información detallada sobre un recurso y sus precios del último día\\.\n\n"
        "**/findid \\<nombre_del_recurso\\>**\n"
        "\\- Busca el ID de un recurso por su nombre \\(mínimo 3 letras, insensible a mayúsculas/tildes\\)\\.\n\n"
        "**/help**\n"
        "\\- Muestra esta ayuda\\."
    )
    await update.message.reply_markdown_v2(help_message)

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

        remaining_args = args[2:] # Obtener todos los argumentos después de resourceId

        if remaining_args:
            # Intentar analizar el primer argumento restante como calidad
            try:
                potential_quality = int(remaining_args[0])
                if 0 <= potential_quality <= 12:
                    quality = potential_quality
                    # Si era una calidad válida, el resto es el nombre
                    if len(remaining_args) > 1:
                        name = " ".join(remaining_args[1:])
                else:
                    # Si es un entero pero fuera de rango, generar un error explícito
                    raise ValueError("La calidad debe estar entre 0 y 12.")
            except ValueError:
                # Si remaining_args[0] no se puede convertir a int (o si la calidad estaba fuera de rango
                # y se relanzó ValueError), entonces este argumento (y el resto) es el nombre
                name = " ".join(remaining_args)

        # Asignar un ID único a la alerta
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
        logger.error(f"Error al crear alerta: {e}", exc_info=True) # Añadido exc_info
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

    # Check for admin code
    if context.args and len(context.args) == 1 and context.args[0] == ADMIN_CODE:
        is_admin = True
        alerts_to_show = alerts # Show all alerts
        message_title = "Todas las alertas activas (ADMIN):\n\n"
    else:
        alerts_to_show = [a for a in alerts if a['user_id'] == user_id] # Show user's alerts
        message_title = "Tus alertas activas:\n\n"

    if not alerts_to_show:
        if is_admin:
            await update.message.reply_text("No hay alertas activas en el bot.")
        else:
            await update.message.reply_text("No tienes alertas activas.")
        return

    try: # Added try-except for this function as well
        # Escape the title as well, just in case it contains special markdown characters
        message = escape_markdown_v2(message_title)
        for alert_data in alerts_to_show:
            quality_info = f"Quality >= {alert_data['quality']}" if alert_data['quality'] is not None else "Todas las calidades"
            user_id_info = f"User ID: `{alert_data['user_id']}`\n" if is_admin else ""

            # Escape specific parts for MarkdownV2
            name_str = escape_markdown_v2(str(alert_data['name'])) # User-provided name
            target_price_str = escape_markdown_v2(f"{alert_data['target_price']:.3f}") # Price value
            quality_info_str = escape_markdown_v2(quality_info) # Quality text, could have '>'

            message += (
                f"ID: {alert_data['id']}\n" # ID is an integer, usually fine
                f"Nombre: {name_str}\n"
                f"Resource ID: {alert_data['resource_id']}\n" # Resource ID is an integer, usually fine
                f"Precio Objetivo: {target_price_str}\n"
                f"{quality_info_str}\n"
                f"{user_id_info}"
                f"\\-\\-\\-\\n" # Explicitly escape '---' to be literal text, not a Markdown rule
            )
        await update.message.reply_markdown_v2(message)
    except Exception as e:
        logger.error(f"Error al mostrar alertas: {e}", exc_info=True)
        await update.message.reply_text("Ocurrió un error al intentar mostrar las alertas.")


async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Elimina una alerta por su ID.
    Si se proporciona el ADMIN_CODE, un administrador puede eliminar cualquier alerta.
    Uso: /delete <id> [admin_code]
    """

    global alerts
    global last_alerted_datetimes

    args = context.args
    if not args or len(args) < 1 or len(args) > 2:
        await update.message.reply_text("Uso incorrecto. Ejemplo: `/delete 1` o `/delete 1 2358`")
        return

    try:
        alert_id_to_delete = int(args[0])
        user_id = update.effective_user.id
        is_admin = False

        # Check for admin code as the second argument
        if len(args) == 2 and args[1] == ADMIN_CODE:
            is_admin = True
            
        initial_len = len(alerts)
        
        # Filter alerts based on admin status or user_id
        if is_admin:
            # Admin can delete any alert by ID
            alerts[:] = [
                alert_data for alert_data in alerts
                if not (alert_data['id'] == alert_id_to_delete)
            ]
        else:
            # Non-admin can only delete their own alerts
            alerts[:] = [
                alert_data for alert_data in alerts
                if not (alert_data['id'] == alert_id_to_delete and alert_data['user_id'] == user_id)
            ]
        
        if len(alerts) < initial_len:
            save_alerts(alerts)
            # Find the alert key to delete from last_alerted_datetimes
            # This requires knowing the original user_id of the deleted alert
            # A simpler approach for admin is to clear all related entries for that alert_id
            keys_to_delete = [key for key in last_alerted_datetimes if key.endswith(f"-{alert_id_to_delete}")]
            for key in keys_to_delete:
                del last_alerted_datetimes[key]
            save_last_alerted_datetimes(last_alerted_datetimes)

            await update.message.reply_text(f"✅ Alerta con ID {alert_id_to_delete} eliminada{' (por administrador)' if is_admin else ''}.")
        else:
            await update.message.reply_text(f"No se encontró una alerta con ID {alert_id_to_delete} o no tienes permiso para eliminarla.")
    except ValueError:
        await update.message.reply_text("El ID o el código de administrador debe ser un número válido.")
    except Exception as e:
        logger.error(f"Error al eliminar alerta: {e}")
        await update.message.reply_text("Ocurrió un error al eliminar la alerta.")

async def delete_all_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (Solo Admin) Elimina todas las alertas del bot o todas las alertas de un usuario específico.
    Uso: /deleteall [admin_code] [user_id]
    """
    global alerts
    global last_alerted_datetimes

    args = context.args
    
    if not args or len(args) < 1 or args[0] != ADMIN_CODE:
        await update.message.reply_text("Permiso denegado. Solo los administradores con el código correcto pueden usar este comando.")
        return
    
    is_admin = True # We already checked ADMIN_CODE

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

        if user_id_to_delete_alerts_for is not None:
            # Delete alerts for a specific user
            original_alerts_len = len(alerts)
            alerts[:] = [
                alert_data for alert_data in alerts
                if alert_data['user_id'] != user_id_to_delete_alerts_for
            ]
            deleted_count = original_alerts_len - len(alerts)
            for alert_data in alerts: # Find IDs of deleted alerts
                if alert_data['user_id'] == user_id_to_delete_alerts_for:
                    deleted_alert_ids.add(alert_data['id'])
            
            message_suffix = f" para el usuario ID {user_id_to_delete_alerts_for}"
        else:
            # Delete all alerts
            deleted_count = len(alerts)
            deleted_alert_ids.update(alert['id'] for alert in alerts)
            alerts.clear()
            message_suffix = " del bot"
        
        if deleted_count > 0:
            save_alerts(alerts)
            
            # Clear associated last_alerted_datetimes
            keys_to_remove = []
            for key in last_alerted_datetimes:
                # Key format: "user_id-alert_id"
                parts = key.split('-')
                if len(parts) == 2:
                    try:
                        alert_id_in_key = int(parts[1])
                        if alert_id_in_key in deleted_alert_ids or user_id_to_delete_alerts_for is None:
                            keys_to_remove.append(key)
                        elif user_id_to_delete_alerts_for is not None and int(parts[0]) == user_id_to_delete_alerts_for:
                             keys_to_remove.append(key)
                    except ValueError:
                        # Handle malformed keys if any
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

        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL)
            response.raise_for_status()
            data = response.json()

            found_prices = []
            for item in data['prices']:
                if item['resourceId'] == resource_id:
                    if quality_filter is None or item['quality'] >= quality_filter:
                        found_prices.append(item)

            if found_prices:
                message = f"Precios actuales para Resource ID {resource_id}"
                if quality_filter is not None:
                    message += f" (Quality >= {quality_filter})"
                message += ":\n"

                # Ordenar por quality para una mejor visualización
                found_prices.sort(key=lambda x: x['quality'])

                for item in found_prices:
                    message += f"- Quality {item['quality']}: {item['price']} (última actualización: {datetime.fromisoformat(item['datetime'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')})\n"
                await update.message.reply_text(message)
            else:
                await update.message.reply_text(f"No se encontraron precios para Resource ID {resource_id}")

    except ValueError as e:
        await update.message.reply_text(f"Error en los parámetros: {e}")
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"Error al obtener datos de la API: {e}")
        logger.error(f"Error HTTP al obtener precios: {e}")
    except Exception as e:
        logger.error(f"Error al obtener precio: {e}")
        await update.message.reply_text("Ocurrió un error al obtener el precio actual.")

# --- Nueva función para el comando /resource ---
async def get_resource_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Obtiene y muestra información detallada de un recurso usando su resourceId y opcionalmente quality.
    Uso: /resource <resourceId> [quality]
    """
    args = context.args
    if not args or len(args) < 1 or len(args) > 2: # Allow 1 or 2 arguments
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
            response.raise_for_status() # Lanza una excepción si la respuesta no es 2xx
            data = response.json()

            resource_name = data['resource']['resourceName']
            summaries_by_quality = data['resource']['summariesByQuality']

            # Escapar los paréntesis en el mensaje inicial
            message = f"📊 Información del Recurso: *{resource_name}* \\(ID: `{resource_id}`\\)\n"
            if quality_filter is not None:
                message += f"Para Calidad: `{quality_filter}`\n\n"
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

                    message += f"➡️ Calidad: `{quality}`\n"
                    if last_day_candlestick:
                        open_price = last_day_candlestick.get('open', 'N/A')
                        low_price = last_day_candlestick.get('low', 'N/A')
                        high_price = last_day_candlestick.get('high', 'N/A')
                        close_price = last_day_candlestick.get('close', 'N/A')
                        volume = last_day_candlestick.get('volume', 'N/A')
                        vwap = last_day_candlestick.get('vwap', 'N/A')

                        # Formatear números solo si no son 'N/A'
                        open_str = f"{open_price:.3f}" if isinstance(open_price, (int, float)) else str(open_price)
                        low_str = f"{low_price:.3f}" if isinstance(low_price, (int, float)) else str(low_price)
                        high_str = f"{high_price:.3f}" if isinstance(high_price, (int, float)) else str(high_price)
                        close_str = f"{close_price:.3f}" if isinstance(close_price, (int, float)) else str(close_price)
                        volume_str = f"{volume:,}" if isinstance(volume, (int, float)) else str(volume)
                        vwap_str = f"{vwap:.3f}" if isinstance(vwap, (int, float)) else str(vwap)

                        # Escapar solo los caracteres que Telegram MarkdownV2 podría interpretar como sintaxis
                        # Los puntos y guiones en números y fechas no necesitan ser escapados aquí
                        # porque están dentro de un contexto de texto normal.
                        message += (
                            f"  Apertura: `{open_str}`\n"
                            f"  Mínimo: `{low_str}`\n"
                            f"  Máximo: `{high_str}`\n"
                            f"  Cierre: `{close_str}`\n"
                            f"  Volumen: `{volume_str}`\n"
                            f"  VWAP: `{vwap_str}`\n"
                        ).replace('.', '\\.').replace('-', '\\-').replace(',', '\\,') # Re-applying specific escapes here for safety.


                    else:
                        message += "  Datos del último día no disponibles.\n"
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
    
    # MODIFICACIÓN AQUÍ: Cambiado de 4 a 3
    if len(search_query) < 3:
        await update.message.reply_text("Por favor, ingresa al menos 3 letras para la búsqueda del recurso.")
        return

    if not STATIC_RESOURCES:
        await update.message.reply_text("Lo siento, la lista de recursos estáticos no está disponible. Por favor, informa al administrador del bot.")
        return

    matches = search_resources_by_query(search_query)

    if matches:
        message = f"Coincidencias encontradas para '{search_query}':\n\n"
        for name, resource_id in matches:
            message += f"- *{escape_markdown_v2(name)}* \\(ID: `{resource_id}`\\)\n"
        
        # Limitar el número de resultados para evitar mensajes muy largos en Telegram
        if len(matches) > 10: # Si hay más de 10 coincidencias
            message += escape_markdown_v2(f"\nSe encontraron {len(matches)} coincidencias. Mostrando las primeras 10. Por favor, sé más específico.")
            message_parts = message.split('\n')
            message = '\n'.join(message_parts[:13]) # Limita a las primeras 10 entradas de la lista + encabezados

        await update.message.reply_markdown_v2(message)
    else:
        await update.message.reply_text(f"No se encontraron recursos que coincidan con '{search_query}'.")


# --- Lógica de Verificación de Alertas (Job del Bot) ---

async def check_prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Función que se ejecuta cada 30 segundos para verificar los precios."""
    logger.info("Iniciando verificación de precios...")
    if not alerts:
        logger.info("No hay alertas activas para verificar.")
        return

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL)
            response.raise_for_status()
            market_data = response.json()['prices']

            current_time = datetime.now()

            for alert_data in list(alerts):  # Usar una copia para evitar problemas si se elimina una alerta
                user_id = alert_data['user_id']
                alert_id = alert_data['id']
                target_price = alert_data['target_price']
                resource_id = alert_data['resource_id']
                quality_filter = alert_data['quality']
                alert_name = alert_data['name']

                alert_sent = False
                alert_key = f"{user_id}-{alert_id}"

                for item in market_data:
                    if item['resourceId'] == resource_id:
                        # Filtrar por calidad si se especificó
                        if quality_filter is not None and item['quality'] < quality_filter:
                            continue # Saltar si la calidad es menor a la solicitada

                        current_price = item['price']
                        item_datetime_str = item['datetime']

                        # Convertir a datetime con conciencia de zona horaria (UTC)
                        item_datetime = datetime.fromisoformat(item_datetime_str.replace('Z', '+00:00'))

                        last_alert_datetime_str = last_alerted_datetimes.get(alert_key)
                        last_alert_datetime = None
                        if last_alert_datetime_str:
                            last_alert_datetime = datetime.fromisoformat(last_alert_datetime_str.replace('Z', '+00:00'))

                        if current_price <= target_price:
                            # Alerta si el precio es menor o igual
                            # Y si el datetime ha cambiado o es la primera vez que se cumple la condición
                            if last_alert_datetime is None or item_datetime > last_alert_datetime:
                                message = (
                                    f"🚨 ¡ALERTA DE PRECIO! 🚨\n\n"
                                    f"Alerta: {alert_name}\n"
                                    f"Resource ID: {resource_id}\n"
                                    f"Calidad: {item['quality']}\n"
                                    f"Precio Actual: {current_price} (Objetivo: {target_price})\n"
                                    f"Última actualización: {item_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                                )
                                # Send as plain text. No MarkdownV2 escaping needed for send_message.
                                await context.bot.send_message(chat_id=user_id, text=message)

                                # Actualizar el datetime de la última alerta
                                last_alerted_datetimes[alert_key] = item_datetime_str
                                save_last_alerted_datetimes(last_alerted_datetimes)
                                alert_sent = True
                                break # Una alerta por cada recurso que cumpla la condición es suficiente

                if not alert_sent and alert_key in last_alerted_datetimes:
                    pass

    except httpx.HTTPStatusError as e:
        logger.error(f"Error HTTP al verificar precios: {e}")
    except Exception as e:
        logger.error(f"Error en la verificación de precios: {e}", exc_info=True)

def main() -> None:
    """Función principal para ejecutar el bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Añadir manejadores de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("edit", edit_alert)) # Nuevo manejador
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("alerts", show_alerts))
    application.add_handler(CommandHandler("delete", delete_alert))
    application.add_handler(CommandHandler("deleteall", delete_all_alerts)) # Nuevo manejador
    application.add_handler(CommandHandler("price", get_price))
    application.add_handler(CommandHandler("resource", get_resource_info))
    application.add_handler(CommandHandler("findid", find_resource_id)) # Nuevo manejador para buscar ID


    # Configurar el Job Queue para la verificación de precios
    job_queue: JobQueue = application.job_queue
    # MODIFICACIÓN AQUÍ: Cambiado de 60 a 30 segundos
    job_queue.run_repeating(check_prices_job, interval=30, first=10) # Se ejecuta cada 30 segundos, empieza después de 10 segundos

    # Iniciar el bot
    logger.info("Bot de SimcoTools iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
