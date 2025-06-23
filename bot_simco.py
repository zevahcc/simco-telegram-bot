import json
import os
from datetime import datetime, timedelta
import pytz # Necesitas esta importación si usas pytz.timezone
import logging
import httpx # Para hacer peticiones HTTP asíncronas
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuración y Variables Globales ---

# Configura el logger para ver los mensajes en la consola
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Tu token de bot de Telegram (¡NO LO PONGAS DIRECTAMENTE AQUÍ EN PRODUCCIÓN!)
# Lo cargaremos desde una variable de entorno en Render
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'TU_TOKEN_AQUI_PARA_PRUEBAS_LOCALES')

API_URL = "https://api.simcotools.com/v1/realms/0/market/prices"
ALERTS_FILE = 'alerts.json' # Archivo para guardar las alertas
LAST_ALERTED_DATETIMES_FILE = 'last_alerted_datetimes.json' # Archivo para guardar los últimos datetimes alertados

# Estructura para almacenar las alertas activas
# alerts = {
#    "user_id": {
#      "alert_id": {
#        "resourceId": int,
#        "quality": int | None,
#        "target_price": float,
#        "name": str | None,
#        "chat_id": int # Para saber a qué chat enviar la alerta
#      }
#    }
# }
alerts = {}

# Estructura para almacenar el último datetime de alerta para cada combinación (chat_id, resourceId, quality, target_price)
# Esto evita enviar la misma alerta si no ha pasado el cooldown
# {
#    "chat_id": {
#      "resourceId_quality_targetprice": "datetime_isoformat"
#    }
# }
last_alerted_datetimes = {}

# --- Funciones de Utilidad para Persistencia ---

def load_data(filename, default_value):
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Error al cargar {filename}. El archivo está corrupto o vacío. Se inicializará con valores por defecto.")
                return default_value
    return default_value

