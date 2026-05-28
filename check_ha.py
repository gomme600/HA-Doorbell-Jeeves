import asyncio
import aiohttp
from scripts.e2e_test_suite import get_token, URL

async def check():
    try:
        t = await get_token()
        print("Token OK")
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {t}"}) as s:
            async with s.get(f"{URL}/api/config") as r:
                print("Config status:", r.status, await r.json())
    except Exception as e:
        print("Error:", e)

asyncio.run(check())
