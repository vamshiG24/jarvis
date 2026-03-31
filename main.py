import os
import ctypes
import warnings
warnings.filterwarnings('ignore')

# Suppress ALSA errors
ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
def py_error_handler(filename, line, function, err, fmt):
    pass
c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
asound = ctypes.cdll.LoadLibrary('libasound.so.2')
asound.snd_lib_error_set_handler(c_error_handler)

import ollama
import pyttsx3
import speech_recognition as sr

# ─────────────────────────────────────
#  JARVIS - Level 1
#  Personal AI for Vamshi 🔥
# ─────────────────────────────────────

SYSTEM_PROMPT = """
You are JARVIS, a personal AI assistant built 
exclusively for Vamshi. He is a 3rd year B.Tech 
AI & Data Science student at IIIT Senapati, Manipur.
Always call him Boss. Be smart, confident and concise.
Never break character. You are running locally on 
his ROG laptop — completely private.
"""

# ── TTS (Mouth) ──
engine = pyttsx3.init()
engine.setProperty('rate', 170)
engine.setProperty('volume', 1.0)

def speak(text):
    print(f"\n🤖 Jarvis: {text}")
    print("─" * 50)
    engine.say(text)
    engine.runAndWait()

# ── STT (Ears) ──
recognizer = sr.Recognizer()

def listen():
    with sr.Microphone(device_index=0) as source:
        print("\n🎤 Listening Boss...")
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        try:
            # Automatically detects when you start and stop speaking
            audio = recognizer.listen(source)
            text = recognizer.recognize_google(audio)
            print(f"\n👤 Vamshi: {text}")
            print("─" * 50)
            return text.lower()
        except sr.UnknownValueError:
            print("❓ Couldn't understand, try again Boss!")
            return ""
        except sr.RequestError:
            speak("Internet needed Boss.")
            return ""

# ── Brain (Ollama) ──
def think(user_input):
    response = ollama.chat(
        model="llama3",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_input}
        ]
    )
    return response['message']['content']

# ── Main Loop ──
def main():
    print("\n" + "═" * 50)
    print("        ⚡ JARVIS - Personal AI ⚡")
    print("           Built for Vamshi 🔥")
    print("═" * 50)
    speak("Jarvis online. Ready Boss!")

    while True:
        command = listen()

        if not command:
            continue

        if any(word in command for word in ["exit", "quit", "shutdown", "bye"]):
            speak("Shutting down. Goodbye Boss!")
            break

        print("\n🧠 Thinking...")
        response = think(command)
        speak(response)

if __name__ == "__main__":
    main()
