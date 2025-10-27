from flask import Flask, request, send_from_directory, jsonify, send_file
import os
import sounddevice as sd
import threading
import numpy as np
import whisper
from scipy.signal import resample
import logging
from queue import Queue
import time
import requests
import json
import os
import base64
from pyt2s.services import stream_elements
import io
import re

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)  # Only log errors, suppress everything else

audio = {}
input_queues = {}
message_lists = {}
intermediate_queues = {}
tts_queues = {}
output_queues = {}

used_uuids = set()

transcriber = whisper.load_model("base") 

def get_random_base64_string(length=16):
    random_bytes = os.urandom(length)  # Generate random bytes
    base64_string = base64.b64encode(random_bytes).decode('utf-8')  # Encode to Base64 and decode to string
    return base64_string

def process_audio(audio, input_rate):
    target_rate = 16000
    if input_rate != target_rate:
        audio = resample(audio, int(len(audio) * target_rate / input_rate))

    if len(audio.shape) > 1:
        audio = np.mean(audio, axis=1)

    return audio

def play(data, samplerate):
    sd.play(data, samplerate=samplerate)
    sd.wait()

def transcribe(audio, samplerate, uuid):
    print(f"Transcribing from {uuid}")

    processed_audio = process_audio(audio, samplerate)
    result = transcriber.transcribe(processed_audio)
    text = result["text"].strip()

    if not text:
        return None

    print(f"Heard \"{text}\" from {uuid}")
    if not uuid in input_queues:
        input_queues[uuid] = Queue()
    input_queues[uuid].put(text)

    return text

def llm_worker():
    ollama_server = "http://localhost:11434/api/chat"
    ollama_model = "llama3.2"

    while True:
        time.sleep(0.1)
        for uuid, queue in input_queues.items():
            if not uuid in intermediate_queues:
                intermediate_queues[uuid] = Queue()
                tts_queues[uuid] = Queue()
            if not uuid in message_lists:
                message_lists[uuid] = []
            run = not queue.empty()
            while not queue.empty():
                message_lists[uuid].append({"role": "user", "content": queue.get()})

            if not run:
                continue

            payload = {
                "model": ollama_model,
                "stream": True,
                "messages": message_lists[uuid]
            }
            headers = {"Content-Type": "application/json"}

            with requests.post(ollama_server, headers=headers, json=payload, stream=True) as response:
                message_part = []
                message_part_long = []
                for line in response.iter_lines():
                    line = line.strip()
                    if line:
                        line_data = json.loads(line.decode("utf-8"))
                        token = line_data.get("message").get("content")
                        message_part.append(token)
                        message_part_long.append(token)
                        if ("\n" in token) or token in [
                            ".",
                            ",",
                            "!",
                            "?",
                            "!?",
                            "?!",
                            ";"
                        ]:
                            message_part = "".join(message_part)
                            if "\n" in token:
                                message_part_long = "".join(message_part_long).strip()
                                message_lists[uuid].append({"role": "assistant", "content": message_part_long})
                                intermediate_queues[uuid].put(message_part_long)
                                message_part_long = []
                            tts_queues[uuid].put(message_part)
                            message_part = []
                message_part_long = "".join(message_part_long).strip()
                message_lists[uuid].append({"role": "assistant", "content": message_part_long})
                intermediate_queues[uuid].put(message_part_long)
                message_part = "".join(message_part).strip()
                tts_queues[uuid].put(message_part)
             
@app.route('/upload_audio', methods=['POST'])
def upload_audio():
    global audio
    if 'audio' not in request.files:
        return 'No file part', 400

    uuid = str(request.cookies.get("uuid"))

    audio_file = request.files['audio']

    is_end = request.form.get('is_end') == 'true'
    samplerate = int(request.form.get('samplerate'))

    if not uuid in audio:
        audio[uuid] = []
    audio[uuid].append(np.frombuffer(audio_file.read(), np.float32))

    if is_end:
        transcription = transcribe(np.concatenate(audio[uuid]), samplerate, uuid)
        audio[uuid] = []

        return jsonify({"transcription": transcription}), 200

    return jsonify({}), 200

@app.route('/get_message', methods=['GET'])
def get_message():
    uuid = str(request.cookies.get("uuid"))

    if (not uuid in intermediate_queues) or (intermediate_queues[uuid].empty()):
        return "No Content", 204

    return intermediate_queues[uuid].get(), 200

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

@app.route('/get_tts', methods=['GET'])
def get_tts():
    uuid = str(request.cookies.get("uuid"))

    if (not uuid in tts_queues) or (tts_queues[uuid].empty()):
        return "No Content", 204

    text = tts_queues[uuid].get()

    text = preprocess_latex_for_tts(text)
    text = text.replace("\n", " ")

    print(text)

    if not text:
        return "No Content", 204

    data = stream_elements.requestTTS(text)
    return send_file(io.BytesIO(data), mimetype="audio/mpeg")

@app.route('/new_uuid', methods=['GET'])
def get_uuid():
    uuid = get_random_base64_string()
    while uuid in used_uuids:
        uuid = get_random_base64_string()
    used_uuids.add(uuid)
    print(f"Created new UUID {uuid}!")
    return uuid, 200

@app.route('/')
def root():
    return send_file("web/index.html")

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    return send_from_directory('web', path)


if __name__ == '__main__':
    task = threading.Thread(target=llm_worker)
    task.daemon = True
    task.start()
    app.run(host='0.0.0.0', port=8050)

