import unittest
from unittest.mock import AsyncMock
from app.progress import Progress


class ArchiveProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_overall_percent_does_not_reset_when_download_becomes_upload(self):
        tg=type('Telegram',(),{'edit':AsyncMock()})()
        p=Progress(tg,1,2,'fixture');p.percent_span=25
        await p.update(100,100,'file');self.assertEqual(p.percent(),25)
        p.percent_base=25;p.percent_span=70
        await p.update(0,100,'file');self.assertEqual(p.percent(),25)
        await p.update(50,100,'file');self.assertEqual(p.percent(),60)
        await p.update(100,100,'file');self.assertEqual(p.percent(),95)
        p.percent_base=95;p.percent_span=0
        self.assertEqual(p.percent(),95)
        self.assertEqual(p.percent(finished=True),100)
