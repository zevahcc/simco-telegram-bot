import json
import os
from datetime import datetime, timedelta, timezone
import logging
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

# --- Configuración y Variables Globales ---
# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Token de Bot de Telegram (DEBE ser configurado como variable de entorno en Render)
# En Render, ir a 'Environment' y añadir una variable con Nombre: TELEGRAM_BOT_TOKEN y Valor: TU_TOKEN
# Para desarrollo local, puedes ponerlo directamente aquí o usar un archivo .env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7774762013:AAHPM6Q-_A1TtxQaz8U1c9v8R7G1glWEKIQ") # Reemplaza con tu token si es para testeo local sin .env

API_URL = "https://api.simcotools.com/v1/realms/0/market/prices"
ALERTS_FILE = "alerts.json"
LAST_ALERTED_DATETIMES_FILE = "last_alerted_datetimes.json"

# Almacenamiento en memoria para alertas y últimos datetimes alertados
# NOTA: En el nivel gratuito de Render, estos archivos se reiniciarán con cada despliegue/reinicio del bot.
# Para persistencia real, se necesitaría una base de datos externa.
active_alerts = {}  # {alert_id: {chat_id, resourceId, quality, price, name}}
last_alerted_datetimes = {} # {(resourceId, quality): datetime_str}

# --- Funciones de Utilidad para Carga/Guardado (para demostración de persistencia local) ---

def load_alerts():
    global active_alerts
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, 'r') as f:
                active_alerts = json.load(f)
            logger.info(f"Alertas cargadas desde {ALERTS_FILE}.")
        else:
            active_alerts = {}
            logger.info(f"Archivo de alertas '{ALERTS_FILE}' no encontrado. Se inició con alertas vacías.")
    except Exception as e:
        active_alerts = {}
        logger.error(f"Error al cargar alertas: {e}. Se inició con alertas vacías.", exc_info=True)

def save_alerts():
    try:
        with open(ALERTS_FILE, 'w') as f:
            json.dump(active_alerts, f, indent=4)
        logger.info(f"Alertas guardadas en {ALERTS_FILE}.")
    except Exception as e:
        logger.error(f"Error al guardar alertas: {e}", exc_info=True)

def load_last_alerted_datetimes():
    global last_alerted_datetimes
    try:
        if os.path.exists(LAST_ALERTED_DATETIMES_FILE):
            with open(LAST_ALERTED_DATETIMES_FILE, 'r') as f:
                loaded_data = json.load(f)
                # Convertir claves de string a tuplas de ints
                last_alerted_datetimes = {tuple(map(int, k.strip('()').split(', '))): v for k, v in loaded_data.items()}
            logger.info(f"Últimos datetimes alertados cargados desde {LAST_ALERTED_DATETIMES_FILE}.")
        else:
            last_alerted_datetimes = {}
            logger.info(f"Archivo de últimos datetimes alertados '{LAST_ALERTED_DATETIMES_FILE}' no encontrado. Se inició vacío.")
    except Exception as e:
        last_alerted_datetimes = {}
        logger.error(f"Error al cargar últimos datetimes alertados: {e}. Se inició vacío.", exc_info=True)

def save_last_alerted_datetimes():
    try:
        # Convertir claves de tuplas a strings para guardar en JSON
        with open(LAST_ALERTED_DATETIMES_FILE, 'w') as f:
            json.dump({str(k): v for k, v in last_alerted_datetimes.items()}, f, indent=4)
        logger.info(f"Últimos datetimes alertados guardados en {LAST_ALERTED_DATETIMES_FILE}.")
    except Exception as e:
        logger.error(f"Error al guardar últimos datetimes alertados: {e}", exc_info=True)

# --- Funciones de la API de SimcoTools ---

