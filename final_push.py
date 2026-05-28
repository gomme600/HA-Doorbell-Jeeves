import asyncio
import aiohttp
import sys

URL = "https://homeassistant.gomme600.ovh"
CLIENT_ID = "https://homeassistant.gomme600.ovh/"

async def get_token():
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{URL}/auth/login_flow", json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}) as resp:
            data = await resp.json()
            flow_id = data.get("flow_id")
        async with session.post(f"{URL}/auth/login_flow/{flow_id}", json={"client_id": CLIENT_ID, "username": "gomme600", "password": "claudetest"}) as resp:
            data = await resp.json()
            code = data.get("result")
        async with session.post(f"{URL}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as resp:
            data = await resp.json()
            return data.get("access_token")

async def main():
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{URL}/api/states") as resp:
            states = await resp.json()
            update_entity = next((s['entity_id'] for s in states if s['entity_id'].startswith('update.') and 'jeeves' in s['entity_id'].lower()), None)
        
        if update_entity:
            print(f"Updating {update_entity}...")
            await session.post(f"{URL}/api/services/update/install", json={"entity_id": update_entity})
            await asyncio.sleep(15)
        
        print("Restarting HA...")
        await session.post(f"{URL}/api/services/homeassistant/restart")
        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
