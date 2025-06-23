import json
import os
from datetime import datetime, timedelta, timezone # Import timezone here instead of pytz
import logging
import httpx # Para hacer peticiones HTTP asíncronas
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

# --- Configuración y Variables Globales ---

# Configura el logger para ver los mensajes en la consola
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Tu token de bot de Telegram (¡NO LO PONGAS DIRECTAMENTE AQUÍ EN PRODUCCIÓN!)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN_HERE') # Replace with a test token if running locally

API_URL = "https://api.simcotools.com/v1/realms/0/market/prices"
ALERTS_FILE = 'alerts.json' # Archivo para guardar las alertas
LAST_ALERTED_DATETIMES_FILE = 'last_alerted_datetimes.json' # Archivo para guardar los últimos datetimes alertados

# Estructura para almacenar las alertas activas
# {
#    "user_id": {
#      "alert_id": {
#        "resourceId": int,
#        "quality": int | None, # None means any quality
#        "target_price": float,
#        "name": str | None,
#        "chat_id": int
#      }
#    }
# }
alerts = {}

# Estructura para almacenar el último datetime de alerta para cada combinación (chat_id, resourceId, quality, target_price)
# Esto evita enviar la misma alerta si el datetime no ha cambiado
# {
#    "chat_id": {
#      "resourceId_quality_targetprice_alertname": "datetime_isoformat"
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
last_alerted_datetypes = load_data(LAST_ALERTED_DATETIMES_FILE, {})

# --- Funciones de la API de SimcoTools ---