async def get_market_prices():
    """Obtiene los precios actuales del mercado de la API de SimcoTools."""
    logger.info(f"Intentando obtener precios del mercado de: {API_URL}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, timeout=15.0)
            
            logger.info(f"Respuesta de la API: Estado {response.status_code}")
            response.raise_for_status()  # Lanza una excepción para códigos de estado HTTP 4xx/5xx

            data = response.json()
            if 'prices' in data:
                logger.info("Precios obtenidos con éxito de la API.")
                return data['prices']
            else:
                logger.warning(f"La respuesta de la API no contiene la clave 'prices'. Respuesta completa: {data}")
                return []
    except httpx.TimeoutException as e:
        logger.error(f"La petición a la API de SimcoTools ha excedido el tiempo límite (timeout): {e}")
        return None
    except httpx.RequestError as e:
        logger.error(f"Error de red o conexión al intentar obtener precios de {API_URL}: {e}")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f"Error de estado HTTP al obtener precios de {API_URL}: {e.response.status_code} - {e.response.text}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Error al decodificar la respuesta JSON de la API. Puede que la respuesta no sea JSON válida: {e}")
        try:
            logger.error(f"Contenido de la respuesta (posiblemente no JSON): {response.text}")
        except NameError: # If response was not defined due to earlier error
            pass
        return None
    except Exception as e:
        logger.error(f"Error inesperado al obtener precios de {API_URL}: {e}", exc_info=True)
        return None

