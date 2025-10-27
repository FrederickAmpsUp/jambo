import asyncio
import websockets
import whisper
from scipy.signal import resample
import threading
from queue import Queue
from flask import Flask, request, send_from_directory, jsonify, send_file
import logging
import numpy as np
import time
import requests
import json
import sounddevice as sd
from pyt2s.services import stream_elements
import io
import re

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)  # Only log errors, suppress everything else

class BioutputQueue:
    def __init__(self):
        self.queue1 = Queue()
        self.queue2 = Queue()

    def put(self, val):
        self.queue1.put(val)
        self.queue2.put(val)

def process_audio(audio, input_sr):
    target_sr = 16000

    if input_sr != target_sr:
        audio = resample(audio, int(len(audio) * target_sr / input_sr))

    if len(audio.shape) > 1:
        audio = np.mean(audio, axis=1)

    return audio

def speech_transcription_worker(stop_event, interrupt_event, audio_queue, text_queue):
    transcriber = whisper.load_model("base")

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
            if not audio_queue.empty():
                audio, sr = audio_queue.get_nowait()
                audio = process_audio(audio, sr)
                print("Transcribing")
                result = transcriber.transcribe(audio, language="en")
                text = result["text"].strip()

                if text:
                    interrupt_event.set()
                    text_queue.put(text)
    except Exception as e:
        print(f"Error in speech transcription: {e}")
    stop_event.set()

def llm_worker(stop_event, interrupt_event, in_queue, out_queue_1, out_queue_2):
    ollama_server = "http://localhost:11434/api/chat"
    ollama_model = "llama3.2"

    messages = []

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
            interrupt_event.clear()
            if not in_queue.empty():
                message = in_queue.get_nowait()
                messages.append({"role": "user", "content": message})
                payload = {
                    "model": ollama_model,
                    "stream": True,
                    "messages": messages
                }
                headers = {"Content-Type": "application/json"}

                print("Generating")
                with requests.post(ollama_server, headers=headers, json=payload, stream=True) as response:
                    sentence = []

                    def finish_sentence():
                        nonlocal sentence
                        text = "".join(sentence).strip()
                        messages.append({"role": "assistant", "content": text})
                        out_queue_2.put(text)
                        sentence = []

                    for line in response.iter_lines():
                        if interrupt_event.is_set():
                            break

                        line = line.strip()
                        if not line:
                            continue
                        line_data = json.loads(line.decode("utf-8"))

                        token = line_data.get("message").get("content")
                        sentence.append(token)
                        out_queue_1.put(token)

                        if "\n" in token or token in [
                            ".",
                            ",",
                            "!",
                            "?",
                            "!?",
                            "?!",
                            ";" 
                        ]:
                            finish_sentence()
                    if not interrupt_event.is_set():
                        sentence.append("\n")
                        out_queue_1.put("\n")
                        finish_sentence()
                    out_queue_1.put("<EOM>")

                    interrupt_event.clear()
    except Exception as e:
        print(f"Error in LLM: {e}")
    stop_event.set()

def preprocess_latex_for_tts(latex):
    tts_string = latex
    
    tts_string = re.sub(r'\$(.*?)\$', lambda m: m.group(1), tts_string)
    
    tts_string = re.sub(r'\\times', ' times ', tts_string)
    tts_string = re.sub(r'\\div', ' divided by ', tts_string)
    tts_string = re.sub(r'\\leq', ' less than or equal to ', tts_string)
    tts_string = re.sub(r'\\geq', ' greater than or equal to ', tts_string)
    tts_string = re.sub(r'\\pm', ' plus or minus ', tts_string)
    tts_string = re.sub(r'\^2', ' squared ', tts_string)
    tts_string = re.sub(r'\^3', ' cubed ', tts_string)
    tts_string = re.sub(r'\^', ' to the power of ', tts_string)
    tts_string = re.sub(r'\\sqrt', ' square root of ', tts_string)
    tts_string = re.sub(r'\\sum', ' sum ', tts_string)
    tts_string = re.sub(r'\\int', ' integral of ', tts_string)

        # definitely wrote this one by hand
    tts_string = re.sub(r'\\frac\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', r'\1 over \2', tts_string)

    tts_string = re.sub(r'\\alpha', 'alpha', tts_string)
    tts_string = re.sub(r'\\beta', 'beta', tts_string)
    tts_string = re.sub(r'\\gamma', 'gamma', tts_string)
    tts_string = re.sub(r'\\delta', 'delta', tts_string)
    tts_string = re.sub(r'\\epsilon', 'epsilon', tts_string)

    # Handle negative numbers and variables with a minus in front of them
    tts_string = re.sub(r'(\s|^)-(\d+)', r'\1negative \2', tts_string)  # for negative numbers like "-5"
    tts_string = re.sub(r'(\s|^)-([a-zA-Z0-9]+)', r'\1negative \2', tts_string)  # for variables like "-a"
    
    # Handle subtraction between two numbers or variables, e.g., "5-3" should be pronounced as "5 minus 3"
    tts_string = re.sub(r'(\d+)\s*-\s*(\d+)', r'\1 minus \2', tts_string)  # numbers with subtraction
    tts_string = re.sub(r'([a-zA-Z0-9]+)\s*-\s*([a-zA-Z0-9]+)', r'\1 minus \2', tts_string)  # variables with subtraction


    tts_string = re.sub(r'[{}]', '', tts_string)
    return tts_string

