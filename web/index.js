let SAMPLERATE = 16000;

const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const host = window.location.host.split(":")[0];

const socket_url = `${protocol}//${host}/ws`;
let socket;

let tts_queue = [];
let is_tts_playing = false;
let tts_audio;
let tts_muted = false;
let user_muted = false;

const text_input = document.getElementById("input");

function check_speech(audioData) {
    const threshold = 0.1;
    return audioData.some(sample => Math.abs(sample) > threshold);
}

const messagebox = document.getElementById("messagebox");

function add_message(klass, message) {
    	console.log(`${klass}: ${message}`);
  
	if (klass == "user" && is_tts_playing) {
		tts_queue = []; // stop yapping when the user interrupts
	}

	if (klass == "assistant" && message == "<EOM>") {
		let li = document.createElement('li');
		li.classList.add(klass);
		li.innerHTML = "";
		li.dataset.rawContent = "";
		messagebox.appendChild(li);
		return;
	}

	let li;

	if (messagebox.lastElementChild && klass == "assistant" && messagebox.lastElementChild.classList.contains("assistant")) {
		li = messagebox.lastElementChild;
		message = li.dataset.rawContent + message;
	} else {
		li = document.createElement('li');
    		li.classList.add(klass);

    		messagebox.appendChild(li);
	}

	message = message.replace("\n", "  \n");
	li.dataset.rawContent = message;

    	let formattedMessage = marked.parse(message);

    	const latexPattern = /(\$\$[\s\S]+?\$\$|\$[\s\S]+?\$)/g;
	
	try {
		formattedMessage = formattedMessage.replace(latexPattern, (match) => {
			const isBlock = match.startsWith('$$');
			const latex = match.replace(/^\$+\s*|\s*\$+$/g, ''); // Remove the dollar signs

			if (isBlock) {
				return `<div class="katex">${katex.renderToString(latex, { displayMode: true })}</div>`;
			} else {
				return `<span class="katex">${katex.renderToString(latex)}</span>`;
			}
		});
	} catch (e) {
		console.error(e);
	}
    	
	li.innerHTML = formattedMessage;
	
    	setTimeout(() => {
        	messagebox.scrollTop = messagebox.scrollHeight;
    	}, 350);
}

function to_blob(audio, sr) {
	const buf = new ArrayBuffer(4 + audio.length * Float32Array.BYTES_PER_ELEMENT);
	const view = new DataView(buf);

	view.setInt32(0, sr, true);

	for (let i = 0; i < audio.length; i++) {
		view.setFloat32(4 + i * 4, audio[i], true);
	}

	return new Blob([view], { type: "application/octet-stream" });
}

async function send_to_server(audio, samplerate) {
	blob = to_blob(audio, samplerate);

	socket.send(blob);
}

const user_speech_indicator = document.getElementById("user-speech-indicator");
let text_message_queue = [];

async function begin_recording() {
	const stream = await navigator.mediaDevices.getUserMedia({audio: true});
	const audio_ctx = new (window.AudioContext || window.webkitAudioContext)();
	SAMPLERATE = audio_ctx.sampleRate;
	console.log("Using samplerate", SAMPLERATE);
	const media_stream_source = audio_ctx.createMediaStreamSource(stream);
	const script_processor = audio_ctx.createScriptProcessor(1024*16, 1, 1);

	media_stream_source.connect(script_processor);
	script_processor.connect(audio_ctx.destination);

	let last_was_speech = false;
	let text_message_queue_fence = false;
	script_processor.onaudioprocess = async (event) => {
		const audio_data = event.inputBuffer.getChannelData(0);

		const is_speech = check_speech(audio_data) && !user_muted;
		
		if (is_speech || last_was_speech) {
			text_message_queue_fence = true;
			await send_to_server(audio_data, SAMPLERATE);
			text_message_queue_fence = false;
		}

		if (is_speech) {
			user_speech_indicator.style.backgroundColor = '#44f';
		} else {
		
			if (user_muted) {
				user_speech_indicator.style.backgroundColor = "#400";
			} else {
				user_speech_indicator.style.backgroundColor = 'var(--primary-bg-color)';
			}
		}

		last_was_speech = is_speech;
	};

	setInterval(() => {
		if (!text_message_queue_fence && text_message_queue.length > 0) {
			socket.send(text_message_queue[0]);
			text_message_queue.splice(0, 1);
		}
	}, 50);

	console.log("Audio recording started!");
}

const ai_speech_indicator = document.getElementById("ai-speech-indicator");

async function run_tts() {
	if (is_tts_playing || tts_queue.length == 0) return;

	is_tts_playing = true;
	if (!tts_muted) ai_speech_indicator.style.backgroundColor = "#4f4";
	tts_queue[0].addEventListener('ended', () => {
		tts_queue.splice(0, 1);
		is_tts_playing = false;
		tts_audio = null;
		if (!tts_muted) ai_speech_indicator.style.backgroundColor = "var(--primary-bg-color)";
	});
	tts_audio = tts_queue[0];
	tts_audio.muted = tts_muted;
	await tts_audio.play();
}

function toggle_tts_mute() {
	tts_muted = !tts_muted;
	if (tts_audio) {
		tts_audio.muted = tts_muted;
	}
	if (tts_muted) {
		ai_speech_indicator.style.backgroundColor = "#400";
	} else {
		if (is_tts_playing) {
			ai_speech_indicator.style.backgroundColor = "#4f4";
		} else {
			ai_speech_indicator.style.backgroundColor = "var(--primary-bg-color)";
		}
	}
}

function toggle_user_mute() {
	user_muted = !user_muted;
	if (user_muted) {
		user_speech_indicator.style.backgroundColor = "#400";
	} else {
		user_speech_indicator.style.backgroundColor = "var(--primary-bg-color)";
	}
}

async function main() {
	console.log(`Connecting to ${socket_url}`);
	socket = new WebSocket(socket_url);

	socket.addEventListener("open", async () => {	
		console.log("Connected!");
		setInterval(run_tts, 100);
		await begin_recording();
	});

	socket.onmessage = function(event) {
		if (event.data instanceof Blob) {
			const audio = new Audio(URL.createObjectURL(event.data));
			tts_queue.push(audio);
		} else {
			role = event.data[0] == "A" ? "assistant" : "user";
			add_message(role, event.data.slice(1));
		}
	};

	text_input.addEventListener("keydown", function (event) {
		if (event.key == "Enter") {
			text_message_queue.push(text_input.value);
			text_input.value = "";
			event.preventDefault();
		}
	});
}

main();
