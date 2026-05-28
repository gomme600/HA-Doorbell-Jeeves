"""
Comprehensive E2E AI Test Suite for Doorbell Jeeves.
Verifies all tool calls, vision, and session stability.
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
import speech_recognition as sr
import io

# --- Configuration ---
URL = "https://homeassistant.gomme600.ovh"
WS_URL = "wss://homeassistant.gomme600.ovh/api/websocket"
CLIENT_ID = f"{URL}/"
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
    CHUNK_SIZE = 4096 # ~128ms at 16kHz
    INTERVAL = 0.128
    
    chunks = [pcm_data[i:i+CHUNK_SIZE] for i in range(0, len(pcm_data), CHUNK_SIZE)]
    print(f"  [Audio] Streaming {len(chunks)} chunks to AI...")
    
    for i, chunk in enumerate(chunks):
        chunk_b64 = base64.b64encode(chunk).decode('ascii')
        payload = {"audio_base64": chunk_b64}
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/send_audio", json=payload) as resp:
            if resp.status != 200:
                print(f"  [Audio] Error sending chunk {i}: {await resp.text()}")
        
        if i < len(chunks) - 1:
            await asyncio.sleep(INTERVAL)

class JeevesMonitor:
    def __init__(self, ws):
        self.ws = ws
        self.transcripts = []
        self.tool_calls = []
        self.audio_outputs = []
        self.recognizer = sr.Recognizer()
        self.msg_id = 1

    async def listen(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "event":
                    event = data.get("event", {})
                    event_type = event.get("event_type")
                    event_data = event.get("data", {})
                    
                    if event_type == "ha_doorbell_jeeves_transcript":
                        role = event_data.get("role")
                        text = event_data.get("text")
                        print(f"  [Transcript] {role}: {text}")
                        self.transcripts.append(event_data)
                    
                    elif event_type == "ha_doorbell_jeeves_tool_call":
                        func = event_data.get("function")
                        args = event_data.get("arguments")
                        print(f"  [Tool Call] {func}({args})")
                        self.tool_calls.append(event_data)
                    
                    elif event_type == "ha_doorbell_jeeves_audio_output":
                        audio_b64 = event_data.get("audio_base64")
                        if audio_b64:
                            self.audio_outputs.append(audio_b64)
                            # Optional: Run STT in background
                            # asyncio.create_task(self.do_stt(audio_b64))

    async def do_stt(self, audio_b64):
        try:
            pcm_data = base64.b64decode(audio_b64)
            # Create wav in memory
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000) # Integration outputs 24kHz for Gemini usually
                wf.writeframes(pcm_data)
            buffer.seek(0)
            with sr.AudioFile(buffer) as source:
                audio = self.recognizer.record(source)
                text = self.recognizer.recognize_google(audio)
                print(f"  [STT IN] AI said: {text}")
        except Exception:
            pass

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

# --- Test Cases ---

async def test_case_carport(session, monitor):
    print("\n>>> TEST: Carport Vision & Tool Switch")
    cmd = "Jeeves, switch to the carport camera and tell me how many cars you see there. Then save an event 'Carport Check' with the count."
    await send_audio_stream(session, generate_pcm(cmd))
    
    # Wait for execution (switch + vision + save event)
    start_time = time.time()
    found_switch = False
    found_save = False
    while time.time() - start_time < 45:
        await asyncio.sleep(2)
        if any(t.get("function") == "switch_camera" for t in monitor.tool_calls):
            found_switch = True
        if any(t.get("function") == "save_event" for t in monitor.tool_calls):
            found_save = True
        if found_switch and found_save:
            break
    
    if found_switch and found_save:
        print("✅ PASSED: Carport switch and event save detected.")
        return True
    else:
        print(f"❌ FAILED: Switch={found_switch}, Save={found_save}")
        return False

async def test_case_read_state(session, monitor):
    print("\n>>> TEST: Read Entity State")
    # We'll ask for a specific entity like a light or sensor
    cmd = "Jeeves, what is the current state of the front door camera?"
    await send_audio_stream(session, generate_pcm(cmd))
    
    start_time = time.time()
    found = False
    while time.time() - start_time < 20:
        await asyncio.sleep(2)
        if any(t.get("function") == "read_entity_state" for t in monitor.tool_calls):
            found = True
            break
    
    if found:
        print("✅ PASSED: read_entity_state called.")
        return True
    else:
        print("❌ FAILED: read_entity_state not called.")
        return False

async def test_case_complex_tools(session, monitor):
    print("\n>>> TEST: Calendar & History")
    cmd = "Check my calendar for tomorrow and tell me if there's anything important. Also check the history of the front door for the last 2 hours."
    await send_audio_stream(session, generate_pcm(cmd))
    
    start_time = time.time()
    found_cal = False
    found_hist = False
    while time.time() - start_time < 30:
        await asyncio.sleep(2)
        if any(t.get("function") == "get_calendar_events" for t in monitor.tool_calls):
            found_cal = True
        if any(t.get("function") == "get_entity_history" for t in monitor.tool_calls):
            found_hist = True
        if found_cal and found_hist:
            break
    
    if found_cal and found_hist:
        print("✅ PASSED: Calendar and History tools called.")
        return True
    else:
        print(f"❌ FAILED: Cal={found_cal}, Hist={found_hist}")
        return False

async def run_suite():
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(WS_URL) as ws:
            # Auth WS
            await ws.send_json({"type": "auth", "access_token": token})
            auth_resp = await ws.receive_json()
            if auth_resp.get("type") != "auth_ok":
                print("WS Auth failed")
                return

            # Subscribe to events
            monitor = JeevesMonitor(ws)
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_transcript"})
            monitor.msg_id += 1
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_tool_call"})
            monitor.msg_id += 1
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_audio_output"})
            monitor.msg_id += 1

            # Start background listener
            listener_task = asyncio.create_task(monitor.listen())

            print("--- Starting AI E2E Test Suite ---")
            await session.post(f"{URL}/api/services/ha_doorbell_jeeves/start_session")
            await asyncio.sleep(8)
            
            # Greeting bypass
            await send_audio_stream(session, generate_pcm("Hi Jeeves, I'm here."))
            await asyncio.sleep(8)

            results = []
            results.append(await test_case_carport(session, monitor))
            results.append(await test_case_read_state(session, monitor))
            results.append(await test_case_complex_tools(session, monitor))

            await session.post(f"{URL}/api/services/ha_doorbell_jeeves/stop_session")
            await asyncio.sleep(5)
            
            listener_task.cancel()
            
            if all(results):
                print("\n✅ ALL TESTS PASSED!")
            else:
                print(f"\n❌ SUITE FAILED. Results: {results}")
                sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_suite())
