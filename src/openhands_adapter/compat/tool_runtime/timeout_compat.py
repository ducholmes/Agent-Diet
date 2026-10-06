"""The Bash polling timeout for prepared images with Python 3.10.

Python 3.11+ uses asyncio.timeout unchanged. The fallback only encloses the
source polling loop; it cancels that task at the same deadline and converts
its own cancellation to TimeoutError. External cancellation still propagates.
"""
import asyncio


class _PollingTimeout:
    def __init__(self, seconds):
        self.seconds = seconds
        self.expired = False

    async def __aenter__(self):
        self.task = asyncio.current_task()
        self.handle = asyncio.get_running_loop().call_later(self.seconds, self._expire)
        return self

    def _expire(self):
        self.expired = True
        self.task.cancel()

    async def __aexit__(self, exc_type, exc, traceback):
        self.handle.cancel()
        if self.expired and exc_type is asyncio.CancelledError:
            raise asyncio.TimeoutError from exc
        return False


timeout = getattr(asyncio, "timeout", _PollingTimeout)
