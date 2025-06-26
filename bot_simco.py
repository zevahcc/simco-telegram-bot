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
ADMIN_CODE = "e2358e" # El código que los administradores deben usar

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
    """Muestra los comandos disponibles para usuarios generales."""
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
        "**/alerts**\n"
        "\\- Muestra todas tus alertas activas\\.\n\n"
        "**/delete \\<id1\\> \\[id2 ... id5\\]**\n"
        "\\- Elimina una o varias alertas por sus IDs \\(hasta 5 a la vez\\)\\.\n\n"
        "**/deleteall**\n"
        "\\- Elimina **todas tus propias** alertas\\.\n\n"
        "**/price \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra el precio actual del mercado para un recurso\\.\n\n"
        "**/resource \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra información detallada sobre un recurso y sus precios del último día\\.\n\n"
        "**/findid \\<nombre_del_recurso\\>**\n"
        "\\- Busca el ID de un recurso por su nombre \\(mínimo 3 letras, insensible a mayúsculas/tildes\\)\\.\n\n"
        "**/help**\n"
        "\\- Muestra esta ayuda\\.\n\n"
        "Para comandos de administrador, usa /admin_help con el código de administrador." # Hint for admin help
    )
    await update.message.reply_markdown_v2(help_message)


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los comandos disponibles solo para administradores."""
    args = context.args
    if not args or len(args) != 1 or args[0] != ADMIN_CODE:
        await update.message.reply_text("Permiso denegado. Para ver los comandos de administrador, usa `/admin_help <código_de_administrador>`.")
        return

    admin_help_message = (
        "Comandos de Administrador:\n\n"
        "**/alerts \\<admin_code\\>**\n"
        "\\- Muestra **todas las alertas** activas del bot\\.\n\n"
        "**/delete \\<id1\\> \\[id2 ... id5\\] \\<admin_code\\>**\n"
        "\\- Elimina una o varias alertas por sus IDs \\(hasta 5 a la vez\\)\\.\n"
        "\\- El `admin_code` debe ser el último argumento para eliminar alertas de *cualquier* usuario\\.\n\n"
        "**/deleteall \\<admin_code\\> \\[user_id\\]**\n"
        "\\- Elimina todas las alertas del bot\\.\n"
        "\\- Si se proporciona solo el `admin_code`: Elimina **todas las alertas del bot** \\(incluyendo las de todos los usuarios\\)\\.\n"
        "\\- Si se proporciona el `admin_code` y un `user_id`: Elimina todas las alertas de ese `user_id` específico\\.\n"
    )
    await update.message.reply_markdown_v2(admin_help_message)


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
    Elimina una o varias alertas por sus IDs.
    Uso: /delete <id1> [id2 ... id5] [admin_code]
    """
    global alerts
    global last_alerted_datetimes

    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso incorrecto. Ejemplo: `/delete 1` o `/delete 1 2 3` o `/delete 5 e2358e`\n"
            "Puedes eliminar hasta 5 alertas a la vez."
        )
        return

    user_id = update.effective_user.id
    is_admin = False
    alert_ids_to_delete = []
    
    # Check if the last argument is the admin code
    if len(args) > 1 and args[-1] == ADMIN_CODE:
        is_admin = True
        # If admin code is provided, the IDs are all arguments except the last one
        id_args = args[:-1]
    else:
        # Otherwise, all arguments are assumed to be IDs
        id_args = args

    # Validate number of IDs
    if not (1 <= len(id_args) <= 5):
        await update.message.reply_text("Puedes eliminar entre 1 y 5 alertas a la vez.")
        return

    # Parse alert IDs
    for arg_id in id_args:
        try:
            alert_ids_to_delete.append(int(arg_id))
        except ValueError:
            await update.message.reply_text(f"'{arg_id}' no es un ID de alerta válido. Los IDs deben ser números enteros.")
            return

    deleted_count = 0
    not_found_or_no_permission = []
    
    initial_alerts_state = list(alerts) # Create a copy to iterate and compare against
    
    # Build the new alerts list by filtering out the ones to be deleted
    final_alerts_list = []
    for alert_data in initial_alerts_state:
        if alert_data['id'] in alert_ids_to_delete:
            if is_admin or alert_data['user_id'] == user_id:
                # This alert will be deleted, do not add it to final_alerts_list
                deleted_count += 1
                # Remove from last_alerted_datetimes for this specific alert_id and user_id
                alert_key = f"{alert_data['user_id']}-{alert_data['id']}"
                if alert_key in last_alerted_datetimes:
                    del last_alerted_datetimes[alert_key]
            else:
                # This alert cannot be deleted by this user, keep it in the list
                final_alerts_list.append(alert_data)
                not_found_or_no_permission.append(f"ID {alert_data['id']} (sin permiso)")
        else:
            # This alert was not targeted for deletion, keep it
            final_alerts_list.append(alert_data)
            
    # Check for IDs requested that were not found at all
    for requested_id in alert_ids_to_delete:
        if requested_id not in [a['id'] for a in initial_alerts_state]:
            not_found_or_no_permission.append(f"ID {requested_id} (no encontrada)")

    alerts[:] = final_alerts_list # Update the global list with the filtered list

    save_alerts(alerts)
    save_last_alerted_datetimes(last_alerted_datetimes) # Save changes to last alerted datetimes

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
    
    is_admin_request = False
    target_user_id_for_admin = None

    # Determine if it's an admin request and if a specific user_id is targeted
    if args and args[0] == ADMIN_CODE:
        is_admin_request = True
        if len(args) == 2:
            try:
                target_user_id_for_admin = int(args[1])
            except ValueError:
                await update.message.reply_text("El ID de usuario para eliminar debe ser un número entero válido.")
                return
    elif args: # Arguments provided but not ADMIN_CODE first
        await update.message.reply_text("Permiso denegado. Solo los administradores con el código correcto pueden usar argumentos con este comando.")
        return
    
    # Logic for deletion
    deleted_count = 0
    deleted_alert_ids = set() # To track IDs of alerts actually deleted
    initial_alerts_state = list(alerts) # Create a copy to work with

    if is_admin_request:
        if target_user_id_for_admin is not None:
            # Admin deletes all alerts for a specific user_id
            # Corrected logic to ensure only target_user_id's alerts are removed
            alerts[:] = [
                alert_data for alert_data in initial_alerts_state
                if alert_data['user_id'] != target_user_id_for_admin
            ]
            deleted_count = len(initial_alerts_state) - len(alerts) # Calculate count correctly
            deleted_alert_ids.update(
                a['id'] for a in initial_alerts_state
                if a['user_id'] == target_user_id_for_admin
            )
            message_suffix = f" para el usuario ID {target_user_id_for_admin}"
        else:
            # Admin deletes ALL alerts in the bot
            deleted_count = len(alerts)
            deleted_alert_ids.update(alert['id'] for alert in alerts)
            alerts.clear()
            message_suffix = " del bot (todas las alertas)."
    else: # Normal user, no arguments or just /deleteall
        # User deletes their own alerts
        alerts[:] = [
            alert_data for alert_data in initial_alerts_state
            if alert_data['user_id'] != user_id
        ]
        deleted_count = len(initial_alerts_state) - len(alerts) # Calculate count correctly
        deleted_alert_ids.update(
            a['id'] for a in initial_alerts_state
            if a['user_id'] == user_id
        )
        message_suffix = " tuyas."

    if deleted_count > 0:
        save_alerts(alerts)
        
        # Limpiar last_alerted_datetimes para las alertas eliminadas
        keys_to_remove = []
        for key in last_alerted_datetimes:
            parts = key.split('-')
            if len(parts) == 2:
                try:
                    alert_user_id = int(parts[0])
                    alert_id_in_key = int(parts[1])
                    
                    if alert_id_in_key in deleted_alert_ids: # If the alert ID was specifically deleted
                        keys_to_remove.append(key)
                    elif not is_admin_request and alert_user_id == user_id: # Non-admin deleted their own
                         keys_to_remove.append(key)
                    elif is_admin_request and target_user_id_for_admin is not None and alert_user_id == target_user_id_for_admin: # Admin deleted specific user's
                        keys_to_remove.append(key)
                    elif is_admin_request and target_user_id_for_admin is None: # Admin deleted ALL alerts
                        keys_to_remove.append(key)
                except ValueError:
                    pass # Ignorar claves mal formadas
        for key in keys_to_remove:
            del last_alerted_datetimes[key]
        save_last_alerted_datetimes(last_alerted_datetimes)

        await update.message.reply_text(f"✅ Se eliminaron {deleted_count} alerta(s){message_suffix}.")
    else:
        if is_admin_request and target_user_id_for_admin:
            await update.message.reply_text(f"No se encontraron alertas para el usuario ID {target_user_id_for_admin}.")
        elif is_admin_request and not target_user_id_for_admin:
            await update.message.reply_text("No hay alertas activas en el bot para eliminar.")
        else: # Normal user
            await update.message.reply_text("No tienes alertas activas para eliminar.")

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
    # ¡ATENCIÓN! Si este es el lugar donde ocurre el SyntaxError, verifica la indentación
    # de esta línea 'except' y del bloque 'try' que la precede. Deben estar alineados.
        
    except Exception as e: 
        logger.error(f"Error en la verificación de precios: {e}", exc_info=True)

def main() -> None:
    """Función principal para ejecutar el bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Añadir manejadores de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("admin_help", admin_help)) # NUEVO: Manejador para ayuda de admin
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("edit", edit_alert))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("alerts", show_alerts))
    application.add_handler(CommandHandler("delete", delete_alert))
    application.add_handler(CommandHandler("deleteall", delete_all_alerts))
    application.add_handler(CommandHandler("price", get_price))
    application.add_handler(CommandHandler("resource", get_resource_info))
    application.add_handler(CommandHandler("findid", find_resource_id))


    # Configurar el Job Queue para la verificación de precios
    job_queue: JobQueue = application.job_queue
    job_queue.run_repeating(check_prices_job, interval=30, first=10) # Se ejecuta cada 30 segundos, empieza después de 10 segundos

    # Iniciar el bot
    logger.info("Bot de SimcoTools iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
