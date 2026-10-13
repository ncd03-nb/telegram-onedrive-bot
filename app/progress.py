"""Edit one Telegram message at a fixed interval, including during slow chunks."""
import asyncio
import time


class Progress:
    def __init__(self, telegram, chat, message_id, job_id, interval=15):
        self.telegram, self.chat, self.message_id = telegram, chat, message_id
        self.job_id, self.interval = job_id, interval
        self.count, self.total, self.destination = 0, None, ""
        self.started = time.monotonic()
        self.task = None

    def text(self, finished=False):
        pct = 100 if finished else (min(99, self.count * 100 / self.total) if self.total else 0)
        phase = "Hoàn tất" if finished else "Đang tải → upload OneDrive"
        total = f"{self.total / 1024**3:.2f} GiB" if self.total is not None else "chưa biết dung lượng"
        return (f"{self.job_id} — {phase}\nTiến độ: {pct:.1f}%\n"
                f"{self.count / 1024**3:.2f} GiB / {total}\n"
                f"Thời gian: {int(time.monotonic() - self.started)} giây")

    async def update(self, count, total, dest):
        self.count, self.total, self.destination = count, total, dest

    async def render(self, text=None):
        try:
            await self.telegram.edit(self.chat, self.message_id, text or self.text())
        except Exception:
            # Delivery failures must not abort a multi-GB upload or leak an API URL.
            pass

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.render()

    async def start(self):
        await self.render()
        self.task = asyncio.create_task(self.loop())

    async def close(self, final=None, finished=False):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.render(final or self.text(finished))
