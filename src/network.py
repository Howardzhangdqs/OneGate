from __future__ import annotations

import aiohttp


async def fetch_public_ip() -> str:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://ifconfig.me/ip") as resp:
            return (await resp.text()).strip()