def save_data(data, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

# Cargar datos al inicio
alerts = load_data(ALERTS_FILE, {})
last_alerted_datetimes = load_data(LAST_ALERTED_DATETIMES_FILE, {})

# --- Funciones de la API de SimcoTools ---

async def get_market_prices():
    """Obtiene los precios actuales del mercado de la API de SimcoTools."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, timeout=10.0) # Añadir timeout
            response.raise_for_status()  # Lanza una excepción para códigos de estado HTTP 4xx/5xx
            data = response.json()
            return data.get('prices', [])
    except httpx.RequestError as e:
        logger.error(f"Error de red o conexión al intentar obtener precios: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado al obtener precios: {e}")
        return None

# --- Funciones del Bot (Handlers) ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje de ayuda cuando se emite el comando /help."""
    help_text = (
        "¡Hola! Soy tu bot de alertas de SimcoTools.\n\n"
        "Comandos disponibles:\n"
        "`/alert <resourceId> <precio_objetivo> [quality] [nombre_alerta]`\n"
        "  - `resourceId`: ID del recurso (ej. 1 para Madera)\n"
        "  - `precio_objetivo`: El precio máximo al que quieres comprar.\n"
        "  - `quality` (Opcional): La calidad del recurso (0, 1, 2...). Si no se especifica, busca en todas las calidades.\n"
        "  - `nombre_alerta` (Opcional): Un nombre descriptivo para tu alerta.\n"
        "  *Ejemplo:* `/alert 1 0.25 0 MaderaBarata` (alerta para madera calidad 0 a 0.25 o menos)\n"
        "  *Ejemplo:* `/alert 5 10` (alerta para recurso 5 a 10 o menos, cualquier calidad)\n\n"
        "`/status`\n"
        "  - Muestra el estado actual del bot y si está monitoreando precios.\n\n"
        "`/alerts`\n"
        "  - Muestra todas tus alertas activas con su ID para poder eliminarlas.\n\n"
        "`/delete <id_alerta>`\n"
        "  - Elimina una alerta específica usando su ID.\n"
        "  *Ejemplo:* `/delete 5`\n\n"
        "`/price <resourceId> [quality]`\n"
        "  - Muestra el precio actual de un `resourceId` específico y opcionalmente una `quality`.\n"
        "  *Ejemplo:* `/price 1` (precio de madera, todas las calidades)\n"
        "  *Ejemplo:* `/price 1 0` (precio de madera calidad 0)\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el estado del bot."""
    num_alerts = sum(len(user_alerts) for user_alerts in alerts.values())
    await update.message.reply_text(f"Bot de SimcoTools activo y monitoreando el mercado.\n"
                                     f"Alertas activas en total: {num_alerts}.")

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja el comando /alert para crear nuevas alertas."""
    args = context.args
    chat_id = update.message.chat_id
    user_id = str(update.effective_user.id) # Convertir a string para usar como clave

    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/alert <resourceId> <precio_objetivo> [quality] [nombre_alerta]`\n"
            "Ejemplo: `/alert 1 0.25 0 MaderaBarata`",
            parse_mode='Markdown'
        )
        return

    try:
        resource_id = int(args[0])
        target_price = float(args[1])
    except ValueError:
        await update.message.reply_text("`resourceId` y `precio_objetivo` deben ser números válidos.")
        return

    quality = None
    name = None

    if len(args) >= 3:
        try:
            # Intenta parsear el tercer argumento como quality
            test_quality = int(args[2])
            quality = test_quality
            if len(args) >= 4:
                # Si hay cuarto argumento, es el nombre
                name = " ".join(args[3:])
        except ValueError:
            # Si el tercer argumento no es un número, asúmelo como parte del nombre
            name = " ".join(args[2:])

    if resource_id < 0 or target_price <= 0 or (quality is not None and quality < 0):
        await update.message.reply_text("Valores inválidos. `resourceId` y `quality` deben ser no negativos, `precio_objetivo` debe ser positivo.")
        return

    # Generar un ID único y fácil de recordar para la alerta
    if user_id not in alerts:
        alerts[user_id] = {}
    
    # Encontrar el ID más bajo disponible para el usuario
    new_alert_id = 1
    while str(new_alert_id) in alerts[user_id]:
        new_alert_id += 1
    
    new_alert_id_str = str(new_alert_id)

    alerts[user_id][new_alert_id_str] = {
        "resourceId": resource_id,
        "quality": quality,
        "target_price": target_price,
        "name": name,
        "chat_id": chat_id # Guarda el chat_id para enviar la alerta
    }
    save_data(alerts, ALERTS_FILE)

    quality_str = f"Q{quality}" if quality is not None else "todas las calidades"
    name_str = f" (Nombre: '{name}')" if name else ""

    await update.message.reply_text(
        f"¡Alerta creada con éxito! ID: `{new_alert_id_str}`\n"
        f"Buscar: Recurso ID `{resource_id}`, Calidad: `{quality_str}`\n"
        f"Precio objetivo: `{target_price}` o menos.{name_str}",
        parse_mode='Markdown'
    )
    logger.info(f"Alerta creada para user {user_id}: {alerts[user_id][new_alert_id_str]}")


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra las alertas activas para el usuario."""
    user_id = str(update.effective_user.id)
    
    if user_id not in alerts or not alerts[user_id]:
        await update.message.reply_text("No tienes alertas activas.")
        return

    alerts_list = ["*Tus alertas activas:*\n"]
    for alert_id, alert_data in alerts[user_id].items():
        quality_str = f"Q{alert_data['quality']}" if alert_data['quality'] is not None else "todas"
        name_str = f" - '{alert_data['name']}'" if alert_data['name'] else ""
        alerts_list.append(
            f"`{alert_id}`: Recurso ID `{alert_data['resourceId']}`, "
            f"Calidad `{quality_str}`, Precio $\\le {alert_data['target_price']}${name_str}\n"
        )
    
    await update.message.reply_text("".join(alerts_list), parse_mode='Markdown')

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elimina una alerta por su ID."""
    args = context.args
    user_id = str(update.effective_user.id)

    if not args or len(args) != 1:
        await update.message.reply_text("Uso: `/delete <id_alerta>`")
        return

    alert_id_to_delete = args[0]

    if user_id in alerts and alert_id_to_delete in alerts[user_id]:
        deleted_alert = alerts[user_id].pop(alert_id_to_delete)
        if not alerts[user_id]: # Si no quedan alertas para el usuario, eliminar su entrada
            del alerts[user_id]
        save_data(alerts, ALERTS_FILE)
        
        # Eliminar el estado de alerta del datetime para la alerta eliminada
        # La clave en last_alerted_datetimes ahora es chat_id -> clave_unica_de_alerta
        # Necesitamos recrear esa clave única para eliminarla
        chat_id_for_deleted_alert = deleted_alert['chat_id']
        resource_id_for_deleted_alert = deleted_alert['resourceId']
        quality_for_deleted_alert = deleted_alert['quality']
        target_price_for_deleted_alert = deleted_alert['target_price'] # Asegúrate de que este campo coincida

        alert_key_to_delete = f"{chat_id_for_deleted_alert}-{resource_id_for_deleted_alert}-{quality_for_deleted_alert}-{target_price_for_deleted_alert}"
        
        if chat_id_for_deleted_alert in last_alerted_datetimes and \
           alert_key_to_delete in last_alerted_datetimes[chat_id_for_deleted_alert]:
            
            del last_alerted_datetypes[chat_id_for_deleted_alert][alert_key_to_delete]
            if not last_alerted_datetypes[chat_id_for_deleted_alert]:
                del last_alerted_datetypes[chat_id_for_deleted_alert]
            save_data(last_alerted_datetypes, LAST_ALERTED_DATETIMES_FILE)

        await update.message.reply_text(f"Alerta con ID `{alert_id_to_delete}` eliminada correctamente.")
        logger.info(f"Alerta {alert_id_to_delete} eliminada para user {user_id}")
    else:
        await update.message.reply_text(f"No se encontró ninguna alerta con el ID `{alert_id_to_delete}` para ti.")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el precio actual de un recurso o calidad específica."""
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text("Uso: `/price <resourceId> [quality]`")
        return

    try:
        resource_id = int(args[0])
        quality = int(args[1]) if len(args) >= 2 else None
    except ValueError:
        await update.message.reply_text("`resourceId` y `quality` (si se especifica) deben ser números válidos.")
        return

    prices_data = await get_market_prices()
    if prices_data is None:
        await update.message.reply_text("Lo siento, no pude obtener los precios del mercado en este momento. Intenta de nuevo más tarde.")
        return

    found_prices = []
    for item in prices_data:
        if item['resourceId'] == resource_id:
            if quality is None or item['quality'] == quality:
                found_prices.append(item)
    
    if not found_prices:
        quality_str = f" (Q{quality})" if quality is not None else ""
        await update.message.reply_text(f"No se encontraron precios para Recurso ID `{resource_id}`{quality_str}.")
        return
    
    response_lines = [f"*Precios actuales para Recurso ID `{resource_id}`:*\n"]
    for p in sorted(found_prices, key=lambda x: x['quality']):
        # Asegúrate de que 'datetime' existe y formatéalo correctamente
        updated_time = p.get('datetime', 'N/A')
        if updated_time != 'N/A':
            # Intentar parsear y formatear si es un ISO string
            try:
                dt_obj = datetime.fromisoformat(updated_time.replace('Z', '+00:00')) # Manejar 'Z' si existe
                updated_time_str = dt_obj.strftime('%H:%M:%S UTC')
            except ValueError:
                updated_time_str = updated_time # Usar tal cual si no se puede parsear
        else:
            updated_time_str = 'N/A'

        response_lines.append(f"  - Calidad `{p['quality']}`: `{p['price']}` (actualizado: `{updated_time_str}`)\n")
    
    await update.message.reply_text("".join(response_lines), parse_mode='Markdown')

# --- Función de Verificación Periódica de Alertas ---

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Iniciando chequeo de alertas...")
    api_url = "https://api.simcotools.com/v1/realms/0/market/prices"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, timeout=30.0)
            response.raise_for_status()
            market_data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Error HTTP al obtener precios: {e.response.status_code} - {e.response.text}")
        return
    except httpx.RequestError as e:
        logger.error(f"Error de red al obtener precios: {e}")
        return
    except Exception as e:
        logger.error(f"Error inesperado al obtener precios: {e}")
        return

    current_time = datetime.now(pytz.utc) # Usamos pytz.utc para la zona horaria
    cooldown_period = timedelta(minutes=30) # Cooldown de 30 minutos para no spammear

    # Iterar sobre cada usuario y sus alertas
    for user_id, user_alerts_dict in alerts.items():
        for alert_id, alert_data in user_alerts_dict.items():
            chat_id = alert_data['chat_id']
            resource_id = alert_data['resourceId']
            target_quality = alert_data['quality']
            target_price = alert_data['target_price']
            
            # La clave para last_alerted_datetimes debe ser única para esta alerta en este chat
            # Combina chat_id, resourceId, quality, y target_price
            alert_key = f"{chat_id}-{resource_id}-{target_quality if target_quality is not None else 'None'}-{target_price}"
            
            # Comprobación del cooldown
            if chat_id in last_alerted_datetimes and alert_key in last_alerted_datetypes[chat_id]:
                last_alert_time_str = last_alerted_datetypes[chat_id][alert_key]
                last_alert_time = datetime.fromisoformat(last_alert_time_str)
                if current_time - last_alert_time < cooldown_period:
                    logger.info(f"Alerta {alert_id} para {user_id} en chat {chat_id} en cooldown.")
                    continue # Saltar esta alerta si está en cooldown

            found_price_info = None
            
            # Si target_quality es None, buscar desde 0 hasta el máximo. Si no, desde la calidad objetivo.
            start_quality = 0 if target_quality is None else target_quality

            for q in range(start_quality, 13): # Asumiendo calidad máxima 12 (puedes ajustar si es necesario)
                for item in market_data:
                    if item['resourceId'] == resource_id and item['quality'] == q:
                        if item['sellOffers'] and item['sellOffers'][0]['price'] <= target_price:
                            # Hemos encontrado un precio que cumple, y es de la misma calidad o superior
                            # Preferimos la calidad más alta que cumpla la condición
                            if found_price_info is None or q > found_price_info['quality']:
                                found_price_info = {
                                    'quality': q,
                                    'price': item['sellOffers'][0]['price'],
                                    'market_time': item['sellOffers'][0]['createdAt']
                                }
                        # Importante: rompe el bucle interno (de 'item in market_data') una vez que encuentras el recurso con la calidad 'q'
                        # para evitar procesar duplicados si la API lo devuelve así, o para pasar a la siguiente calidad
                        break 
                # Si ya encontramos una oferta que cumple y su calidad es al menos la que estamos buscando,
                # no necesitamos seguir buscando calidades inferiores.
                # O si target_quality es None (buscando cualquier calidad) y ya encontramos algo, podemos parar la búsqueda de calidad.
                if found_price_info and (target_quality is None or found_price_info['quality'] >= start_quality):
                    break 

            if found_price_info:
                market_price = found_price_info['price']
                market_quality = found_price_info['quality']
                market_time = found_price_info['market_time']

                message = (
                    f"🚨 ¡Alerta de precio! 🚨\n"
                    f"Recurso ID: {resource_id}\n"
                    f"Calidad Encontrada: Q{market_quality} (Alerta configurada para Q{target_quality if target_quality is not None else 'cualquier'} o superior)\n"
                    f"Precio: ${market_price:.2f} (Objetivo: <= ${target_price:.2f})\n"
                    f"Última actualización del mercado: {market_time}"
                )
                try:
                    await context.bot.send_message(chat_id=chat_id, text=message)
                    logger.info(f"Alerta enviada para chat {chat_id}, recurso {resource_id}, calidad {market_quality}, precio {market_price}. FechaHora: {market_time}")

                    # Actualizar el último tiempo de alerta para esta alerta específica
                    if chat_id not in last_alerted_datetypes:
                        last_alerted_datetypes[chat_id] = {}
                    last_alerted_datetypes[chat_id][alert_key] = current_time.isoformat()
                    save_data(last_alerted_datetypes, LAST_ALERTED_DATETIMES_FILE)
                    
                    logger.info(f"Estado de alerta actualizado para {resource_id} Q{target_quality if target_quality is not None else 'any'} en chat {chat_id}.")
                except Exception as send_error:
                    logger.error(f"Error al enviar mensaje a chat {chat_id}: {send_error}")


    logger.info("Chequeo de alertas completado.")


# --- Función Principal ---

def main() -> None:
    """Inicia el bot."""
    # Crea la Application y pasa el token de tu bot.
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Obtiene el JobQueue de la aplicación
    job_queue = application.job_queue

    # Agrega manejadores para los comandos
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command)) # Comando /alerts correctamente mapeado
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("price", price_command))

    # Programa la tarea periódica para verificar alertas
    # La tarea se ejecutará cada 60 segundos
    job_queue.run_repeating(check_alerts, interval=60, first=0) # first=0 para que se ejecute al iniciar

    # Inicia el polling del bot
    logger.info("Bot iniciado. Esperando comandos...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()