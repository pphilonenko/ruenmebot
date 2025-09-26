import aiohttp
import random
import logging
from config import GROK_API_KEY, GROK_API_URL
from my_info import ERROR_RESPONSES
from utils import notify_development

async def query_grok(query, system_prompt=None, user_info=None, bot=None):
    timeout = aiohttp.ClientTimeout(total=10)  # 10 секунд
    async with aiohttp.ClientSession(timeout=timeout) as session:
        headers = {
            "Authorization": f"Bearer {GROK_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "grok-3",
            "messages": [
                {"role": "system", "content": system_prompt} if system_prompt else None,
                {"role": "user", "content": query}
            ],
            "temperature": 0.7
        }
        data["messages"] = [msg for msg in data["messages"] if msg is not None]
        try:
            logging.info(f"Sending Grok API request: {query}")
            async with session.post(GROK_API_URL, headers=headers, json=data) as response:
                if response.status != 200:
                    error_msg = f"Network issue connecting to Grok AI: {response.status}"
                    await notify_development(error_msg, user_info, bot=bot)
                    return random.choice(ERROR_RESPONSES)
                result = await response.json()
                logging.info(f"Grok API response: {result}")
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            error_msg = f"Network issue connecting to Grok AI: {str(e)}"
            logging.error(f"Grok API error: {error_msg}")
            await notify_development(error_msg, user_info, bot=bot)
            return random.choice(ERROR_RESPONSES)