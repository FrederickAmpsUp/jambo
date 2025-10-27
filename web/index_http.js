let recorded_chunks = [];
let empty_chunks = -1;

let SAMPLERATE = 16000;
const DEAD_AIR_THRESH = 0.5; // seconds to wait while no speech is detected before we stop recording

function setCookie(name, value, days) {
    let expires = "";
    if (days) {
        const date = new Date();
        date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
        expires = `; expires=${date.toUTCString()}`;
    }
    document.cookie = `${name}=${value || ""}${expires}; path=/`;
}

function getCookie(name) {
    const cookies = document.cookie.split("; ");
    for (const cookie of cookies) {
        const [key, value] = cookie.split("=");
        if (key === name) {
            return value;
        }
    }
    return null;
}

function deleteCookie(name) {
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/`;
}

let tts_queue = [];
let is_tts_playing = false;

function check_speech(audioData) {
    const threshold = 0.1;
    return !is_tts_playing && audioData.some(sample => Math.abs(sample) > threshold);
}

function to_blob(audio, is_end) {
	const buf = new ArrayBuffer(audio.length * Float32Array.BYTES_PER_ELEMENT);
	const view = new DataView(buf);

	for (let i = 0; i < audio.length; i++) {
		view.setFloat32(i * 4, audio[i], true);
	}

	return new Blob([view], { type: "application/octet-stream" });
}

const messagebox = document.getElementById("messagebox");

function add_message(klass, message) {
    	console.log(`${klass}: ${message}`);
   
	let li;

	if (messagebox.lastElementChild && klass == "assistant" && messagebox.lastElementChild.classList.contains("assistant")) {
		li = messagebox.lastElementChild;
		message = li.dataset.rawContent + message;
	} else {
		li = document.createElement('li');
    		li.classList.add(klass);

    		messagebox.appendChild(li);
	}	

	li.dataset.rawContent = message + "  \n";

    	let formattedMessage = marked.parse(message);

    	const latexPattern = /(\$\$[\s\S]+?\$\$|\$[\s\S]+?\$)/g;

    	formattedMessage = formattedMessage.replace(latexPattern, (match) => {
        	const isBlock = match.startsWith('$$');
        	const latex = match.replace(/^\$+\s*|\s*\$+$/g, ''); // Remove the dollar signs

        	if (isBlock) {
			return `<div class="katex">${katex.renderToString(latex, { displayMode: true })}</div>`;
        	} else {
			return `<span class="katex">${katex.renderToString(latex)}</span>`;
        	}
    	});
    	
	li.innerHTML = formattedMessage;
	
    	setTimeout(() => {
        	messagebox.scrollTop = messagebox.scrollHeight;
    	}, 350);
}


async function send_to_server(blob, is_end) {
	const form_data = new FormData();
	form_data.append('audio', blob);
	form_data.append('is_end', is_end.toString());
	form_data.append('samplerate', SAMPLERATE.toString());

	const res = await fetch("/upload_audio", {
		method: 'POST',
		body: form_data
	});

	if (res.ok) {
		console.log("Sent audio to server!");

		let json = await res.json();
		if (json.transcription) {
			add_message("user", json.transcription);
		}
	} else {
		console.error("Error sending audio:", res.statusText);
	}
}

const user_speech_indicator = document.getElementById("user-speech-indicator");

async function begin_recording() {
	const stream = await navigator.mediaDevices.getUserMedia({audio: true});
	const audio_ctx = new (window.AudioContext || window.webkitAudioContext)();
	SAMPLERATE = audio_ctx.sampleRate;
	console.log("Using samplerate", SAMPLERATE);
	const media_stream_source = audio_ctx.createMediaStreamSource(stream);
	const script_processor = audio_ctx.createScriptProcessor(1024, 1, 1);

	media_stream_source.connect(script_processor);
	script_processor.connect(audio_ctx.destination);

	script_processor.onaudioprocess = (event) => {
		const audio_data = event.inputBuffer.getChannelData(0);

		const is_speech = check_speech(audio_data);

		if (is_speech && empty_chunks == -1) {
			empty_chunks = 0;
			recorded_chunks = [new Float32Array(audio_data)];
			console.log("Started recording!");
		} else if (is_speech && empty_chunks >= 0) {
			empty_chunks = 0; // reset if speech is happening
			recorded_chunks.push(new Float32Array(audio_data));
			user_speech_indicator.style.backgroundColor = '#44f';
		} else if (!is_speech && empty_chunks >= 0) {
			if (empty_chunks <= 2) {
				recorded_chunks.push(new Float32Array(audio_data)); // little fix to get around audio suddenly cutting off
			}
			empty_chunks++;
			if (empty_chunks * 1024 / SAMPLERATE > DEAD_AIR_THRESH) {
				empty_chunks = -1;
				console.log("Stopped recording!");
			}
			user_speech_indicator.style.backgroundColor = '#44f';
		} else {
			user_speech_indicator.style.backgroundColor = 'var(--primary-bg-color)';
		}
		
		// send data to server
		if (recorded_chunks.length > 16 || (recorded_chunks.length > 0 && empty_chunks == -1)) {
			let N = Math.min(recorded_chunks.length, 16);
			let total_length = N * 1024;
			let combined_array = new Float32Array(total_length);
			let offset = 0;
			for (let i = 0; i < N; i++) {
				combined_array.set(recorded_chunks[i], offset);
				offset += recorded_chunks[i].length;
			}
			recorded_chunks.splice(0, N);

			let blob = to_blob(combined_array);

			console.log("Sending audio to server!");
			send_to_server(blob, empty_chunks == -1);
		}
	};

	console.log("Audio recording started!");
}

async function check_messages() {
	const res = await fetch("/get_message", { method: "GET" });

	if (res.status == 204) return;
	if (res.status == 200) {
		add_message("assistant", await res.text());
		setTimeout(check_messages, 50);
		setTimeout(check_messages, 100);
		setTimeout(check_messages, 150);
		setTimeout(check_messages, 200);
	} else {
		console.error("Failed to fetch messages!");
	}
}

async function check_tts() {
	const res = await fetch("/get_tts", { method: "GET" });

	if (res.status == 200) {
		let blob = await res.blob();
		if (blob) {
			const audio = new Audio(URL.createObjectURL(blob));
			tts_queue.push(audio);
		}
	}
}

const ai_speech_indicator = document.getElementById("ai-speech-indicator");

async function run_tts() {
	if (is_tts_playing || tts_queue.length == 0) return;

	is_tts_playing = true;
	ai_speech_indicator.style.backgroundColor = "#4f4";
	tts_queue[0].addEventListener('ended', () => {
		tts_queue.splice(0, 1);
		is_tts_playing = false;
		ai_speech_indicator.style.backgroundColor = "var(--primary-bg-color)";
	});
	await tts_queue[0].play();
}

async function main() {
	if (!getCookie("uuid")) {
		const res = await fetch("/new_uuid", { method: "GET" });

		if (!res.ok) {
			window.alert("Failed to retrieve UUID, press enter to retry...");
			location.reload();
		}

		setCookie("uuid", await res.text(), 99999);
	}
	setInterval(check_messages, 250);
	setInterval(check_tts, 1000);
	setInterval(run_tts, 250);
	await begin_recording();
}

main();
