import aiomysql
import logging
import re
import asyncio
import aiohttp
import smtplib
from grok_api import query_grok
from my_info import SYSTEM_PROMPT, CUSTOM_WORD_MAP
from telegram.ext import CallbackQueryHandler
from telegram.constants import ChatAction
from config import DB_CONFIG, IP_PHONE, SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_RECIPIENTS, ADMIN_CHAT_ID, GLAV_CHAT_ID, DIR_CHAT_ID, STOMCLINICA_CHAT_ID
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from datetime import datetime, timedelta
from email.mime.text import MIMEText

def get_mapped_word(word):
    """Преобразует входное слово в ключевое слово для поиска на основе CUSTOM_WORD_MAP."""
    word = word.lower()
    for key_word, variants in CUSTOM_WORD_MAP.items():
        if word in variants:
            return key_word
    return word

class Stomat:
    async def save_message_to_db(self, user_id, message_text, sender):
        """Сохранение сообщения в MySQL."""
        try:
            async with aiomysql.connect(**DB_CONFIG) as conn:
                async with conn.cursor() as cursor:
                    # Проверка на существование идентичного сообщения
                    check_query = """
                        SELECT COUNT(*) FROM chat_history 
                        WHERE user_id = %s AND message_text = %s AND sender = %s 
                        AND timestamp > NOW() - INTERVAL 5 MINUTE
                    """
                    await cursor.execute(check_query, (user_id, message_text, sender))
                    count = (await cursor.fetchone())[0]
                    if count > 0:
                        logging.info(f"Дубликат сообщения пропущен: user_id={user_id}, sender={sender}")
                        return

                    # Сохранение сообщения
                    query = "INSERT INTO chat_history (user_id, message_text, sender) VALUES (%s, %s, %s)"
                    await cursor.execute(query, (user_id, message_text, sender))
                    # Удаление старых записей (без параметров)
                    query_del = "DELETE FROM chat_history WHERE timestamp < NOW() - INTERVAL 30 DAY"
                    await cursor.execute(query_del)
                    await conn.commit()
                    # logging.info(f"Message saved in MySQL: user_id={user_id}, sender={sender}")
        except Exception as e:
            logging.error(f"Ошибка при сохранении сообщения в MySQL: {e}")

    async def get_user_history(self, user_id, limit=10):
        """Извлечение последних limit сообщений пользователя и бота."""
        try:
            async with aiomysql.connect(**DB_CONFIG) as conn:
                async with conn.cursor() as cursor:
                    query = """
                        SELECT SUBSTRING(message_text, 1, 500) AS message_text, sender 
                        FROM chat_history 
                        WHERE user_id = %s 
                        ORDER BY timestamp DESC 
                        LIMIT %s
                    """
                    await cursor.execute(query, (user_id, limit))
                    history = await cursor.fetchall()
                    return [{'role': 'user' if row[1] == 'user' else 'assistant', 'content': row[0]} for row in reversed(history)]
        except Exception as e:
            logging.error(f"Ошибка при получении истории из MySQL: {e}")
            return []

    async def process_message(self, user_id, message_text):
        """Обработка сообщения с учётом контекста."""
        # Сохраняем входящее сообщение
        await self.save_message_to_db(user_id, message_text, 'user')
        
        # Получаем историю диалога
        history = await self.get_user_history(user_id, limit=10)  # Последние 10 сообщений

        # Ограничиваем длину истории, чтобы избежать превышения лимита токенов
        if len(history) > 4000:  # Жёсткий лимит 4000 символов
            history = history[-5:]  # Оставляем последние 5 сообщений
            logging.warning(f"История сообщений для user_id={user_id} урезана из-за превышения длины")

        query = f"История диалога:\n" + "\n".join([f"{msg['role']}: {msg['content']}" for msg in history]) + f"\nuser: {message_text}"
        
        # Запрос к Grok API
        # logging.info(f"Grok API history request {query}")
        response = await query_grok(query, system_prompt=SYSTEM_PROMPT, user_info=None, bot=None)
        
        # Сохраняем ответ бота
        await self.save_message_to_db(user_id, response, 'bot')
        
        # Проверка на триггеры для пересылки (отзывы/жалобы)
        # if any(keyword in message_text.lower() for keyword in ['отзыв', 'жалоб', 'благодар']):
        #     await self.forward_message(user_id, message_text)
        #     response += "\nЯ передал ваше сообщение в администрацию."
        
        return response
        
    async def get_contacts(self, app):
        """Обработка кнопки Контакты"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT `name`,`note`,`mapslink` FROM filial WHERE `id`=1")
                    return await cursor.fetchall()
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_contacts: {str(e)}")
            return []
        except Exception as e:
            logging.error(f"Unexpected error in get_contacts: {str(e)}")
            return []

    async def get_amerhanov_info(self, app):
        """Обработка информации о враче Амерханов"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:  # Используем DictCursor для словарей
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '189'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']} </a>на сайте\n\n"
                        )

                        # отпуск
                        # current_time = datetime.now() + timedelta(hours=2)  # CEST to Yekaterinburg (UTC+5)
                        # target_start_date = datetime(2025, 8, 4, 0, 0)    # 4 августа 2025, 00:00
                        # if current_time.date() < target_start_date.date():
                        #     start_time = target_start_date
                        # else:
                        #     start_time = current_time + timedelta(days=1)
                        # end_time = start_time + timedelta(days=14)

                        # нет отпуска
                        # # Получаем занятые слоты из таблицы appoint для врача 387
                        current_time = datetime.now()
                        current_time = current_time + timedelta(hours=2)
                        start_time = current_time + timedelta(days=1) 
                        end_time = start_time + timedelta(days=14)
                        await cursor.execute("""
                            SELECT `data_app` FROM `appoint` 
                            WHERE `id_spec` = '189' 
                            AND `data_app` BETWEEN %s AND %s 
                            AND `deleted` = 0
                        """, (start_time, end_time))
                        occupied_slots = [row['data_app'] for row in await cursor.fetchall()]
                        # logging.info(f"Occupied slots: {occupied_slots}")

                        # Генерация всех возможных слотов

                        free_slots = []
                        current_date = start_time.replace(hour=8, minute=0, second=0, microsecond=0)  # Начало дня
                        while current_date < end_time:
                            weekday = current_date.weekday()  # 0 - понедельник, 1 - вторник, ..., 6 - воскресенье
                            day = current_date.day  # День месяца (1–31)

                            if weekday in [0, 1, 2, 3, 4]:  # Понедельник–пятница
                                if day % 2 == 0:  # Четные даты
                                    start_hour, end_hour = 8, 14 # Первая смена
                                else:  # Нечетные даты
                                    start_hour, end_hour = 14, 20  # Вторая смена
                            else:  # Среда (2), суббота (5), воскресенье (6) — не работает
                                current_date += timedelta(days=1)
                                current_date = current_date.replace(hour=8, minute=0, second=0, microsecond=0)
                                continue

                            current_slot = current_date.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                            while current_slot.hour < end_hour:
                                slot_time = current_slot.strftime("%Y-%m-%d %H:%M")
                                if current_slot not in occupied_slots:
                                    free_slots.append(slot_time)
                                current_slot += timedelta(minutes=30)
                            current_date += timedelta(days=1)
                            current_date = current_date.replace(hour=8, minute=0, second=0, microsecond=0)

                        # Группировка и красивый вывод свободных слотов в text
                        if free_slots:
                            text += "Ближайшая свободная запись:\n"
                            current_day = None
                            slots_line = []
                            for slot in sorted(free_slots):
                                slot_datetime = datetime.strptime(slot, "%Y-%m-%d %H:%M")
                                if current_day != slot_datetime.date():
                                    if current_day is not None and slots_line:
                                        text += "  " + " | ".join(slots_line) + "\n"
                                    slots_line = []
                                    day_name = slot_datetime.strftime("%d %B, %A").replace("January", "января").replace("February", "февраля").replace("March", "марта").replace("April", "апреля").replace("May", "мая").replace("June", "июня").replace("July", "июля").replace("August", "августа").replace("September", "сентября").replace("October", "октября").replace("November", "ноября").replace("December", "декабря").replace("Monday", "понедельник").replace("Tuesday", "вторник").replace("Thursday", "четверг").replace("Friday", "пятница")
                                    text += f"{day_name}:\n"
                                    current_day = slot_datetime.date()
                                slots_line.append(slot_datetime.strftime("%H:%M"))
                            if slots_line:
                                text += "  " + " | ".join(slots_line) + "\n"
                        else:
                            text += "На ближайшие три недели свободной записи нет.\n"
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_bagaviev_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_bagaviev_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_ahmetshin_info(self, app):
        """Обработка информации о враче Ахметшин"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '123'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_gubin_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_gubin_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_begmatov_info(self, app):
        """Обработка информации о враче Попова"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '89'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_popova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_popova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_begmatov_info(self, app):
        """Обработка информации о враче Гольцова"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '94'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_goltsova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_goltsova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_belosludtseva_info(self, app):
        """Обработка информации о враче Иванова"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '87'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_ivanova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_ivanova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_belousov_info(self, app):
        """Обработка информации о враче Исаева"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '10'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_isaeva_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_isaeva_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_gruzunova_info(self, app):
        """Обработка информации о враче Пастухов"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '28'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_pastuhov1_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_pastuhov1_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_zamaletdinov_info(self, app):
        """Обработка информации о враче Пастухов"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '35'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_svetlakova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_svetlakova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_rulov_info(self, app):
        """Обработка информации о враче Седкова"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '182'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_sedkova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_sedkova_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_tuckmach_info(self, app):
        """Обработка информации о враче Чехутская"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '76'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_chehutskaya_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_chehutskaya_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_habib_info(self, app):
        """Обработка информации о враче Леонов"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '143'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_leonov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_leonov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_halilova_info(self, app):
        """Обработка информации о враче Халилова"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '17'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_pastuhov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_pastuhov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_chirgilaev_info(self, app):
        """Обработка информации о враче Пастухов"""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `note`, `text_training` FROM `profiles` WHERE `user_id` = '82'")
                    result = await cursor.fetchone()
                    if result:
                        text = (
                            f"<b>{result['fullname']}</b>\n"
                            f"{result['text_training']}\n"
                            f"{result['note']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result['user_id']}\">записаться к {result['shortname']}</a>\n\n"
                        )
                        return text
                    return "Данные о враче не найдены."
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_pastuhov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_pastuhov_info: {str(e)}")
            return "Ошибка при загрузке данных. Попробуйте позже."

    async def get_simonova_info(self, app):
        """Обработка информации о враче Симонова ОЮ Тамара"""
        result = {"text": "", "reply_markup": None, "free_slots": []}
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT `user_id`, `fullname`, `shortname`, `text_training` FROM `profiles` WHERE `user_id` = '13'")
                    result_db = await cursor.fetchone()
                    if result_db:
                        result["text"] = (
                            f"<b>{result_db['fullname']}</b>\n"
                            f"{result_db['text_training']}\n"
                            f"📅 <a href=\"https://izdenta.com/appoint/calendar/?id_spec={result_db['user_id']}\">записаться к {result_db['shortname']}</a> (сайт)\n\n"
                        )
                        # Получаем занятые слоты из таблицы appoint для врача 13
                        days_to_start = 7
                        days_to_end = 150
                        current_time = datetime.now()
                        start_time = current_time + timedelta(days=days_to_start)
                        end_time = start_time + timedelta(days=days_to_end)
                        await cursor.execute("""
                            SELECT `data_app`, `data_end` FROM `appoint` 
                            WHERE `id_spec` = '13' 
                            AND `data_app` BETWEEN %s AND %s 
                            AND `deleted` = 0
                            ORDER BY `data_app`
                        """, (start_time, end_time))
                        appointments = [row for row in await cursor.fetchall()]

                        # Вычисление свободных интервалов
                        free_slots = []
                        if appointments:
                            for i in range(len(appointments)):
                                current_end = appointments[i]['data_end']
                                current_datetime = current_end
                                weekday = current_end.weekday()
                                if weekday in [1, 3, 4]:
                                    if i + 1 < len(appointments):
                                        next_start = appointments[i + 1]['data_app']
                                        time_diff = (next_start - current_end).total_seconds() / 60
                                        if time_diff > 29:
                                            slot_start = current_end
                                            while (slot_start + timedelta(minutes=30) <= next_start) and (slot_start.hour < 13):
                                                if slot_start >= start_time and slot_start < end_time:
                                                    free_slots.append(slot_start.strftime("%Y-%m-%d %H:%M"))
                                                slot_start += timedelta(minutes=30)
                                    if i == len(appointments) - 1:
                                        current_datetime = current_end.replace(hour=9, minute=0, second=0, microsecond=0)
                                        while current_datetime < end_time and current_datetime.weekday() in [1, 3, 4] and current_datetime.hour < 13:
                                            if current_datetime >= current_end and (end_time - current_datetime).total_seconds() / 60 >= 30:
                                                free_slots.append(current_datetime.strftime("%Y-%m-%d %H:%M"))
                                            current_datetime += timedelta(minutes=30)

                        # Добавление слотов для полных свободных дней после последней записи
                        if appointments:
                            last_datetime = appointments[-1]['data_end']
                            current_datetime = last_datetime.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
                            while current_datetime < end_time and len(free_slots) < 105:
                                weekday = current_datetime.weekday()
                                if weekday in [1, 3, 4]:
                                    slot_start = current_datetime
                                    while slot_start.hour < 13 and len(free_slots) < 105:
                                        free_slots.append(slot_start.strftime("%Y-%m-%d %H:%M"))
                                        slot_start += timedelta(minutes=30)
                                current_datetime += timedelta(days=1)

                        # Группировка и красивый вывод свободных слотов в text
                        if free_slots:
                            result["text"] += "Ближайшая свободная запись:\n"
                            current_day = None
                            slots_line = []
                            for slot in sorted(free_slots):
                                slot_datetime = datetime.strptime(slot, "%Y-%m-%d %H:%M")
                                if current_day != slot_datetime.date():
                                    if current_day is not None and slots_line:
                                        result["text"] += "  " + " | ".join(slots_line) + "\n"
                                    slots_line = []
                                    day_name = slot_datetime.strftime("%d %B, %A").replace("January", "января").replace("February", "февраля").replace("March", "марта").replace("April", "апреля").replace("May", "мая").replace("June", "июня").replace("July", "июля").replace("August", "августа").replace("September", "сентября").replace("October", "октября").replace("November", "ноября").replace("December", "декабря").replace("Monday", "понедельник").replace("Tuesday", "вторник").replace("Thursday", "четверг").replace("Friday", "пятница")
                                    result["text"] += f"{day_name}:\n"
                                    current_day = slot_datetime.date()
                                slots_line.append(slot_datetime.strftime("%H:%M"))
                            if slots_line:
                                result["text"] += "  " + " | ".join(slots_line) + "\n"
                        else:
                            result["text"] += "На ближайшие 150 дней свободной записи нет.\n"
                        result["free_slots"] = [datetime.strptime(slot, "%Y-%m-%d %H:%M").strftime("%H:%M") for slot in free_slots]  # Вернули формат времени
                    else:
                        result["text"] = "Данные о враче не найдены."
                    return result
            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_simonova_info: {str(e)}")
            result["text"] = "Ошибка при загрузке данных. Попробуйте позже."
            return result
        except Exception as e:
            logging.error(f"Unexpected error in get_simonova_info: {str(e)}")
            result["text"] = "Ошибка при загрузке данных. Попробуйте позже."
            return result

    async def send_booking_options(self, update, app, current_day_idx=0):
        """Отправка сообщения с опциями записи для указанного дня"""
        query = update.callback_query
        await query.answer()
        try:
            result = await self.get_simonova_info(app)
            if not result["text"].startswith("<b>"):
                await query.edit_message_text(text=result["text"], parse_mode='HTML')
                return
            # Парсим текст для определения дней и слотов
            lines = result["text"].split("\n")
            current_slots = []
            current_day = None
            for line in lines:
                day_match = re.match(r"(\d+ [а-я]+),?", line)
                if day_match:
                    if current_day and current_slots:
                        if current_day_idx == 0:
                            break
                        current_day_idx -= 1
                    current_day = day_match.group(1)
                    current_slots = []
                elif line.strip() and current_day and "|" in line:
                    slots = [s.strip() for s in line.split("|") if s.strip()]
                    current_slots.extend(slots)
                if current_day and current_day_idx == 0 and current_slots:
                    break
            if not current_day or not current_slots:
                logging.error("No valid day or slots found in text")
                await query.message.reply_text("Ошибка при определении даты.", parse_mode='HTML')
                return
                
            keyboard = []
            for i in range(0, len(current_slots), 5):
                row = [InlineKeyboardButton(text=time, callback_data=f"book_{current_day}_{time}") for time in current_slots[i:i+5]]
                keyboard.append(row)
            # Добавляем стрелки в один ряд
            navigation_row = []
            days_count = len([line for line in lines if re.match(r"\d+ [а-я]+", line)])
            if days_count > 1:  # Если есть следующие дни
                navigation_row.append(InlineKeyboardButton(text=">>", callback_data=f"next_day_{1}"))  # Переход ко второму дню
            if days_count > 1:  # Если есть последующий день, добавляем быструю перемотку
                navigation_row.append(InlineKeyboardButton(text=">>>", callback_data=f"last_day_{days_count - 1}"))
            if navigation_row:
                keyboard.append(navigation_row)
            else:
                logging.info("No navigation buttons to add")
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.reply_text(
                f"ближайшая свободная запись на {current_day}, выберите время",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logging.error(f"Error in send_booking_options: {str(e)}")
            await query.message.reply_text("Ошибка при обработке запроса.", parse_mode='HTML')

    async def send_booking_options_next(self, update, app, current_day_idx):
        """Отправка сообщения с опциями записи для следующего дня"""
        query = update.callback_query
        await query.answer()
        try:
            result = await self.get_simonova_info(app)
            if not result["text"].startswith("<b>"):
                await query.edit_message_text(text=result["text"], parse_mode='HTML')
                return
            lines = result["text"].split("\n")
            days = [line.strip() for line in lines if re.match(r"\d+ [а-я]+", line)]
            if current_day_idx >= len(days):
                await query.edit_message_text("Больше нет свободных дат.", parse_mode='HTML')
                return
            current_day = days[current_day_idx]
            booking_day = re.match(r"(\d+ [а-я]+),?", current_day).group(1)  # Новая переменная только с датой
            current_slots = []
            day_index = lines.index(current_day) if current_day in lines else -1
            if day_index != -1:
                for i in range(day_index + 1, len(lines)):
                    line = lines[i].strip()
                    if re.match(r"\d+ [а-я]+", line):  # Новый день
                        break
                    if line and not line.startswith("Ближайшая свободная запись:"):  # Игнорируем заголовок
                        slots = re.findall(r"\d{2}:\d{2}", line)  # Ищем время в формате HH:MM
                        if slots:
                            current_slots.extend(slots)
            if not current_day or not current_slots:
                logging.error(f"No slots found for day {current_day}, checked slots: {current_slots}")
                await query.edit_message_text("Ошибка при определении слотов.", parse_mode='HTML')
                return
            
            keyboard = []
            for i in range(0, len(current_slots), 5):
                row = [InlineKeyboardButton(text=time, callback_data=f"book_{booking_day}_{time}") for time in current_slots[i:i+5]]
                keyboard.append(row)
            # Добавляем стрелки в один ряд
            navigation_row = []
            # Логика в зависимости от позиции
            if current_day_idx == 0:  # Первый день
                if current_day_idx < len(days) - 1:
                    navigation_row.append(InlineKeyboardButton(text=">>", callback_data=f"next_day_{current_day_idx + 1}"))
                if len(days) > 1:
                    navigation_row.append(InlineKeyboardButton(text=">>>", callback_data=f"last_day_{len(days) - 1}"))
            elif current_day_idx == 1:  # Второй день
                if current_day_idx > 0:
                    navigation_row.append(InlineKeyboardButton(text="<<", callback_data=f"prev_day_{current_day_idx - 1}"))
                if current_day_idx < len(days) - 1:
                    navigation_row.append(InlineKeyboardButton(text=">>", callback_data=f"next_day_{current_day_idx + 1}"))
                if len(days) > 2:
                    navigation_row.append(InlineKeyboardButton(text=">>>", callback_data=f"last_day_{len(days) - 1}"))
            elif current_day_idx >= 2 and current_day_idx <= len(days) - 3:  # С 3-го по 3-й с конца
                navigation_row.append(InlineKeyboardButton(text="<<<", callback_data=f"first_day_0"))
                if current_day_idx > 0:
                    navigation_row.append(InlineKeyboardButton(text="<<", callback_data=f"prev_day_{current_day_idx - 1}"))
                if current_day_idx < len(days) - 1:
                    navigation_row.append(InlineKeyboardButton(text=">>", callback_data=f"next_day_{current_day_idx + 1}"))
                navigation_row.append(InlineKeyboardButton(text=">>>", callback_data=f"last_day_{len(days) - 1}"))
            elif current_day_idx == len(days) - 2:  # Предпоследний день
                navigation_row.append(InlineKeyboardButton(text="<<<", callback_data=f"first_day_0"))
                if current_day_idx > 0:
                    navigation_row.append(InlineKeyboardButton(text="<<", callback_data=f"prev_day_{current_day_idx - 1}"))
                if current_day_idx < len(days) - 1:
                    navigation_row.append(InlineKeyboardButton(text=">>", callback_data=f"next_day_{current_day_idx + 1}"))
            elif current_day_idx == len(days) - 1:  # Последний день
                navigation_row.append(InlineKeyboardButton(text="<<<", callback_data=f"first_day_0"))
                if current_day_idx > 0:
                    navigation_row.append(InlineKeyboardButton(text="<<", callback_data=f"prev_day_{current_day_idx - 1}"))
            if navigation_row:
                keyboard.append(navigation_row)
            else:
                logging.info("No navigation buttons to add")
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"свободная запись на {current_day} выберите время",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logging.error(f"Error in send_booking_options_next: {str(e)}")
            await query.message.reply_text("Ошибка при обработке запроса.", parse_mode='HTML')

    # async def handle_callback(self, update, app):
    #     """Обработка callback-запросов"""
    #     query = update.callback_query
    #     await query.answer()
    #     data = query.data
    #     if data.startswith("next_day_"):
    #         next_idx = int(data.split("_")[2])
    #         await self.send_booking_options(update, app, current_day_idx=next_idx)
    #     elif data.startswith("book_"):
    #         day_time = data.split("_")[1:]
    #         await query.message.reply_text(f"Забронировано на {day_time[0]} в {day_time[1]}.", parse_mode='HTML')


    async def get_service_price(self, service_query):
        """Ищет цену услуги в базе для филиалов с 'Касса_1' и возвращает среднее значение."""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            async with conn.cursor() as cursor:
                mapped_query = get_mapped_word(service_query)
                search_pattern = f"%{mapped_query}%"
                await cursor.execute(
                    "SELECT Цена FROM pricelist_global WHERE Услуга LIKE %s AND Услуга NOT LIKE %s AND Филиал LIKE 'Касса_1'",
                    (search_pattern, "Удалено | %%")
                )
                results = await cursor.fetchall()
                if not results:
                    return "Цена не найдена, уточните у администратора."

                prices = [price[0] for price in results]  # Извлекаем цену из одноэлементного кортежа
                if not prices:
                    return "Цена не найдена, уточните у администратора."

                avg_price = sum(prices) / len(prices)
                rounded_price = round(avg_price / 100) * 100
                return f"В нашей клинике {service_query} в среднем стоит: {rounded_price:.0f} руб."
            await conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_service_price: {str(e)}")
            return f"Ошибка при поиске цены (MySQL): {str(e)}. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_service_price: {str(e)}")
            return f"Ошибка при поиске цены: {str(e)}. Попробуйте позже."

    async def get_service_price_list(self, service_query):
        """Выводит список цен для услуги из базы для филиала 'Касса_1'."""
        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            async with conn.cursor() as cursor:
                mapped_query = get_mapped_word(service_query)
                search_pattern = f"%{mapped_query}%"
                await cursor.execute(
                    "SELECT Наименование, Цена FROM pricelist_global WHERE Наименование LIKE %s AND Услуга NOT LIKE %s AND Филиал LIKE 'Касса_1'",
                    (search_pattern, "Удалено | %%")
                )
                results = await cursor.fetchall()
                if not results:
                    return "Цены для этой услуги не найдены, уточните у администратора."

                response = ["Вот список цен по Вашему запросу:"]
                for service, price in results:
                    response.append(f"{service} - {price} руб.")

                return "\n".join(response) if response else "Список цен не найден."
            await conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in get_service_price_list: {str(e)}")
            return f"Ошибка при загрузке списка цен (MySQL): {str(e)}. Попробуйте позже."
        except Exception as e:
            logging.error(f"Unexpected error in get_service_price_list: {str(e)}")
            return f"Ошибка при загрузке списка цен: {str(e)}. Попробуйте позже."

    async def handle_price_query(self, update, app, typing_duration):
        """Обрабатывает запрос цены из сообщения."""
        message_text = update.message.text.lower()

        # Ищем фразу: "прайс на <услуга>", "все цены на <услуга>", "покажи все цены <услуга>", "покажи прайс <услуга>", "покажи цены на <услуга>", "цены на <услуга>"
        price_list_match = re.search(
            r"(?i)(?:прайс|все\s+цены|покажи\s+все\s+цены|покажи\s+прайс|покажи\s+цены|цены)(?:\s+на)?\s+([\w\s]+)",
            message_text
        )
        if price_list_match:
            service_query = price_list_match.group(1)
            await app.bot.send_chat_action(chat_id=update.message.chat.id, action=ChatAction.TYPING)
            await asyncio.sleep(typing_duration)
            price_response = await self.get_service_price_list(service_query)
            await app.bot.send_message(chat_id=update.message.chat.id, text=price_response, parse_mode='HTML')
            return True

        # Ищем фразу: "сколько/почём/стоимость <услуга>", "скажите/подскажите цена/цену/цены/стоимость <услуга>", или "<услуга> впереди"
        match = re.search(
            r"(?i)(?:сколько\s+(?:стоит|стоят)\s+(\w+)|поч[её]м(?:\s+у\s+вас)?\s+(\w+)|стоимость\s+(\w+)|"
            r"(?:скажите|подскажите|скажи)\s+(?:цена|цену|цены|стоимость)\s+(?:на)?\s+([а-яё]+)|"
            r"(\w+)\s+(?:сколько\s+(?:стоит|стоят)|поч[её]м(?:\s+у\s+вас)?|стоимость)|"
            r"(\w+)\s+(?:скажите|подскажите|скажи)\s+(?:цена|цену|цены|стоимость))",
            message_text
        )
        if match:
            service_query = next((group for group in match.groups() if group), None)
            if service_query:
                await app.bot.send_chat_action(chat_id=update.message.chat.id, action=ChatAction.TYPING)
                await asyncio.sleep(typing_duration)
                price_response = await self.get_service_price(service_query)
                await app.bot.send_message(chat_id=update.message.chat.id, text=price_response)
                return True
        return False

    async def handle_start(self, update, app):
        """Обработка команды /start для регистрации или подтверждения подписки"""
        message = update.message
        from_user = message.from_user
        chat_id = message.chat.id
        telegram_id = str(from_user.id)
        link_txt = message.text.strip().split()[1] if len(message.text.strip().split()) > 1 else ""
        # logging.info(f"Debug: handle_start - chat_id={chat_id}, telegram_id={telegram_id}, link_txt={link_txt}, username={from_user.username}")

        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor() as cursor:
                    text_login = ""
                    text = ""

                    if not link_txt:
                        # Проверка наличия Telegram ID в базе
                        await cursor.execute(
                            "SELECT user_id FROM profiles WHERE telegram = %s LIMIT 1",
                            (telegram_id,)
                        )
                        result = await cursor.fetchone()
                        res_telegram_id = result[0] if result else None

                        if res_telegram_id:
                            text_login = "Вы уже подписались на уведомления своего аккаунта."
                            text = f"☝️ Пользователь @{from_user.username}, вернулся в бота."
                        else:
                            text_login = f"{from_user.first_name}, чтобы подписаться, подключите бота к своему аккаунту в разделе «Профиль / Настройки (https://izdenta.com/user/profile)» контрольной панели izdenta."
                            text = f"☝️ Бота добавил новый пользователь @{from_user.username} [Гость]"
                    else:
                        # Проверка ключа telegram_key
                        await cursor.execute(
                            "SELECT user_id, fullname FROM profiles WHERE telegram_key = %s LIMIT 1",
                            (link_txt,)
                        )
                        result = await cursor.fetchone()
                        if result:
                            user_tlg_key, fullname = result
                            text_login = (f"Здравствуйте, {fullname}, Вы подписались на уведомления своего аккаунта. "
                                          "Мы рады, что вы с нами! Сеть клиник «Жемчужинка» в Ижевске представляет все виды "
                                          "стоматологических услуг любой степени сложности. 🦷✨\n"
                                          "Наши высококвалифицированные специалисты предложат наиболее подходящее для Вас лечение!\n"
                                          "Если у вас есть вопросы или вы хотите записаться на прием, используйте вспомогательные "
                                          "кнопки для управления ботом или оставьте свой вопрос в разделе «Сообщения» – мы всегда "
                                          "готовы помочь вам на пути к здоровой и красивой улыбке!\n"
                                          "С заботой\n"
                                          "Ваша команда «Жемчужинка»")
                            text = f"🔥 Эврика! На уведомления через @IzdentaBot подписался новый пользователь izdenta.com {fullname}, @{from_user.username}"

                            # Обновление telegram в базе
                            await cursor.execute(
                                "UPDATE profiles SET telegram = %s WHERE user_id = %s",
                                (telegram_id, user_tlg_key)
                            )
                            await conn.commit()
                        else:
                            text_login = "Неверный ключ подключения. Попробуйте снова или свяжитесь с поддержкой."
                            text = f"☝️ Пользователь @{from_user.username} попытался подключиться с неверным ключом: {link_txt}"

                    # Отправка пользователю с клавиатурой
                    keyboard = [
                        ['📅 Запись', '💰 Цены'],
                        ['☎️ Контакты', '🏥 Клиника'],
                        ['🗓 Моя запись']
                    ]
                    reply_markup = ReplyKeyboardMarkup(
                        keyboard=keyboard,
                        resize_keyboard=True,
                        one_time_keyboard=False,
                        selective=True
                    )
                    await app.bot.send_message(chat_id=chat_id, text=text_login, parse_mode='HTML', reply_markup=reply_markup)

                    # Отправка админу с обработкой ошибки
                    try:
                        await app.bot.send_message(chat_id='365192641', text=text, parse_mode='HTML')
                        # logging.info(f"Message sent to admin 365192641")
                    except Exception as e:
                        logging.error(f"Failed to send to admin 365192641: {str(e)}")

                    # Отправка сотрудникам в Общую  +7 904 276 3483 Жемчужинка Телеграм
                    # try:
                    #     await app.bot.send_message(chat_id='1512892764', text=text, parse_mode='HTML')
                    #     logging.info(f"Message sent to staff 1512892764")
                    # except Exception as e:
                    #     logging.error(f"Failed to send to staff 1512892764: {str(e)}")

            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in handle_start: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке команды. Попробуйте позже.")
        except Exception as e:
            logging.error(f"Unexpected error in handle_start: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке команды. Попробуйте позже.")
    
    async def handle_my_appointment(self, update, app):
        """Обработка команды '🗓 Моя запись'"""
        telegram_id = str(update.message.from_user.id)
        chat_id = update.message.chat.id
        username = update.message.from_user.username or "Неизвестный"
        first_name = update.message.from_user.first_name or "Неизвестный"

        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor() as cursor:
                    # Проверка наличия Telegram ID в базе
                    await cursor.execute("SELECT user_id FROM profiles WHERE telegram = %s LIMIT 1", (telegram_id,))
                    result = await cursor.fetchone()
                    user_telegram_id = result[0] if result else None

                    text_login = ""
                    text_admin = ""

                    if user_telegram_id:
                        # Поиск ближайшей записи на приём
                        await cursor.execute(
                            "SELECT `id`, `id_spec`, `fullname`, `data_app` FROM `appoint` "
                            "WHERE (DATE(data_app) > DATE_ADD(NOW(), INTERVAL 0 HOUR) AND DATE(data_app) < DATE_ADD(CURDATE(), INTERVAL 2 MONTH)) "
                            "AND `deleted` = 0 AND id_patient = %s ORDER BY `data_app` ASC LIMIT 1",
                            (user_telegram_id,)
                        )
                        result = await cursor.fetchone()
                        if result:
                            app_id, spec_id, fullname, data_app = result
                            data_app = data_app.strftime("%d.%m.%Y в %H:%M")

                            # Поиск ФИО сотрудника
                            await cursor.execute(
                                "SELECT `fullname`, `telegram` FROM profiles WHERE user_id = %s LIMIT 1",
                                (spec_id,)
                            )
                            spec_result = await cursor.fetchone()
                            name_spec = spec_result[0] if spec_result else "Неизвестный врач"
                            telegram_id_spec = spec_result[1] if spec_result else None

                            text_login = f"☝️ {fullname}, Вы записаны на приём к {name_spec}\n"
                            text_login += f"на {data_app}\n"
                            text_login += "нажмите /confirm для подтверждения\n\n"
                            text_login += "или /cancelvisit - если Ваши планы изменились. Спасибо!"

                            text_admin = f"☝️ Пользователь @{username} [{fullname}] увидел свою запись на приём"
                        else:
                            text_login = f"☝️ @{username}, у меня отсутствуют записи на приём, связанные с Вашим ID (с этой секунды +2 месяца)"
                            text_admin = f"☝️ Пользователь @{username} [Auth] не нашел свою запись на приём"
                    else:
                        text_login = f"{first_name}, я могу отправить Вам Вашу запись на приём, если узнаю Вас. Для этого - подключите бота к своему аккаунту в разделе «Профиль / Настройки (https://izdenta.com/user/profile)» контрольной панели izdenta."
                        text_admin = f"☝️ Пользователь @{username} [Гость] пытается подружиться с ботом и ищет свои записи на приём"

                    # Отправка пользователю
                    await app.bot.send_message(chat_id=chat_id, text=text_login, parse_mode='HTML')

                    # Отправка админу
                    await app.bot.send_message(chat_id='365192641', text=text_admin + "\n" + text_login, parse_mode='HTML')

                    # Отправка сотрудникам (отключено)
                    # await app.bot.send_message(chat_id='6575779900', text=text_admin, parse_mode='HTML')

                    # Отправка врачу (отключено)
                    # if telegram_id_spec:
                    #     await app.bot.send_message(chat_id=telegram_id_spec, text=text_admin, parse_mode='HTML')

            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in handle_my_appointment: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при загрузке данных. Попробуйте позже.")
        except Exception as e:
            logging.error(f"Unexpected error in handle_my_appointment: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при загрузке данных. Попробуйте позже.")

    async def handle_confirm(self, update, app):
        """Обработка команды /confirm для подтверждения записи"""
        telegram_id = str(update.message.from_user.id)
        chat_id = update.message.chat.id
        username = update.message.from_user.username or "Неизвестный"
        fullname = update.message.from_user.full_name or "Неизвестный"

        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor() as cursor:
                    # Проверка наличия Telegram ID в базе
                    await cursor.execute("SELECT user_id FROM profiles WHERE telegram = %s LIMIT 1", (telegram_id,))
                    result = await cursor.fetchone()
                    user_tlg_id = result[0] if result else None

                    text = ""
                    text1 = ""

                    if user_tlg_id:
                        # Поиск записи на приём за последние 3 дня
                        await cursor.execute(
                            "SELECT `id`, `id_spec`, `fullname`, `data_app` FROM `appoint` "
                            "WHERE DATE(data_app) BETWEEN DATE(CURDATE()) AND DATE_ADD(CURDATE(), INTERVAL 3 DAY) "
                            "AND `deleted` = 0 AND id_patient = %s LIMIT 1",
                            (user_tlg_id,)
                        )
                        result = await cursor.fetchone()
                        if result:
                            app_id, spec_id, fullname, data_app = result
                            data_app = data_app.strftime("%d.%m.%Y в %H:%M")

                            # Поиск Telegram ID сотрудника
                            await cursor.execute(
                                "SELECT telegram FROM profiles WHERE user_id = %s LIMIT 1",
                                (spec_id,)
                            )
                            spec_result = await cursor.fetchone()
                            telegram_id_spec = spec_result[0] if spec_result else None

                            # Обновление firstvisit
                            await cursor.execute(
                                "UPDATE appoint SET firstvisit = 1 WHERE id = %s",
                                (app_id,)
                            )
                            await conn.commit()

                            if data_app:
                                text = f"Спасибо за оповещение!\n{fullname} [@{username}], Вы подтвердили явку на приём по записи на {data_app}"
                                text1 = f"Пациент {fullname} [@{username}] подтвердил явку на приём по записи на {data_app}"
                            else:
                                text = "Спасибо, мы приняли Ваш ответ.\n\n"
                                text1 = f"Пациент [@{username}] подтвердил явку на приём. Но либо слишком рано, либо уже поздно."
                        else:
                            text = "Спасибо, мы приняли Ваш ответ.\n\n"
                            text1 = f"Пациент [@{username}] подтвердил явку, но запись не найдена."
                    else:
                        text = "Спасибо, мы приняли Ваш ответ.\n\n"
                        text1 = f"Пациент [@{username}] попытался подтвердить явку, но не зарегистрирован."

                    # Отправка пользователю
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')

                    # Отправка админу
                    await app.bot.send_message(chat_id='365192641', text=text1, parse_mode='HTML')

                    # Отправка сотрудникам @StomClinica
                    await app.bot.send_message(chat_id='6575779900', text=text1, parse_mode='HTML')

                    # Отправка врачу
                    if telegram_id_spec:
                        await app.bot.send_message(chat_id=telegram_id_spec, text=text1, parse_mode='HTML')

            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in handle_confirm: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке подтверждения. Попробуйте позже.")
        except Exception as e:
            logging.error(f"Unexpected error in handle_confirm: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке подтверждения. Попробуйте позже.")

    async def handle_cancelvisit(self, update, app):
        """Обработка команды /cancelvisit для отмены записи"""
        telegram_id = str(update.message.from_user.id)
        chat_id = update.message.chat.id
        username = update.message.from_user.username or "Неизвестный"
        fullname = update.message.from_user.full_name or "Неизвестный"

        try:
            conn = await aiomysql.connect(**DB_CONFIG)
            try:
                async with conn.cursor() as cursor:
                    # Проверка наличия Telegram ID в базе
                    await cursor.execute("SELECT user_id FROM profiles WHERE telegram = %s LIMIT 1", (telegram_id,))
                    result = await cursor.fetchone()
                    user_tlg_id = result[0] if result else None

                    text = ""
                    text1 = ""

                    if user_tlg_id:
                        # Поиск записи на приём за последние 3 дня
                        await cursor.execute(
                            "SELECT `id`, `id_spec`, `fullname`, `data_app` FROM `appoint` "
                            "WHERE DATE(data_app) BETWEEN DATE(CURDATE()) AND DATE_ADD(CURDATE(), INTERVAL 3 DAY) "
                            "AND `deleted` = 0 AND id_patient = %s LIMIT 1",
                            (user_tlg_id,)
                        )
                        result = await cursor.fetchone()
                        if result:
                            user_id, id_spec, fullname, data_app = result
                            data_app = data_app.strftime("%d.%m.%Y в %H:%M")

                            # Поиск Telegram ID врача
                            await cursor.execute(
                                "SELECT telegram FROM profiles WHERE user_id = %s LIMIT 1",
                                (id_spec,)
                            )
                            spec_result = await cursor.fetchone()
                            telegram_id_spec = spec_result[0] if spec_result else None
                            # logging.info(f"Debug: telegram_id_spec={telegram_id_spec}")

                            # Обновление записи с пометкой об удалении
                            if user_id:
                                await cursor.execute(
                                    "UPDATE appoint SET note_couse = %s, deleted = 1 WHERE id = %s",
                                    ("Пациент удалил свою запись на прием через Telegram, в последний момент", user_id)
                                )
                                await conn.commit()

                            if data_app:
                                text = f"{fullname} [@{username}], Вы отменили явку на приём по записи на {data_app}\nСожалеем об этом! Спасибо, что предупредили."
                                text1 = f"🤮 Пациент {fullname} [@{username}] отменил явку на приём по записи на {data_app}\n"
                                text1 += "Его запись перемещена в удаленные с примечанием."
                            else:
                                text = "Спасибо, мы приняли Ваш ответ."
                                text1 = f"🤮 Пациент [@{username}] отменил явку на приём. Но сделал это слишком поздно, потому уже и не актуально."
                        else:
                            text = "Спасибо, мы приняли Ваш ответ.\n\n"
                            text1 = f"Пациент [@{username}] попытался отменить запись, но она не найдена."
                    else:
                        text = "Спасибо, мы приняли Ваш ответ.\n\n"
                        text1 = f"Пациент [@{username}] попытался отменить запись, но не зарегистрирован."

                    # Отправка пользователю
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')

                    # Отправка админу с обработкой ошибки
                    try:
                        await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text1, parse_mode='HTML')
                        # logging.info(f"Message sent to admin")
                    except Exception as e:
                        logging.error(f"Failed to send to admin: {str(e)}")

                    # Отправка сотрудникам @StomClinica
                    try:
                        await app.bot.send_message(chat_id=STOMCLINICA_CHAT_ID, text=text1, parse_mode='HTML')
                        # logging.info(f"Message sent to staff")
                    except Exception as e:
                        logging.error(f"Failed to send to staff: {str(e)}")

                    # Отправка врачу с обработкой ошибки
                    if telegram_id_spec:
                        try:
                            await app.bot.send_message(chat_id=telegram_id_spec, text=text1, parse_mode='HTML')
                            # logging.info(f"Message sent to doctor {telegram_id_spec}")
                        except Exception as e:
                            logging.error(f"Failed to send to doctor {telegram_id_spec}: {str(e)}")

            finally:
                conn.close()
        except aiomysql.Error as e:
            logging.error(f"MySQL error in handle_cancelvisit: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке отмены. Попробуйте позже.")
        except Exception as e:
            logging.error(f"Unexpected error in handle_cancelvisit: {str(e)}")
            await app.bot.send_message(chat_id=chat_id, text="Ошибка при обработке отмены. Попробуйте позже.")

    async def send_email(self, message):
        """Отправляет email на несколько адресов из EMAIL_RECIPIENTS."""
        if not EMAIL_RECIPIENTS:
            logging.error("EMAIL_RECIPIENTS is empty, skipping email sending")
            return

        msg = MIMEText(message, 'plain', 'utf-8')
        msg['Subject'] = 'Отзыв пациента через @IzdentaBot'
        msg['From'] = SMTP_USER
        msg['To'] = ', '.join(EMAIL_RECIPIENTS)  # Для заголовка письма

        try:
            # logging.debug(f"Starting email sending to {EMAIL_RECIPIENTS}")
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                for recipient in EMAIL_RECIPIENTS:
                    try:
                        if not isinstance(recipient, str) or not recipient.strip():
                            logging.error(f"Invalid email recipient: {recipient}, skipping")
                            continue
                        server.sendmail(SMTP_USER, recipient, msg.as_string())
                        # logging.info(f"Email successfully sent to {recipient}")
                        await asyncio.sleep(0.5)  # Задержка для предотвращения спам-фильтров
                    except Exception as e:
                        logging.error(f"Failed to send email to {recipient}: {str(e)}")
            # logging.info("Completed email sending")
        except Exception as e:
            logging.error(f"Error in send_email: {str(e)}")

# Создание экземпляра класса для использования
stomat = Stomat()