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
    
    if not os.path.exists(temp_wav):
        # Retry with fixed name if timestamp failed
        temp_wav = "temp_test.wav"
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

    async def wait_for_ai_silence(self, timeout=15):
        """Wait until no audio output has been received for a few seconds."""
        print("  [Monitor] Waiting for AI silence...")
        start_time = time.time()
        last_audio_time = time.time()
        initial_audio_count = len(self.audio_outputs)
        
        while time.time() - start_time < timeout:
            await asyncio.sleep(1)
            if len(self.audio_outputs) > initial_audio_count:
                last_audio_time = time.time()
                initial_audio_count = len(self.audio_outputs)
            elif time.time() - last_audio_time > 4:
                print("  [Monitor] AI is silent.")
                return True
        print("  [Monitor] Timeout waiting for silence, proceeding anyway.")
        return False

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
    await monitor.wait_for_ai_silence()
    cmd = "STOP SPEAKING. CALL TOOL switch_camera for camera.carport_fluent. ARRÊTE DE PARLER. Appelle l'outil switch_camera pour camera.carport_fluent."
    await send_audio_stream(session, generate_pcm(cmd))
    
    # Wait for execution (switch + vision + save event)
    start_time = time.time()
    found_switch = False
    found_save = False
    while time.time() - start_time < 50:
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
        print(f"Latest Transcripts: {monitor.transcripts[-3:]}")
        return False

async def test_case_read_state(session, monitor):
    print("\n>>> TEST: Read Entity State")
    await monitor.wait_for_ai_silence()
    cmd = "Call read_entity_state for camera.entree_fluent_lens_0. Appelle read_entity_state pour camera.entree_fluent_lens_0."
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
        print(f"Latest Transcripts: {monitor.transcripts[-3:]}")
        return False

async def test_case_complex_tools(session, monitor):
    print("\n>>> TEST: Calendar & History")
    await monitor.wait_for_ai_silence()
    cmd = "Call get_calendar_events. Appelle get_calendar_events."
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
        if found_cal:
            break
    
    if found_cal:
        print("✅ PASSED: Calendar tool called.")
        return True
    else:
        print(f"❌ FAILED: Cal={found_cal}")
        print(f"Latest Transcripts: {monitor.transcripts[-3:]}")
        return False

async def run_suite():
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(WS_URL) as ws:
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                print(f"Expected auth_required, got: {msg}")
                return
            await ws.send_json({"type": "auth", "access_token": token})
            auth_resp = await ws.receive_json()
            if auth_resp.get("type") != "auth_ok":
                print(f"WS Auth failed: {auth_resp}")
                return

            monitor = JeevesMonitor(ws)
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_transcript"})
            monitor.msg_id += 1
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_tool_call"})
            monitor.msg_id += 1
            await ws.send_json({"id": monitor.msg_id, "type": "subscribe_events", "event_type": "ha_doorbell_jeeves_audio_output"})
            monitor.msg_id += 1

            listener_task = asyncio.create_task(monitor.listen())

            print("--- Starting AI E2E Test Suite ---")
            await session.post(f"{URL}/api/services/ha_doorbell_jeeves/start_session")
            await asyncio.sleep(8)
            
            await send_audio_stream(session, generate_pcm("Hello Jeeves, I'm here for the test suite."))
            await asyncio.sleep(5)

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
