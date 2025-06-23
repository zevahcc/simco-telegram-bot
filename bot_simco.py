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
API_URL = "https://api.simcotools.com/v1/realms/0/market/prices"
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

# --- Comandos del Bot ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje de bienvenida cuando se inicia el bot."""
    await update.message.reply_text(
        "¡Hola! Soy tu bot de alertas de precios de SimcoTools.\n"
        "Usa /help para ver los comandos disponibles."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los comandos disponibles."""
    help_message = (
        "Comandos disponibles:\n"
        "\\*\\*/alert <price objetivo> <resourceId> \\[quality\\] \\[name\\]\\*\\*\n" # Escapado de [] para que no sean links
        "\\- Crea una nueva alerta de precio\\.\n" # Escapado del guion y el punto
        "\\- \\`price objetivo\\`: El precio máximo al que deseas comprar\\.\n" # Escapado de backticks y puntos
        "\\- \\`resourceId\\`: El ID del recurso \\(número entero\\)\\.\n" # Escapado de paréntesis
        "\\- \\`quality\\` \\(opcional\\): La calidad mínima del recurso \\(0\\-12\\)\\.\n"
        "\\- \\`name\\` \\(opcional\\): Un nombre para tu alerta\\.\n\n"
        "\\*\\*/status\\*\\*\n"
        "\\- Muestra el estado actual del bot\\.\n\n"
        "\\*\\*/alerts\\*\\*\n"
        "\\- Muestra todas las alertas activas\\.\n\n"
        "\\*\\*/delete <id>\\*\\*\n"
        "\\- Elimina una alerta por su ID\\.\n\n"
        "\\*\\*/price <resourceId> \\[quality\\]\\*\\*\n"
        "\\- Muestra el precio actual del mercado para un recurso\\.\n\n"
        "\\*\\*/help\\*\\*\n"
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
    """Muestra todas las alertas activas del usuario."""
    user_id = update.effective_user.id
    user_alerts = [a for a in alerts if a['user_id'] == user_id]

    if not user_alerts:
        await update.message.reply_text("No tienes alertas activas.")
        return

    message = "Tus alertas activas:\n\n"
    for alert_data in user_alerts:
        quality_info = f"Quality >= {alert_data['quality']}" if alert_data['quality'] is not None else "Todas las calidades"
        message += (
            f"ID: {alert_data['id']}\n"
            f"Nombre: {alert_data['name']}\n"
            f"Resource ID: {alert_data['resource_id']}\n"
            f"Precio Objetivo: {alert_data['target_price']}\n"
            f"{quality_info}\n"
            f"---\n"
        )
    await update.message.reply_text(message)

async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elimina una alerta por su ID."""

    global alerts # <--- MUEVE esta línea AQUÍ
    global last_alerted_datetimes # <--- También es buena práctica declararlo global si lo vas a modificar

    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text("Uso incorrecto. Ejemplo: `/delete 1`")
        return

    try:
        alert_id_to_delete = int(args[0])
        user_id = update.effective_user.id
        
        initial_len = len(alerts)

        alerts = [
            alert_data for alert_data in alerts 
            if not (alert_data['id'] == alert_id_to_delete and alert_data['user_id'] == user_id)
        ]
        
        if len(alerts) < initial_len:
            save_alerts(alerts)
            # Eliminar el datetime de la última alerta si existiera
            key_to_delete = f"{user_id}-{alert_id_to_delete}"
            if key_to_delete in last_alerted_datetimes:
                del last_alerted_datetimes[key_to_delete]
                save_last_alerted_datetimes(last_alerted_datetimes)

            await update.message.reply_text(f"✅ Alerta con ID {alert_id_to_delete} eliminada.")
        else:
            await update.message.reply_text(f"No se encontró una alerta con ID {alert_id_to_delete} o no tienes permiso para eliminarla.")
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
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
                                await context.bot.send_message(chat_id=user_id, text=message)
                                
                                # Actualizar el datetime de la última alerta
                                last_alerted_datetimes[alert_key] = item_datetime_str
                                save_last_alerted_datetimes(last_alerted_datetimes)
                                alert_sent = True
                                break # Una alerta por cada recurso que cumpla la condición es suficiente

                if not alert_sent and alert_key in last_alerted_datetimes:
                    # Si ya no se cumple la condición de alerta, borrar el datetime para que pueda alertar de nuevo
                    # si el precio vuelve a bajar o el datetime cambia.
                    # Esto evita alertas repetitivas cuando el precio se mantiene bajo pero el datetime no cambia.
                    # Sin embargo, la lógica del problema indica "si el datetime cambia Y el price sigue igual o menor, volver a dar la alerta"
                    # Esto ya se maneja con `item_datetime > last_alert_datetime`.
                    # Este bloque podría ser para resetear si la condición deja de cumplirse, pero no es explícitamente pedido así.
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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("alert", alert))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("alerts", show_alerts))
    application.add_handler(CommandHandler("delete", delete_alert))
    application.add_handler(CommandHandler("price", get_price))

    # Configurar el Job Queue para la verificación de precios
    job_queue: JobQueue = application.job_queue
    job_queue.run_repeating(check_prices_job, interval=60, first=10) # Se ejecuta cada 60 segundos, empieza después de 10 segundos

    # Iniciar el bot
    logger.info("Bot de SimcoTools iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