async def get_market_prices():
    """Obtiene los precios actuales del mercado de la API de SimcoTools."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, timeout=10.0)
            response.raise_for_status()
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
        "  - `quality` (Opcional): La calidad del recurso (0, 1, 2...). Si no se especifica, busca en todas las calidades o superiores.\n"
        "  - `nombre_alerta` (Opcional): Un nombre descriptivo para tu alerta.\n"
        "  *Ejemplo:* `/alert 1 0.25 0 MaderaBarata` (alerta para madera calidad 0 o superior a 0.25 o menos)\n"
        "  *Ejemplo:* `/alert 5 10` (alerta para recurso 5 a 10 o menos, cualquier calidad o superior)\n\n"
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
            test_quality = int(args[2])
            quality = test_quality
            if len(args) >= 4:
                name = " ".join(args[3:])
        except ValueError:
            name = " ".join(args[2:])

    if resource_id < 0 or target_price <= 0 or (quality is not None and quality < 0):
        await update.message.reply_text("Valores inválidos. `resourceId` y `quality` deben ser no negativos, `precio_objetivo` debe ser positivo.")
        return

    if user_id not in alerts:
        alerts[user_id] = {}
    
    new_alert_id = 1
    while str(new_alert_id) in alerts[user_id]:
        new_alert_id += 1
    
    new_alert_id_str = str(new_alert_id)

    alerts[user_id][new_alert_id_str] = {
        "resourceId": resource_id,
        "quality": quality,
        "target_price": target_price,
        "name": name,
        "chat_id": chat_id
    }
    save_data(alerts, ALERTS_FILE)

    quality_str = f"Q{quality} o superior" if quality is not None else "todas las calidades"
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
        quality_str = f"Q{alert_data['quality']} o superior" if alert_data['quality'] is not None else "todas"
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
        if not alerts[user_id]:
            del alerts[user_id]
        save_data(alerts, ALERTS_FILE)
        
        # Eliminar el estado de alerta del datetime para la alerta eliminada
        # Construye la clave EXACTAMENTE como se construye en check_alerts
        chat_id_for_deleted_alert = deleted_alert['chat_id']
        resource_id_for_deleted_alert = deleted_alert['resourceId']
        quality_for_deleted_alert = deleted_alert['quality']
        target_price_for_deleted_alert = deleted_alert['target_price']
        
        # This key needs to match the key used in check_alerts
        alert_key_to_delete = (
            f"{chat_id_for_deleted_alert}-"
            f"{resource_id_for_deleted_alert}-"
            f"{quality_for_deleted_alert if quality_for_deleted_alert is not None else 'None'}-"
            f"{target_price_for_deleted_alert}"
        )
        
        if chat_id_for_deleted_alert in last_alerted_datetimes and \
           alert_key_to_delete in last_alerted_datetypes[chat_id_for_deleted_alert]:
            
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
    # If a specific quality is requested, search for that exact quality
    if quality is not None:
        for item in prices_data:
            if item['resourceId'] == resource_id and item['quality'] == quality:
                found_prices.append(item)
                break # Found the exact quality, no need to continue
    else: # If no quality is specified, get all qualities for the resource
        for item in prices_data:
            if item['resourceId'] == resource_id:
                found_prices.append(item)
    
    if not found_prices:
        quality_str = f" (Q{quality})" if quality is not None else ""
        await update.message.reply_text(f"No se encontraron precios para Recurso ID `{resource_id}`{quality_str}.")
        return
    
    response_lines = [f"*Precios actuales para Recurso ID `{resource_id}`:*\n"]
    for p in sorted(found_prices, key=lambda x: x['quality']):
        updated_time = p.get('datetime', 'N/A')
        if updated_time != 'N/A':
            try:
                # API datetime usually has 'Z' for UTC. fromisoformat needs +00:00 for strict parsing.
                dt_obj = datetime.fromisoformat(updated_time.replace('Z', '+00:00'))
                # Format to HH:MM:SS UTC
                updated_time_str = dt_obj.strftime('%H:%M:%S UTC')
            except ValueError:
                updated_time_str = updated_time # Fallback if parsing fails
        else:
            updated_time_str = 'N/A'

        response_lines.append(f"  - Calidad `{p['quality']}`: `{p['sellOffers'][0]['price']}` (actualizado: `{updated_time_str}`)\n")
    
    await update.message.reply_text("".join(response_lines), parse_mode='Markdown')

# --- Función de Verificación Periódica de Alertas ---

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Iniciando chequeo de alertas...")
    prices_data = await get_market_prices()
    if prices_data is None:
        logger.warning("No se pudieron obtener los precios para el chequeo de alertas.")
        return

    # Use a set to store unique alerts already processed in this cycle to avoid duplicate messages
    # for alerts that might match multiple items due to quality ranges
    processed_alert_keys = set() 

    # Iterate over each user and their alerts
    for user_id, user_alerts_dict in alerts.items():
        for alert_id, alert_data in user_alerts_dict.items():
            chat_id = alert_data['chat_id']
            resource_id = alert_data['resourceId']
            target_quality = alert_data['quality'] # This can be None
            target_price = alert_data['target_price']
            alert_name = alert_data['name']
            
            # Construct a unique key for this specific alert configuration
            # This key is crucial for `last_alerted_datetimes` and `processed_alert_keys`
            alert_unique_identifier = (
                f"{chat_id}-"
                f"{resource_id}-"
                f"{target_quality if target_quality is not None else 'None'}-"
                f"{target_price}"
            )
            
            # Check if this exact alert config has already been processed in this cycle
            if alert_unique_identifier in processed_alert_keys:
                continue # Skip if already processed

            # Cooldown check
            if chat_id in last_alerted_datetimes and alert_unique_identifier in last_alerted_datetypes[chat_id]:
                last_alert_time_str = last_alerted_datetypes[chat_id][alert_unique_identifier]
                # Convert stored ISO string back to datetime object
                last_alert_time = datetime.fromisoformat(last_alert_time_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                
                # Use timezone.utc for current_time for proper comparison
                current_time = datetime.now(timezone.utc) 
                
                if current_time - last_alert_time < timedelta(minutes=30):
                    logger.info(f"Alerta '{alert_id}' para user {user_id} (chat {chat_id}) en cooldown.")
                    continue # Skip if in cooldown

            found_price_info = None
            
            # Determine the starting quality for iteration (0 if target_quality is None, otherwise target_quality)
            start_quality = 0 if target_quality is None else target_quality

            # Iterate from the requested quality (or Q0) up to Q12
            for q in range(start_quality, 13): # Q12 is often the max, adjust if Simco changes this
                # Search for the resource with the current quality (q)
                for item in prices_data:
                    if item['resourceId'] == resource_id and item['quality'] == q:
                        if item['sellOffers'] and item['sellOffers'][0]['price'] <= target_price:
                            # Found a matching item. Prioritize higher quality if multiple match.
                            if found_price_info is None or q > found_price_info['quality']:
                                found_price_info = {
                                    'quality': q,
                                    'price': item['sellOffers'][0]['price'],
                                    'market_time': item['sellOffers'][0]['createdAt']
                                }
                        # Break inner loop once the resource with quality 'q' is found (even if price doesn't match)
                        # This avoids unnecessary iteration for the same quality
                        break 
                
                # If a suitable item is found (price <= target and quality >= target_quality), 
                # and if we were looking for a specific quality, stop searching for lower qualities.
                # If target_quality was None, we stop as soon as we find any match.
                if found_price_info and (target_quality is None or found_price_info['quality'] >= start_quality):
                    break 

            if found_price_info:
                market_price = found_price_info['price']
                market_quality = found_price_info['quality']
                market_time = found_price_info['market_time']

                message = (
                    f"🚨 *¡ALERTA DE PRECIO!* 🚨\n"
                    f"Recurso ID: `{resource_id}`\n"
                    f"Calidad Encontrada: `Q{market_quality}` (Alerta configurada para `Q{target_quality if target_quality is not None else 'cualquier'}` o superior)\n"
                    f"Precio: `${market_price:.2f}` (Objetivo: $\\le {target_price:.2f}$)\n"
                    f"Última actualización del mercado: `{datetime.fromisoformat(market_time.replace('Z', '+00:00')).strftime('%H:%M:%S')} UTC`"
                )
                if alert_name:
                    message += f"\nNombre de Alerta: *{alert_name}*"

                try:
                    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                    logger.info(f"Alerta enviada para chat {chat_id}, recurso {resource_id}, calidad {market_quality}, precio {market_price}. FechaHora: {market_time}")

                    # Update last alerted time for this specific alert configuration
                    if chat_id not in last_alerted_datetypes:
                        last_alerted_datetypes[chat_id] = {}
                    last_alerted_datetypes[chat_id][alert_unique_identifier] = datetime.now(timezone.utc).isoformat()
                    save_data(last_alerted_datetypes, LAST_ALERTED_DATETIMES_FILE)
                    
                    # Add to processed set to avoid duplicate messages in this cycle
                    processed_alert_keys.add(alert_unique_identifier)
                    logger.info(f"Estado de alerta actualizado para {resource_id} Q{target_quality if target_quality is not None else 'any'} en chat {chat_id}.")
                except Exception as send_error:
                    logger.error(f"Error al enviar mensaje a chat {chat_id}: {send_error}")
            else:
                # If the price no longer meets the condition, clear its last alerted state
                # This ensures the alert can trigger again if the price drops back down
                if chat_id in last_alerted_datetypes and alert_unique_identifier in last_alerted_datetypes[chat_id]:
                    del last_alerted_datetypes[chat_id][alert_unique_identifier]
                    save_data(last_alerted_datetypes, LAST_ALERTED_DATETIMES_FILE)
                    logger.info(f"Limpiado estado de alerta para {resource_id} Q{target_quality if target_quality is not None else 'any'} en chat {chat_id} porque el precio subió.")
    logger.info("Chequeo de alertas completado.")


# --- Función Principal ---

def main() -> None:
    """Inicia el bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    job_queue = application.job_queue

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("price", price_command))

    job_queue.run_repeating(check_alerts, interval=60, first=0) # Run every 60 seconds

    logger.info("Bot iniciado. Esperando comandos...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()