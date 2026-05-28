import pyttsx3
import wave
import audioop
import base64
import asyncio
import aiohttp
import sys
import os

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

async def send_audio_chunks(session, pcm_data):
    CHUNK_SIZE = 4096
    print(f"Total PCM bytes: {len(pcm_data)}")
    for i in range(0, len(pcm_data), CHUNK_SIZE):
        chunk = pcm_data[i:i+CHUNK_SIZE]
        chunk_b64 = base64.b64encode(chunk).decode('ascii')
        payload = {"audio_base64": chunk_b64}
        await session.post(f"{URL}/api/services/ha_doorbell_jeeves/send_audio", json=payload)
        await asyncio.sleep(0.128)

def generate_pcm(text):
    print(f"Generating speech for: '{text}'")
    engine = pyttsx3.init()
    engine.save_to_file(text, 'test_speech.wav')
    engine.runAndWait()
    
    with wave.open('test_speech.wav', 'rb') as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        data = wf.readframes(nframes)
        
    if nchannels == 2:
        data = audioop.tomono(data, sampwidth, 0.5, 0.5)
        
    if framerate != 16000:
        data, _ = audioop.ratecv(data, sampwidth, 1, framerate, 16000, None)
        
    return data

async def main():
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    print("Starting session...")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/start_session") as resp:
            print("Start session response:", await resp.text())
            
        print("Waiting 5 seconds for session to connect...")
        await asyncio.sleep(5)
        
        greeting = "Hi Jeeves!"
        pcm_data = generate_pcm(greeting)
        print("Sending initial greeting to bypass greeting phase...")
        await send_audio_chunks(session, pcm_data)
            
        print("Waiting 10 seconds for the agent's greeting response...")
        await asyncio.sleep(10)
        
        switch_cmd = "Please switch the camera to the carport."
        print(f"\nSending command: {switch_cmd}")
        pcm_data = generate_pcm(switch_cmd)
        await send_audio_chunks(session, pcm_data)
        
        print("Waiting 15 seconds for camera switch and vision processing...")
        await asyncio.sleep(15)
        
        count_cmd = "Please look carefully at the current camera feed. Count how many cars are in the carport. Then, use the save event tool to save an event with the title 'Car Count'. In the description, write exactly how many cars you see."
        print(f"\nSending command: {count_cmd}")
        pcm_data = generate_pcm(count_cmd)
        await send_audio_chunks(session, pcm_data)
            
        print("Waiting 30 seconds for the agent to process vision and execute the save_event tool...")
        for i in range(30):
            print(f"Waiting... {30-i}s", end="\r")
            await asyncio.sleep(1)
            
        print("\nChecking event timeline sensor for the 'Car Count' event...")
        success = False
        async with session.get(f"{URL}/api/states") as resp:
            states = await resp.json()
            for state in states:
                if 'jeeves' in state['entity_id'] and 'events_feed' in state['entity_id']:
                    events = state.get("attributes", {}).get("events", [])
                    print(f"Checking {state['entity_id']} - found {len(events)} events.")
                    for evt in events:
                        title = evt.get("title", "").lower()
                        desc = evt.get("description", "").lower()
                        if "car" in title or "count" in title:
                            print(f"\nFOUND EVENT: '{evt.get('title')}'")
                            print(f"DESCRIPTION: '{evt.get('description')}'")
                            if "2" in desc or "two" in desc:
                                print("SUCCESS: Agent correctly identified 2 cars!")
                                success = True
                            else:
                                print("FAILURE: Agent did not identify 2 cars. Hallucination or vision failed.")
                            break
                    if success:
                        break

        print("\nStopping session...")
        async with session.post(f"{URL}/api/services/ha_doorbell_jeeves/stop_session") as resp:
            print("Stop session response:", await resp.text())
            
        if not success:
            print("\nTest failed or event not found.")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())