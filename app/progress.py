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
        self.finished = False

    def text(self, finished=False):
        pct = 100 if finished else (min(99, self.count * 100 / self.total) if self.total else 0)
        phase = "Hoàn tất" if finished else ("Đang xác minh OneDrive" if self.total is not None and self.count >= self.total else "Đang tải → upload OneDrive")
        total = f"{self.total / 1024**3:.2f} GiB" if self.total is not None else "chưa biết dung lượng"
        elapsed = max(0.1,time.monotonic()-self.started)
        speed = self.count/elapsed
        eta = f"\nƯớc tính còn: {int((self.total-self.count)/speed/60)} phút" if speed and self.total and self.count<self.total else ""
        return (f"{self.job_id} — {phase}\nTiến độ: {pct:.1f}%\n"
                f"{self.count / 1024**3:.2f} GiB / {total}\n"
                f"Tốc độ TB: {speed/1024**2:.2f} MiB/s\nThời gian: {int(elapsed)} giây" + eta)

    async def update(self, count, total, dest):
        self.count, self.total, self.destination = count, total, dest

    async def render(self, text=None):
        delivered = False
        try:
            await self.telegram.edit(self.chat, self.message_id, text or self.text())
            delivered = True
        except Exception:
            # Delivery failures must not abort a multi-GB upload or leak an API URL.
            pass
        # Useful in Actions logs even if Telegram cannot be reached. Never log URLs or secrets.
        pct = 100 if self.finished else (min(99, self.count * 100 / self.total) if self.total else 0)
        print(f"Progress: {pct:.1f}% | bytes={self.count} | total={self.total} | "
              f"elapsed={int(time.monotonic() - self.started)}s | telegram_edit={'ok' if delivered else 'failed'}", flush=True)

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
        self.finished = finished
        await self.render(final or self.text(finished))