# --- Funciones de Comandos del Bot ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje de bienvenida cuando se inicia el bot con /start."""
    welcome_message = (
        "¡Hola! Soy tu bot de alertas de mercado para SimCoTools.\n"
        "Usa /help para ver los comandos disponibles."
    )
    await update.message.reply_text(welcome_message)
    logger.info(f"Comando /start recibido de chat_id {update.effective_chat.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje de ayuda con los comandos disponibles."""
    help_text = (
        "Aquí están los comandos que puedes usar:\n\n"
        "👉 /alert `<resourceId>` `<precio_objetivo>` `[quality]` `[nombre]`\n"
        "   - Crea una alerta. `quality` y `nombre` son opcionales.\n"
        "   - Ejemplo: `/alert 1 0.5` (recurso 1, precio 0.5, cualquier calidad)\n"
        "   - Ejemplo: `/alert 1 0.5 5` (recurso 1, precio 0.5, calidad 5 o superior)\n"
        "   - Ejemplo: `/alert 1 0.5 5 mi_alerta`\n\n"
        "📊 /price `<resourceId>` `[quality]`\n"
        "   - Muestra el precio actual de un recurso.\n"
        "   - Ejemplo: `/price 1`\n"
        "   - Ejemplo: `/price 1 5`\n\n"
        "📈 /status\n"
        "   - Muestra el estado actual del bot.\n\n"
        "🔔 /alerts\n"
        "   - Muestra todas tus alertas activas.\n\n"
        "🗑️ /delete `<id_alerta>`\n"
        "   - Elimina una alerta por su ID.\n"
        "   - Ejemplo: `/delete 1`\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')
    logger.info(f"Comando /help recibido de chat_id {update.effective_chat.id}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el estado actual del bot."""
    num_alerts = len(active_alerts)
    status_message = (
        "🟢 *Estado del Bot: Funcionando*\n"
        f"Número de alertas activas: {num_alerts}\n"
        "Las alertas se chequean cada 60 segundos."
    )
    await update.message.reply_text(status_message, parse_mode='Markdown')
    logger.info(f"Comando /status recibido de chat_id {update.effective_chat.id}")

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra todas las alertas activas para el usuario."""
    chat_id = update.effective_chat.id
    user_alerts = {k: v for k, v in active_alerts.items() if v['chat_id'] == chat_id}

    if not user_alerts:
        await update.message.reply_text("No tienes alertas activas. Usa /alert para crear una.")
        logger.info(f"Comando /alerts: No hay alertas para chat_id {chat_id}.")
        return

    message = "*Tus Alertas Activas:*\n\n"
    for alert_id, alert in user_alerts.items():
        quality_str = f"Q{alert['quality']}" if alert['quality'] is not None else "Cualquier Q"
        name_str = f"({alert['name']})" if alert['name'] else ""
        message += (
            f"ID: `{alert_id}` {name_str}\n"
            f"  Recurso: {alert['resourceId']} {quality_str}\n"
            f"  Precio Objetivo: {alert['price']} SimC\n"
            f"  Última alerta: {last_alerted_datetypes.get((alert['resourceId'], alert['quality'] if alert['quality'] is not None else -1), 'N/A')}\n\n"
        )
    await update.message.reply_text(message, parse_mode='Markdown')
    logger.info(f"Comando /alerts: Mostrando {len(user_alerts)} alertas para chat_id {chat_id}.")


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Genera una nueva alerta."""
    chat_id = update.effective_chat.id
    args = context.args

    # /alert <resourceId> <target_price> [quality] [name]
    if not (2 <= len(args) <= 4):
        await update.message.reply_text(
            "Uso incorrecto. Formato: `/alert <resourceId> <precio_objetivo> [quality] [name]`\n"
            "Ejemplos: `/alert 1 0.5`, `/alert 1 0.5 5`, `/alert 1 0.5 5 mi_alerta`"
        )
        logger.warning(f"Comando /alert con argumentos incorrectos: {args} de chat_id {chat_id}.")
        return

    try:
        resource_id = int(args[0])
        target_price = float(args[1])
        quality = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
        name = args[3] if len(args) > 3 else None

        if quality is not None and not (0 <= quality <= 12):
            await update.message.reply_text("La calidad (quality) debe ser un número entre 0 y 12.")
            logger.warning(f"Comando /alert: Calidad fuera de rango ({quality}) de chat_id {chat_id}.")
            return
            
        # Generar un ID único para la alerta
        alert_id = len(active_alerts) + 1
        while alert_id in active_alerts: # Asegurar que el ID sea único
            alert_id += 1

        active_alerts[alert_id] = {
            'chat_id': chat_id,
            'resourceId': resource_id,
            'quality': quality,
            'price': target_price,
            'name': name
        }
        save_alerts() # Guardar alertas

        quality_str = f"Q{quality}" if quality is not None else "Cualquier Q"
        name_str = f" (Nombre: {name})" if name else ""

        response_message = (
            f"🔔 *Alerta Creada!* (ID: `{alert_id}`{name_str})\n"
            f"  Recurso: {resource_id} {quality_str}\n"
            f"  Precio Objetivo: {target_price} SimC\n"
            "Te notificaré cuando el precio sea igual o menor."
        )
        await update.message.reply_text(response_message, parse_mode='Markdown')
        logger.info(f"Alerta creada: ID {alert_id}, Recurso {resource_id}, Precio {target_price}, Calidad {quality}, Nombre '{name}' para chat_id {chat_id}.")

    except ValueError:
        await update.message.reply_text(
            "Error en los valores. Asegúrate de que `resourceId`, `precio_objetivo` y `quality` (si se usa) sean números válidos."
        )
        logger.error(f"Comando /alert: Error de valor en argumentos {args} de chat_id {chat_id}.", exc_info=True)
    except Exception as e:
        logger.error(f"Error inesperado al crear alerta para chat_id {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("Hubo un error inesperado al crear la alerta. Inténtalo de nuevo.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elimina una alerta por su ID."""
    chat_id = update.effective_chat.id
    args = context.args

    if not args or not args[0].isdigit():
        await update.message.reply_text("Uso: `/delete <id_alerta>` (ej. `/delete 1`)")
        logger.warning(f"Comando /delete sin ID o ID inválido de chat_id {chat_id}.")
        return

    alert_id_to_delete = int(args[0])

    if alert_id_to_delete in active_alerts and active_alerts[alert_id_to_delete]['chat_id'] == chat_id:
        deleted_alert = active_alerts.pop(alert_id_to_delete)
        save_alerts() # Guardar alertas
        
        # Eliminar las entradas de last_alerted_datetimes para esta alerta si existieran
        resource_id = deleted_alert['resourceId']
        quality = deleted_alert['quality'] if deleted_alert['quality'] is not None else -1
        if (resource_id, quality) in last_alerted_datetimes:
            del last_alerted_datetimes[(resource_id, quality)]
            save_last_alerted_datetimes() # Guardar los datetimes

        quality_str = f"Q{deleted_alert['quality']}" if deleted_alert['quality'] is not None else "Cualquier Q"
        name_str = f" '{deleted_alert['name']}'" if deleted_alert['name'] else ""

        await update.message.reply_text(f"🗑️ Alerta ID `{alert_id_to_delete}`{name_str} (Recurso: {deleted_alert['resourceId']} {quality_str}) eliminada.")
        logger.info(f"Alerta ID {alert_id_to_delete} eliminada por chat_id {chat_id}.")
    else:
        await update.message.reply_text(f"No se encontró una alerta con ID `{alert_id_to_delete}` o no te pertenece.")
        logger.warning(f"Intento de eliminar alerta ID {alert_id_to_delete} fallido por chat_id {chat_id} (no encontrada o no propietaria).")


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el precio actual de un recurso dado su ID y calidad opcional."""
    logger.info(f"Comando /price recibido de chat_id {update.effective_chat.id}. Mensaje: '{update.message.text}'")

    args = context.args
    if not args:
        await update.message.reply_text("Uso: `/price <resourceId> [quality]` (ej. `/price 1`, `/price 1 5`)")
        logger.warning(f"Comando /price sin resourceId proporcionado por chat_id {update.effective_chat.id}.")
        return

    try:
        resource_id = int(args[0])
        # Aceptar quality como argumento opcional
        quality = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        
        if quality is not None and not (0 <= quality <= 12):
            await update.message.reply_text("La calidad (quality) debe ser un número entre 0 y 12.")
            logger.warning(f"Comando /price: Calidad fuera de rango ({quality}) de chat_id {update.effective_chat.id}.")
            return

        logger.info(f"Precio solicitado para Recurso ID: {resource_id}, Calidad: {quality}")

    except ValueError:
        await update.message.reply_text("El `resourceId` y `quality` (si se usa) deben ser números enteros válidos.")
        logger.warning(f"Comando /price con argumentos inválidos ('{args}') de chat_id {update.effective_chat.id}.")
        return

    prices_data = await get_market_prices() # Obtener los últimos precios

    if prices_data is None:
        await update.message.reply_text("No se pudieron obtener los precios del mercado en este momento. Inténtalo de nuevo más tarde.")
        logger.error(f"No se pudieron obtener los precios del mercado para el comando /price para resourceId {resource_id}.")
        return

    found_prices = []
    for item in prices_data:
        if item.get('resourceId') == resource_id:
            # Si se especificó calidad, verificar calidad igual o superior
            if quality is None: # Si no se especificó calidad, añadir todas las calidades para este resourceId
                found_prices.append(item)
            elif item.get('quality') is not None and item.get('quality') >= quality:
                found_prices.append(item)
    
    # Ordenar por calidad para mostrar de menor a mayor o viceversa si se desea
    found_prices.sort(key=lambda x: x.get('quality', 0))

    if found_prices:
        message = f"📊 *Precios actuales para Recurso ID {resource_id}:*\n"
        if quality is not None:
             message += f"  (Calidad {quality} o superior)\n"

        for item in found_prices:
            item_quality = item.get('quality', 'N/A')
            buy_price = item.get('buyPrice', 'N/A')
            sell_price = item.get('sellPrice', 'N/A')
            volume = item.get('volume', 'N/A')
            
            message += (
                f"\n  *Q{item_quality}:*\n"
                f"    Compra: {buy_price} SimC\n"
                f"    Venta: {sell_price} SimC\n"
                f"    Volumen (24h): {volume}\n"
            )
        try:
            await update.message.reply_text(message, parse_mode='Markdown')
            logger.info(f"Mensaje de precio para resourceId {resource_id} (Q{quality}) enviado exitosamente a chat_id {update.effective_chat.id}.")
        except Exception as e:
            logger.error(f"Error al enviar el mensaje de precio para resourceId {resource_id} (Q{quality}) a chat_id {update.effective_chat.id}: {e}", exc_info=True)
            await update.message.reply_text("Hubo un error al enviar el mensaje de precio. Por favor, inténtalo de nuevo.")
    else:
        await update.message.reply_text(f"No se encontró información para el Recurso ID: {resource_id}" + (f" con calidad {quality} o superior." if quality is not None else ".") + " Asegúrate de que el ID es correcto.")
        logger.warning(f"Recurso ID {resource_id} (Q{quality}) no encontrado en los datos de precios para chat_id {update.effective_chat.id}.")


# --- Función de Chequeo de Alertas (JobQueue) ---

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Función que se ejecuta periódicamente para verificar las alertas.
    Envía notificaciones si las condiciones de precio y datetime se cumplen.
    """
    logger.info("Iniciando chequeo de alertas...")
    
    # Recargar alerts y last_alerted_datetimes en cada ejecución para asegurar consistencia
    # (aunque en Render gratuito, se reinician con cada despliegue/reinicio,
    # esto ayuda si en un futuro se implementa una persistencia más robusta)
    load_alerts()
    load_last_alerted_datetimes()

    prices_data = await get_market_prices()
    if prices_data is None:
        logger.warning("No se pudieron obtener los precios para el chequeo de alertas. Se omitirá esta ronda.")
        return

    active_alert_ids = list(active_alerts.keys()) # Hacer una copia para poder modificar active_alerts si se eliminan
    
    for alert_id in active_alert_ids:
        # Check if alert still exists (could have been deleted by another async task)
        if alert_id not in active_alerts:
            continue
            
        alert = active_alerts[alert_id]
        chat_id = alert['chat_id']
        resource_id = alert['resourceId']
        target_price = alert['price']
        alert_quality = alert['quality'] # Quality solicitada en la alerta (puede ser None)
        alert_name = alert['name']

        # Encontrar precios relevantes de la API para esta alerta
        relevant_prices = []
        for item in prices_data:
            if item.get('resourceId') == resource_id:
                item_quality = item.get('quality')
                if item_quality is not None:
                    if alert_quality is None:
                        # Si la alerta es para cualquier calidad, consideramos todas las calidades
                        relevant_prices.append(item)
                    elif item_quality >= alert_quality and item_quality <= 12:
                        # Si se especificó calidad, considerar esa o superior hasta Q12
                        relevant_prices.append(item)
        
        # Filtrar por el precio objetivo
        found_match = False
        for item_match in relevant_prices:
            current_price = item_match.get('price')
            current_datetime_str = item_match.get('datetime')
            item_quality = item_match.get('quality')

            if current_price is not None and current_price <= target_price:
                # Condición de precio cumplida
                
                # Clave para el seguimiento del datetime: (resourceId, quality)
                # Usamos -1 para quality si la alerta fue para cualquier calidad (quality is None)
                datetime_key = (resource_id, item_quality if item_quality is not None else -1)
                
                last_alert_datetime = last_alerted_datetypes.get(datetime_key)

                if last_alert_datetime != current_datetime_str:
                    # El datetime ha cambiado O es la primera vez que se activa esta alerta para este par (resourceId, quality)
                    
                    alert_message_name = f" '{alert_name}'" if alert_name else ""
                    alert_message_quality = f" Q{item_quality}" if item_quality is not None else ""

                    alert_notification = (
                        f"🔔 *¡Alerta de Compra!* (ID: `{alert_id}`{alert_message_name})\n"
                        f"  Recurso: {resource_id}{alert_message_quality}\n"
                        f"  Precio Objetivo: {target_price} SimC\n"
                        f"  *Precio Actual: {current_price} SimC*\n"
                        f"  Última Actualización: {current_datetime_str}\n"
                    )
                    
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=alert_notification, parse_mode='Markdown')
                        last_alerted_datetypes[datetime_key] = current_datetime_str
                        save_last_alerted_datetimes() # Guardar el nuevo datetime alertado
                        logger.info(f"Alerta ID {alert_id} disparada para Recurso {resource_id} Q{item_quality} a precio {current_price} (chat_id: {chat_id}).")
                        found_match = True # Marca que al menos una combinación de Q/Precio disparó la alerta
                    except Exception as e:
                        logger.error(f"Error al enviar notificación de alerta ID {alert_id} a chat_id {chat_id}: {e}", exc_info=True)
                else:
                    logger.info(f"Alerta ID {alert_id} para Recurso {resource_id} Q{item_quality}: Condición cumplida pero datetime no ha cambiado. No se reenviará.")
            else:
                logger.debug(f"Alerta ID {alert_id} para Recurso {resource_id} Q{item_quality}: Precio actual ({current_price}) no cumple condición (>{target_price}).")
        
        if not found_match:
            logger.info(f"Alerta ID {alert_id}: No se encontró ningún precio que cumpla la condición para Recurso {resource_id} y calidad {alert_quality}.")

    logger.info("Chequeo de alertas completado.")

# --- Función Principal del Bot ---

def main() -> None:
    """Inicia el bot."""
    # Cargamos las alertas y datetimes al iniciar el bot
    load_alerts()
    load_last_alerted_datetimes()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Obtener el JobQueue de la aplicación
    job_queue = application.job_queue

    # Añadir handlers para los comandos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("price", price_command)) # Handler para el comando /price

    # Programar el chequeo de alertas cada 60 segundos
    if job_queue: # Verificar que job_queue no sea None
        job_queue.run_repeating(check_alerts, interval=60, first=0) # first=0 para ejecutar inmediatamente al iniciar
        logger.info("Job 'check_alerts' programado para ejecutarse cada 60 segundos.")
    else:
        logger.error("JobQueue no está configurado. La funcionalidad de alertas programadas no funcionará.")

    # Iniciar el bot (polling para actualizaciones)
    logger.info("Bot iniciado. Escuchando actualizaciones...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()