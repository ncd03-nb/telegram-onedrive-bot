import asyncio
import unittest

from app.transfer import buffered_source
from app.transfer import Transfer
from app.settings import Settings


class BufferTests(unittest.IsolatedAsyncioTestCase):
    def test_upload_transport_can_be_rolled_back(self):
        settings = Settings(token='test', users={1})
        self.assertIn('--disable-http2', Transfer(settings, None).flags())
        settings.onedrive_disable_http2 = False
        self.assertNotIn('--disable-http2', Transfer(settings, None).flags())

    async def test_download_continues_while_upload_pauses_and_remains_bounded(self):
        read = 0

        class Source:
            async def iter_chunked(self, _):
                nonlocal read
                for n in range(20):
                    read += 1
                    yield bytes([n]) * 1024**2

        async with buffered_source(Source(), 2) as chunks:
            first = await anext(chunks)
            await asyncio.sleep(0.02)  # Simulate an outstanding upload request.
            self.assertGreater(read, 1)
            self.assertLessEqual(read, 4)  # two queued + consumer + producer
            received = [first, *[data async for data in chunks]]
        self.assertEqual(b''.join(received), b''.join(bytes([n]) * 1024**2 for n in range(20)))

    async def test_upstream_failure_propagates_after_buffered_bytes(self):
        class Source:
            async def iter_chunked(self, _):
                yield b'x' * 1024**2
                raise OSError('source disconnected')

        async with buffered_source(Source(), 2) as chunks:
            self.assertEqual(len(await anext(chunks)), 1024**2)
            with self.assertRaises(OSError):
                await anext(chunks)

    async def test_early_exit_cancels_producer(self):
        stopped = asyncio.Event()

        class Source:
            async def iter_chunked(self, _):
                try:
                    yield b'x' * 1024**2
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        async with buffered_source(Source(), 2) as chunks:
            await anext(chunks)
        self.assertTrue(stopped.is_set())
