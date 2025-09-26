import logging
from aiohttp import web

async def notify_development(error_msg, user_info, bot=None):
    developer_chat_id = "365192641"  # Твой chat ID (@PeterFilonenko)
    if user_info:
        username = user_info.get('username', 'Unknown')
        first_name = user_info.get('first_name', '')
        last_name = user_info.get('last_name', '')
        contact_info = f"{first_name} {last_name}".strip() or username
    else:
        contact_info = "Unknown"
    message = f"Ошибка: {error_msg}\nКонтакт: {contact_info}"
    try:
        if bot:
            await bot.send_message(chat_id=developer_chat_id, text=message)
            logging.info(f"Sent error to developer: {message}")
        else:
            logging.warning(f"No bot instance provided for notification: {message}")
    except Exception as e:
        logging.error(f"Failed to notify developer: {e}")
