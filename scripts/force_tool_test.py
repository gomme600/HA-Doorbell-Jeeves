"""
E2E AI Test Suite for Doorbell Jeeves - TOOL CALLER EDITION.
This script focuses on bypassing the AI's chatty nature by using highly direct
instructional prompts to force tool execution.
"""

import pyttsx3
import wave
import audioop
import base64
import asyncio
import aiohttp
import sys
import os
import time
import json

# --- Configuration ---
URL = "https://homeassistant.gomme600.ovh"
WS_URL = "wss://homeassistant.gomme600.ovh/api/websocket"
CLIENT_ID = "https://homeassistant.gomme600.ovh"
USERNAME = "gomme600"
PASSWORD = "claudetest"

# --- Audio Utilities ---

def generate_pcm(text):
    """Generate 16kHz mono 16-bit PCM from text using Windows TTS."""
    print(f"  [TTS OUT] Generating: '{text}'")
    engine = pyttsx3.init()
    temp_wav = f"temp_{int(time.time() * 1000)}.wav"
    engine.save_to_file(text, temp_wav)
    engine.runAndWait()
    
    with wave.open(temp_wav, 'rb') as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        data = wf.readframes(nframes)
        
    if nchannels == 2:
        data = audioop.tomono(data, sampwidth, 0.5, 0.5)
        
    if framerate != 16000:
        data, _ = audioop.ratecv(data, sampwidth, 1, framerate, 16000, None)
    
    if os.path.exists(temp_wav):
        os.remove(temp_wav)
    return data

async def send_audio_stream(session, pcm_data):
    """Send PCM data to HA send_audio service at a real-time rate."""
    CHUNK_SIZE = 4096
    INTERVAL = 0.128
    
    chunks = [pcm_data[i:i+CHUNK_SIZE] for i in range(0, len(pcm_data), CHUNK_SIZE)]
    print(f"  [Audio] Streaming {len(chunks)} chunks...")
    
    for i, chunk in enumerate(chunks):
        chunk_b64 = base64.b64encode(chunk).decode('ascii')
        payload = {"audio_base64": chunk_b64}
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/send_audio", json=payload) as resp:
            pass
        if i < len(chunks) - 1:
            await asyncio.sleep(INTERVAL)

class JeevesMonitor:
    def __init__(self, ws):
        self.ws = ws
        self.tool_calls = []
        self.msg_id = 1

    async def listen(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "event":
                    event = data.get("event", {})
                    if event.get("event_type") == "ha_doorbell_jeeves_tool_call":
                        event_data = event.get("data", {})
                        func = event_data.get("function")
                        print(f"  [Tool Call Detected] {func}")
                        self.tool_calls.append(event_data)

# --- HA API Utilities ---

async def get_token():
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{URL}/auth/login_flow", json={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}) as resp:
            data = await resp.json()
            flow_id = data.get("flow_id")
        async with session.post(f"{URL}/auth/login_flow/{flow_id}", json={"client_id": CLIENT_ID, "username": USERNAME, "password": PASSWORD}) as resp:
            data = await resp.json()
            code = data.get("result")
        async with session.post(f"{URL}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as resp:
            data = await resp.json()
            return data.get("access_token")

async def run_suite():
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(WS_URL) as ws:
            # Protocol boilerplate
            await ws.receive_json() # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            await ws.receive_json() # auth_ok

            monitor = JeevesMonitor(ws)
            await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_tool_call"})
            asyncio.create_task(monitor.listen())

            print("--- Starting FORCE TOOL Suite ---")
            await session.post(f"{URL}/api/services/ha_doorbell_jeeves/start_session")
            await asyncio.sleep(10)
            
            # Send greeting to clear greeting phase
            await send_audio_stream(session, generate_pcm("Hi Jeeves."))
            await asyncio.sleep(8)

            print("\n>>> FORCING switch_camera...")
            cmd = "STOP. COMMAND: switch_camera to camera.carport_fluent. EXECUTE NOW."
            await send_audio_stream(session, generate_pcm(cmd))
            await asyncio.sleep(15)

            print("\n>>> FORCING read_entity_state...")
            cmd = "STOP. COMMAND: read_entity_state for camera.entree_fluent_lens_0. EXECUTE NOW."
            await send_audio_stream(session, generate_pcm(cmd))
            await asyncio.sleep(15)

            print("\n>>> FORCING save_event...")
            cmd = "STOP. COMMAND: save_event title 'Test' description 'Success'. EXECUTE NOW."
            await send_audio_stream(session, generate_pcm(cmd))
            await asyncio.sleep(15)

            await session.post(f"{URL}/api/services/ha_doorbell_jeeves/stop_session")
            
            # Results
            funcs = [t.get("function") for t in monitor.tool_calls]
            print(f"\nCaptured Tool Calls: {funcs}")
            
            success = all(f in funcs for f in ["switch_camera", "read_entity_state", "save_event"])
            if success:
                print("\n✅ ALL TOOLS VERIFIED!")
            else:
                print("\n❌ SOME TOOLS FAILED.")
                sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_suite())
