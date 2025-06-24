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

# Cargar alertas y datetimes al iniciar el bot
alerts = load_alerts()
last_alerted_datetimes = load_last_alerted_datetimes()

# --- Helper function for MarkdownV2 escaping ---
def escape_markdown_v2(text: str) -> str:
    """Escapa caracteres especiales para Telegram MarkdownV2."""
    # Lista de caracteres especiales que necesitan ser escapados en MarkdownV2
    # https://core.telegram.org/bots/api#formatting-options
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Crear una tabla de traducción para el método str.translate
    translator = str.maketrans({char: '\\' + char for char in escape_chars})
    return text.translate(translator)

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
        "**/status**\n"
        "\\- Muestra el estado actual del bot\\.\n\n"
        "**/alerts \\[admin_code\\]**\n"
        "\\- Muestra todas tus alertas activas\\.\n"
        "\\- Si eres administrador y usas el `admin_code`, muestra todas las alertas del bot\\.\n\n"
        "**/delete \\<id\\> \\[admin_code\\]**\n"
        "\\- Elimina una alerta por su ID\\.\n"
        "\\- Si eres administrador y usas el `admin_code`, puedes eliminar la alerta de cualquier usuario\\.\n\n"
        "**/price \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra el precio actual del mercado para un recurso\\.\n\n"
        "**/resource \\<resourceId\\> \\[quality\\]**\n"
        "\\- Muestra información detallada sobre un recurso y sus precios del último día\\.\n\n"
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

        if len(args) > 2:
            try:
                quality = int(args[2])
                if not (0 <= quality <= 12):
                    raise ValueError("La calidad debe estar entre 0 y 12.")
            except ValueError:
                # Si el tercer argumento no es un número, asúmelo como nombre
                name = args[2]
                if len(args) > 3:
                    name = " ".join(args[2:])
            else:
                # Si el tercer argumento es un número, el cuarto sería el nombre
                if len(args) > 3:
                    name = " ".join(args[3:])

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
        logger.error(f"Error al crear alerta: {e}")
        await update.message.reply_text("Ocurrió un error al crear la alerta.")

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

    # Escape the title as well, just in case it contains special markdown characters
    message = escape_markdown_v2(message_title)
    for alert_data in alerts_to_show:
        quality_info = f"Quality >= {alert_data['quality']}" if alert_data['quality'] is not None else "Todas las calidades"
        # User ID info is already inside backticks, so it's treated as literal code,
        # no further escaping needed for its content.
        user_id_info = f"User ID: `{alert_data['user_id']}`\n" if is_admin else ""

        # Escape individual components that might contain special MarkdownV2 characters
        alert_id_str = str(alert_data['id']) # ID is an integer, usually no special chars
        name_str = escape_markdown_v2(str(alert_data['name'])) # Name can be user-defined, needs escaping
        resource_id_str = str(alert_data['resource_id']) # Resource ID is an integer
        target_price_str = escape_markdown_v2(f"{alert_data['target_price']:.3f}") # Price is float, needs escaping
        quality_info_str = escape_markdown_v2(quality_info) # Quality info string might contain special chars

        message += (
            f"ID: {alert_id_str}\n"
            f"Nombre: {name_str}\n"
            f"Resource ID: {resource_id_str}\n"
            f"Precio Objetivo: {target_price_str}\n"
            f"{quality_info_str}\n"
            f"{user_id_info}" # No additional escaping here
            f"\\-\\-\\-\\n" # Explicitly escape '---' to be literal text, not a Markdown rule
        )
    await update.message.reply_markdown_v2(message)


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

                        # Escapar los caracteres especiales para MarkdownV2
                        message += (
                            f"  Apertura: `{open_str}`\n"
                            f"  Mínimo: `{low_str}`\n"
                            f"  Máximo: `{high_str}`\n"
                            f"  Cierre: `{close_str}`\n"
                            f"  Volumen: `{volume_str}`\n"
                            f"  VWAP: `{vwap_str}`\n"
                        ).replace('.', '\\.').replace('-', '\\-').replace(',', '\\,')


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


# --- Lógica de Verificación de Alertas (Job del Bot) ---

async def check_prices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Función que se ejecuta cada 60 segundos para verificar los precios."""
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
                                # Ensure the alert message is also MarkdownV2 safe
                                await context.bot.send_message(chat_id=user_id, text=escape_markdown_v2(message))

                                # Actualizar el datetime de la última alerta
                                last_alerted_datetimes[alert_key] = item_datetime_str
                                save_last_alerted_datetimes(last_alerted_datetimes)
                                alert_sent = True
                                break # Una alerta por cada recurso que cumpla la condición es suficiente

                if not alert_sent and alert_key in last_alerted_datetimes:
                    # If the alert condition is no longer met, or the item_datetime has not updated
                    # and the price is still below the target, we don't want to spam.
                    # The current logic correctly handles not re-alerting if datetime hasn't changed.
                    pass # This block is fine as is, no change needed.

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
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("alerts", show_alerts))
    application.add_handler(CommandHandler("delete", delete_alert))
    application.add_handler(CommandHandler("price", get_price))
    application.add_handler(CommandHandler("resource", get_resource_info))


    # Configurar el Job Queue para la verificación de precios
    job_queue: JobQueue = application.job_queue
    job_queue.run_repeating(check_prices_job, interval=60, first=10) # Se ejecuta cada 60 segundos, empieza después de 10 segundos

    # Iniciar el bot
    logger.info("Bot de SimcoTools iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
