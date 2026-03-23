/**
 * audio_search.js — Audio recording, upload, and voice-powered product search.
 *
 * Depends on: app.js, renderers.js, chat.js
 *
 * Flow:
 *   1. User clicks microphone button → starts recording via MediaRecorder API
 *   2. Recording indicator shown with waveform and timer
 *   3. User clicks again to stop → audio sent to /audio-search endpoint
 *   4. Backend transcribes with Whisper → searches products
 *   5. Results rendered with transcription context
 */

// ── State ───────────────────────────────────────────────────────────────────

let _mediaRecorder = null;
let _audioChunks = [];
let _recordingStartTime = null;
let _recordingTimerInterval = null;
let _isRecording = false;

// ── Toggle recording ────────────────────────────────────────────────────────

async function toggleAudioRecording() {
  if (_isRecording) {
    stopAudioRecording();
  } else {
    await startAudioRecording();
  }
}

// ── Start recording ─────────────────────────────────────────────────────────

async function startAudioRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: 16000,
      },
    });

    // Prefer webm/opus, fallback to wav
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/wav';

    _mediaRecorder = new MediaRecorder(stream, { mimeType });
    _audioChunks = [];

    _mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        _audioChunks.push(event.data);
      }
    };

    _mediaRecorder.onstop = () => {
      // Stop all tracks
      stream.getTracks().forEach(track => track.stop());
      handleRecordingComplete();
    };

    _mediaRecorder.start(100); // collect data every 100ms
    _isRecording = true;
    _recordingStartTime = Date.now();

    // Update UI
    document.getElementById('audioSearchBtn').classList.add('recording');
    document.getElementById('audioRecordingBar').classList.add('active');
    startRecordingTimer();

    // Auto-stop after 60 seconds
    setTimeout(() => {
      if (_isRecording) stopAudioRecording();
    }, 60000);

  } catch (err) {
    if (err.name === 'NotAllowedError') {
      appendError('Microphone access denied. Please allow microphone access in your browser settings.');
    } else {
      appendError('Could not access microphone. Please check your device settings.');
    }
    console.error('Microphone error:', err);
  }
}

// ── Stop recording ──────────────────────────────────────────────────────────

function stopAudioRecording() {
  if (_mediaRecorder && _mediaRecorder.state !== 'inactive') {
    _mediaRecorder.stop();
  }
  _isRecording = false;
  clearInterval(_recordingTimerInterval);
  document.getElementById('audioSearchBtn').classList.remove('recording');
  document.getElementById('audioRecordingBar').classList.remove('active');
}

function cancelAudioRecording() {
  if (_mediaRecorder && _mediaRecorder.state !== 'inactive') {
    _mediaRecorder.ondataavailable = null;
    _mediaRecorder.onstop = null;
    _mediaRecorder.stop();
    _mediaRecorder.stream.getTracks().forEach(track => track.stop());
  }
  _isRecording = false;
  _audioChunks = [];
  clearInterval(_recordingTimerInterval);
  document.getElementById('audioSearchBtn').classList.remove('recording');
  document.getElementById('audioRecordingBar').classList.remove('active');
}

// ── Timer ───────────────────────────────────────────────────────────────────

function startRecordingTimer() {
  const timerEl = document.getElementById('audioTimer');
  _recordingTimerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - _recordingStartTime) / 1000);
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    timerEl.textContent = `${mins}:${secs.toString().padStart(2, '0')}`;
  }, 200);
}

// ── Handle completed recording ──────────────────────────────────────────────

async function handleRecordingComplete() {
  if (_audioChunks.length === 0) return;

  const blob = new Blob(_audioChunks, { type: _mediaRecorder.mimeType || 'audio/webm' });
  _audioChunks = [];

  // Minimum duration check (avoid accidental taps)
  const duration = (Date.now() - _recordingStartTime) / 1000;
  if (duration < 0.5) {
    return; // Too short, ignore
  }

  await submitAudioSearch(blob);
}

// ── Handle audio file upload ────────────────────────────────────────────────

function handleAudioFileSelect(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  event.target.value = '';
  submitAudioSearch(file);
}

// ── Submit audio search ─────────────────────────────────────────────────────

async function submitAudioSearch(audioBlob) {
  hideWelcome();

  // Show user message
  const msgs = document.getElementById('messages');
  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.innerHTML = `<div class="avatar">U</div><div class="bubble">
    <div class="audio-search-query">
      <span>&#127908;</span>
      <span>Voice search (${(audioBlob.size / 1024).toFixed(0)} KB audio)</span>
    </div>
  </div>`;
  msgs.appendChild(userDiv);
  msgs.scrollTop = msgs.scrollHeight;

  // Show processing
  const typing = appendTyping();

  try {
    const formData = new FormData();
    const ext = (audioBlob.type || '').includes('webm') ? 'webm' : 'wav';
    formData.append('file', audioBlob, `recording.${ext}`);

    const r = await fetch(`${API}/audio-search`, {
      method: 'POST',
      body: formData,
    });

    typing.remove();

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: 'Audio search failed' }));
      appendError(err.detail || `Audio search error: HTTP ${r.status}`);
      return;
    }

    const data = await r.json();

    if (data.success && data.products && data.products.length > 0) {
      const transcriptMsg = data.transcription
        ? `I heard: "${data.transcription}". Here are the matching products:`
        : 'Here are the matching products:';
      appendMsg('agent', transcriptMsg, { products: data.products });
    } else if (data.transcription) {
      // Got transcription but no products — send as text chat
      appendMsg('agent',
        `I heard: "${data.transcription}". ${data.message || 'No products found for that query.'}` +
        '\nLet me try searching for that in the catalog...');
      // Auto-search via text
      doSend(data.transcription);
    } else {
      appendMsg('agent', data.message || data.error || 'Could not understand the audio. Please try again.');
    }
  } catch (e) {
    typing.remove();
    appendError('Audio search failed. Please try again.');
    console.error('Audio search error:', e);
  }
}
