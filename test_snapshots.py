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

async def test_snapshot(camera_id):
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        print(f"Testing snapshot for {camera_id}...")
        async with session.get(f"{URL}/api/camera_proxy/{camera_id}") as resp:
            print(f"Status: {resp.status}")
            if resp.status == 200:
                data = await resp.read()
                print(f"Success! Got {len(data)} bytes of JPEG.")
            else:
                print(f"Failed! Error: {await resp.text()}")

async def main():
    cameras = ["camera.carport_fluent", "camera.entree_fluent_lens_0", "camera.terasse_fluent"]
    for cam in cameras:
        await test_snapshot(cam)

if __name__ == "__main__":
    asyncio.run(main())
