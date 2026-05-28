"""
E2E AI Test Suite for Doorbell Jeeves.
This script performs real-time audio interaction with the AI agent via the HA API
to verify vision, tool calling, and session stability.
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

# --- Configuration ---
URL = "https://homeassistant.gomme600.ovh"
CLIENT_ID = f"{URL}/"
USERNAME = "gomme600"
PASSWORD = "claudetest"

# --- Audio Utilities ---

def generate_pcm(text):
    """Generate 16kHz mono 16-bit PCM from text using Windows TTS."""
    print(f"  [TTS] Generating: '{text}'")
    engine = pyttsx3.init()
    temp_wav = f"temp_{int(time.time())}.wav"
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
    
    os.remove(temp_wav)
    return data

async def send_audio_stream(session, pcm_data):
    """Send PCM data to HA send_audio service at a real-time rate."""
    CHUNK_SIZE = 4096 # ~128ms at 16kHz
    INTERVAL = 0.128
    
    chunks = [pcm_data[i:i+CHUNK_SIZE] for i in range(0, len(pcm_data), CHUNK_SIZE)]
    print(f"  [Audio] Streaming {len(chunks)} chunks...")
    
    for i, chunk in enumerate(chunks):
        chunk_b64 = base64.b64encode(chunk).decode('ascii')
        payload = {"audio_base64": chunk_b64}
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/send_audio", json=payload) as resp:
            if resp.status != 200:
                print(f"  [Audio] Error sending chunk {i}: {await resp.text()}")
        
        if i < len(chunks) - 1:
            await asyncio.sleep(INTERVAL)
    print("  [Audio] Stream complete.")

# --- HA API Utilities ---

async def get_token():
    """Authenticate and return a long-lived access token."""
    async with aiohttp.ClientSession() as session:
        # Step 1: Start login flow
        async with session.post(f"{URL}/auth/login_flow", json={
            "client_id": CLIENT_ID, 
            "handler": ["homeassistant", None], 
            "redirect_uri": CLIENT_ID
        }) as resp:
            data = await resp.json()
            flow_id = data.get("flow_id")
        
        # Step 2: Authenticate
        async with session.post(f"{URL}/auth/login_flow/{flow_id}", json={
            "client_id": CLIENT_ID, 
            "username": USERNAME, 
            "password": PASSWORD
        }) as resp:
            data = await resp.json()
            code = data.get("result")
        
        # Step 3: Exchange code for token
        async with session.post(f"{URL}/auth/token", data={
            "grant_type": "authorization_code", 
            "code": code, 
            "client_id": CLIENT_ID
        }) as resp:
            data = await resp.json()
            return data.get("access_token")

async def get_sensor_events(session):
    """Fetch recent events from the Jeeves events feed sensor."""
    async with session.get(f"{URL}/api/states") as resp:
        states = await resp.json()
        for state in states:
            if 'jeeves' in state['entity_id'] and 'events_feed' in state['entity_id']:
                return state.get("attributes", {}).get("events", [])
    return []

# --- Test Cases ---

async def test_case_carport_vision(session):
    print("\n>>> TEST CASE: Carport Car Counting (Vision + Tool Calling)")
    
    # 1. Switch to carport
    cmd1 = "Jeeves, please switch the camera to the carport."
    print(f"Sending: {cmd1}")
    await send_audio_stream(session, generate_pcm(cmd1))
    await asyncio.sleep(15) # Wait for switch and frame arrival
    
    # 2. Count cars and save event
    cmd2 = "Look at the carport camera right now. Count exactly how many cars you see. Then, use the save event tool to save an event titled 'Carport Check'. In the description, write 'I see X cars', replacing X with the number."
    print(f"Sending: {cmd2}")
    await send_audio_stream(session, generate_pcm(cmd2))
    
    print("Waiting 30 seconds for processing...")
    await asyncio.sleep(30)
    
    # 3. Verify
    events = await get_sensor_events(session)
    for evt in events:
        if evt.get("title") == "Carport Check":
            desc = evt.get("description", "").lower()
            print(f"Result: {evt.get('description')}")
            if "2" in desc or "two" in desc:
                print("✅ PASSED: Agent saw 2 cars!")
                return True
            else:
                print("❌ FAILED: Agent hallucinated or missed the cars.")
                return False
    
    print("❌ FAILED: Event 'Carport Check' was never saved.")
    return False

async def run_suite():
    token = await get_token()
    if not token:
        print("Failed to authenticate with HA.")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        print("--- Starting AI E2E Test Suite ---")
        
        # Start Session
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/start_session") as resp:
            print(f"Session Start: {resp.status}")
        
        await asyncio.sleep(10) # Connect time
        
        # Greeting Bypass
        await send_audio_stream(session, generate_pcm("Hello Jeeves, are you there?"))
        await asyncio.sleep(10)
        
        # Run tests
        success = await test_case_carport_vision(session)
        
        # Stop Session
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/stop_session") as resp:
            print(f"Session Stop: {resp.status}")
            
        if success:
            print("\n✅ ALL TESTS PASSED!")
        else:
            print("\n❌ SUITE FAILED.")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_suite())
