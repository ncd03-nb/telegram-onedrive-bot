import asyncio

import aiohttp


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=50), trust_env=False)

    async def close(self):
        await self.session.close()

    async def call(self, method, **params):
        for attempt in range(3):
            async with self.session.post(self.base + method, json=params) as r:
                obj = await r.json()
                if r.status == 429:
                    await asyncio.sleep(min(obj.get("parameters", {}).get("retry_after", 3), 30))
                    continue
                if not obj.get("ok"):
                    # Do not expose a token, signed URL, arbitrary API response or exception URL.
                    raise TelegramError(f"Telegram {method} HTTP {r.status}")
                return obj["result"]
        raise TelegramError("Telegram giới hạn tần suất.")

    async def send(self, chat, text):
        return await self.call("sendMessage", chat_id=chat, text=text[:4000],
            link_preview_options={"is_disabled": True})

    async def edit(self, chat, message_id, text):
        return await self.call("editMessageText", chat_id=chat, message_id=message_id,
            text=text[:4000], link_preview_options={"is_disabled": True})
