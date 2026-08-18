import asyncio

from curl_cffi.requests import AsyncSession

from config import PlatformConfig

# A single pooled session, reused across every request (including retries and
# concurrent city-wide fan-outs) instead of opening a fresh TLS connection
# per call -- this is what actually makes the city-wide searches fast.
_session: AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(impersonate: str) -> AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = AsyncSession(impersonate=impersonate, max_clients=32)
    return _session


async def close_session() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


class InventoryClient:
    def __init__(self, config: PlatformConfig, impersonate: str = "chrome124"):
        self.config = config
        self.impersonate = impersonate

    async def request(
        self,
        path: str,
        method: str = "GET",
        params: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        retries: int = 3,
        retry_delay: float = 1.5,
    ) -> dict:
        session = await _get_session(self.impersonate)
        last_error: dict | None = None
        for attempt in range(retries):
            resp = await session.request(
                method,
                self.config.base_url + path,
                headers=headers if headers is not None else self.config.headers,
                params=params,
                json=json,
            )

            # Back off and retry when the edge throttles us rather than failing
            # the whole column: Zepto 429s hard on a city-wide fan-out.
            if resp.status_code in (429, 503):
                last_error = {"statusCode": resp.status_code}
                self.on_throttle()
                if attempt < retries - 1:
                    retry_after = resp.headers.get("retry-after")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else retry_delay * 2 ** attempt
                    await asyncio.sleep(min(wait, 20))
                    continue

            resp.raise_for_status()
            body = resp.json()

            # The edge layer sometimes returns HTTP 200 with a synthetic
            # error body when the backend fetch was blocked/rate-limited.
            if "data" not in body and "statusCode" in body:
                last_error = body
                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay)
                continue
            return body

        raise RuntimeError(f"Request failed after {retries} attempts: {last_error}")

    def on_throttle(self) -> None:
        """Called when the edge rate-limits us. Platforms that pace themselves
        override this to widen their interval."""

    def parse_inventory(self, raw: dict) -> list[dict]:
        raise NotImplementedError
