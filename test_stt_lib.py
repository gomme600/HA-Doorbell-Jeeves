import speech_recognition as sr
import base64
import wave
import io

def test_stt():
    # Create a dummy silent wav
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b'\x00\x00' * 16000)
    
    buffer.seek(0)
    r = sr.Recognizer()
    with sr.AudioFile(buffer) as source:
        audio = r.record(source)
    
    print("STT library is ready.")

if __name__ == "__main__":
    test_stt()