def tts_worker(stop_event, in_queue, out_queue):
    try:
        while not stop_event.is_set():
            time.sleep(0.1)
            if not in_queue.empty():
                text = in_queue.get_nowait()

                text = preprocess_latex_for_tts(text)
                text = text.replace("\n", " ")

                if not text:
                    continue
                print("Speaking")
                data = stream_elements.requestTTS(text)
                out_queue.put(data)
    except Exception as e:
        print(f"Error in TTS: {e}")
    stop_event.set()

def transmission_worker(stop_event, socket, sr_out_queue, llm_out_queue, tts_out_queue):
    try:
        while not stop_event.is_set():
            wait = True
            if not llm_out_queue.empty():
                message = llm_out_queue.get_nowait()
                asyncio.run(socket.send("A"+message))
                wait = False
            if not sr_out_queue.empty():
                message = sr_out_queue.get_nowait()
                asyncio.run(socket.send("U"+message))
            if not tts_out_queue.empty():
                data = tts_out_queue.get_nowait()
                asyncio.run(socket.send(data))
            if wait:
                time.sleep(0.1)
    except Exception as e:
        print(f"Error in transmission: {e}")
    stop_event.set()

async def connection(socket):
    print("New connection")

    audio_chunks = []
    stop_event = threading.Event()
    interrupt_event = threading.Event()
    speech_queue = Queue()
    speech_text_queue = BioutputQueue()
    llm_out_queue = Queue()
    tts_in_queue = Queue()
    tts_out_queue = Queue()

    speech_task = threading.Thread(target=speech_transcription_worker, args=(stop_event, interrupt_event, speech_queue, speech_text_queue))
    speech_task.daemon = True
    speech_task.start()

    llm_task = threading.Thread(target=llm_worker, args=(stop_event, interrupt_event, speech_text_queue.queue1, llm_out_queue, tts_in_queue))
    llm_task.daemon = True
    llm_task.start()

    tts_task = threading.Thread(target=tts_worker, args=(stop_event, tts_in_queue, tts_out_queue))
    tts_task.daemon = True
    tts_task.start()

    transmission_task = threading.Thread(target=transmission_worker, args=(stop_event, socket, speech_text_queue.queue2, llm_out_queue, tts_out_queue))
    transmission_task.daemon = True
    transmission_task.start()

    try:
        async for message in socket:
            if isinstance(message, str):
                if message.strip():
                    speech_text_queue.put(message.strip())
                    interrupt_event.set()
            if isinstance(message, bytes):
                samplerate = int.from_bytes(message[:4], byteorder="little")
                audio = np.frombuffer(message[4:], np.float32)
               
                audio_chunks.append(audio)

                rms = np.sqrt(np.mean(audio ** 2))
                volume = rms / (rms + 0.5)
                
                if volume < 0.05:
                    speech_queue.put((np.concatenate(audio_chunks), samplerate))
                    audio_chunks = []
                    print(f"transcribing (samplerate {samplerate})")

            if stop_event.is_set():
                break
    except websockets.exceptions.ConnectionClosed as e:
        print(f"Connection closed: {e}")

    stop_event.set()
    print("Done!")

@app.route('/')
def root():
    return send_file("web/index.html")

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    return send_from_directory('web', path)

async def main():
    _ = stream_elements.requestTTS("load model")
    server = await websockets.serve(connection, "0.0.0.0", 8051)
    print("Websocket server started on ws://0.0.0.0:8051")
    server_task = threading.Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": 8050})
    server_task.daemon = True
    server_task.start()

    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
