import { IngestClient } from '/lib/uploader.js';
import { api as defaultApi } from './api.js';

const ACTIVE = new Set(['requesting', 'recording', 'paused', 'saving', 'recoverable']);

export function recordingError(error, source = 'microphone') {
  if (error?.name === 'NotAllowedError' || error?.name === 'PermissionDeniedError') {
    return source === 'tab'
      ? 'Доступ к вкладке отменён. Нажмите «Начать запись», выберите вкладку встречи и включите передачу звука.'
      : 'Нет доступа к микрофону. Разрешите его в настройках сайта в адресной строке и повторите запись.';
  }
  if (error?.name === 'NotFoundError') return 'Микрофон не найден. Подключите его и повторите запись.';
  if (error?.name === 'NotReadableError') return 'Браузер не может получить звук. Проверьте системные разрешения и доступность микрофона/записи экрана.';
  if (error?.name === 'InvalidStateError') return 'Начните запись кнопкой на открытой странице встречи.';
  return error?.message || String(error || 'Не удалось начать запись.');
}

/** Owns actual browser audio capture. The host UI owns consent and meeting editing. */
export class BrowserRecorder {
  constructor({ meetingId, api = defaultApi, onState = () => {} }) {
    this.meetingId = meetingId;
    this.api = api;
    this.onState = onState;
    this.status = 'idle';
    this.error = null;
    this.connected = false;
    this.levels = {};
    this.captureSessionId = null;
    this.client = null;
    this.ctx = null;
    this.streams = [];
    this.nodes = [];
    this.elapsed = 0;
    this.runningSince = null;
    this.pendingControls = new Map();
    this.controlSeq = 0;
    this._stopPromise = null;
    this._pausePromise = null;
    this._resumePromise = null;
    this._releasing = false;
    this._unload = (event) => { if (this.active) { event.preventDefault(); event.returnValue = ''; } };
  }

  get active() { return ACTIVE.has(this.status); }

  snapshot() {
    return {
      meetingId: this.meetingId, captureSessionId: this.captureSessionId, status: this.status,
      elapsedSeconds: this.elapsed + (this.runningSince === null ? 0 : (performance.now() - this.runningSince) / 1000),
      connected: this.connected, bufferedSeconds: this.client?.unackedSeconds() || 0,
      levels: { ...this.levels }, error: this.error,
    };
  }

  _emit() { this.onState(this.snapshot()); }
  _set(status, error = null) { this.status = status; this.error = error; this._emit(); }
  _freezeClock() {
    if (this.runningSince !== null) this.elapsed += (performance.now() - this.runningSince) / 1000;
    this.runningSince = null;
  }

  async start({ source = 'microphone', includeMicrophone = true } = {}) {
    if (this.active) throw new Error('Запись уже запущена. Сначала завершите текущую запись.');
    if (!globalThis.isSecureContext) throw new Error('Для записи откройте сайт через HTTPS или localhost на этом компьютере.');
    if (!navigator.mediaDevices?.getUserMedia || !globalThis.AudioContext || !globalThis.AudioWorkletNode) {
      throw new Error('Этот браузер не поддерживает запись звука. Откройте сайт в Chrome или Edge на компьютере.');
    }
    if (source === 'tab' && !navigator.mediaDevices.getDisplayMedia) {
      throw new Error('Запись вкладки недоступна в этом браузере. Откройте сайт в Chrome или Edge на компьютере.');
    }
    this.elapsed = 0;
    this.levels = {};
    this._set('requesting');
    window.addEventListener('beforeunload', this._unload);
    let requesting = source;
    try {
      // Must precede API awaits: the browser requires a direct click to share a tab.
      if (source === 'tab') {
        const tab = await navigator.mediaDevices.getDisplayMedia({
          video: { displaySurface: 'browser' }, audio: true,
          selfBrowserSurface: 'exclude', surfaceSwitching: 'exclude', systemAudio: 'exclude',
        });
        this.streams.push(tab);
        if (!tab.getAudioTracks().length) {
          throw new Error('Звук не передаётся. Выберите именно вкладку Meet/Zoom и включите «Также передавать звук вкладки».');
        }
      }
      if (source !== 'tab' || includeMicrophone) {
        requesting = 'microphone';
        const mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
        });
        this.streams.push(mic);
      }
      this.ctx = new AudioContext();
      await this.ctx.audioWorklet.addModule('/lib/pcm-worklet.js');
      await this.ctx.resume();
      this.sink = this.ctx.createGain();
      this.sink.gain.value = 0;
      this.sink.connect(this.ctx.destination);
      const capture = await this.api('POST', `/meetings/${this.meetingId}/capture-sessions`, {
        mode: source === 'tab' ? 'browser_tab' : 'local_mic',
      });
      this.captureSessionId = capture.id;
      const url = new URL('/api/v1/ingest/ws', location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      this.client = new IngestClient({
        wsUrl: url.href, captureSessionId: capture.id, maxBufferSeconds: 180,
        client: { name: 'hattama-web', version: '1.0.0', platform: navigator.platform || 'web' },
        onStatus: status => this._transportStatus(status),
      });
      const origin = Math.round((performance.timeOrigin + performance.now()) * 1000);
      this.streams.forEach((stream, index) => {
        const tab = source === 'tab' && index === 0;
        const sourceId = tab ? 'tab' : 'mic';
        const entry = this.client.addSource(sourceId, tab ? 'tab_audio' : source === 'tab' ? 'microphone' : 'local_microphone',
          this.ctx.sampleRate, tab ? 'Звук вкладки встречи' : 'Микрофон');
        entry.epochStartWallUs = origin; // Both sources use one AudioContext clock.
        this._attach(stream, sourceId);
        for (const track of stream.getTracks()) track.addEventListener('ended', () => {
          if (this._releasing || ['saving', 'recoverable', 'stopped'].includes(this.status)) return;
          this.error = 'Источник звука отключён. Сохраняем записанную часть встречи.';
          this._emit();
          this.stop().catch(error => this._set('recoverable', recordingError(error)));
        });
      });
      this.client.connect();
      this.runningSince = performance.now();
      this._set('recording');
      this.timer = setInterval(() => this._emit(), 250);
      return this.snapshot();
    } catch (error) {
      this._freezeClock();
      await this._releaseMedia();
      this.client?.disconnect();
      window.removeEventListener('beforeunload', this._unload);
      const message = recordingError(error, requesting);
      this._set('error', message);
      throw new Error(message);
    }
  }

  _attach(stream, sourceId) {
    const input = this.ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(this.ctx, 'pcm-chunker', {
      processorOptions: { chunkFrames: Math.round(this.ctx.sampleRate * 0.2) },
    });
    node.port.onmessage = ({ data }) => {
      if (data.type === 'chunk') {
        this.client.pushPcm(sourceId, new Int16Array(data.pcm));
        this.levels[sourceId] = this.status === 'paused' ? 0 : data.peak;
      } else if (data.requestId) {
        const key = `${sourceId}:${data.requestId}`;
        this.pendingControls.get(key)?.();
        this.pendingControls.delete(key);
      }
    };
    input.connect(node);
    node.connect(this.sink);
    this.nodes.push({ sourceId, input, node });
  }

  _control(paused) {
    return Promise.all(this.nodes.map(({ node, sourceId }) => new Promise((resolve, reject) => {
      const requestId = ++this.controlSeq;
      const key = `${sourceId}:${requestId}`;
      const timer = setTimeout(() => {
        this.pendingControls.delete(key);
        reject(new Error('Браузер не подтвердил сохранение последнего фрагмента звука. Оставьте страницу открытой и повторите завершение.'));
      }, 5000);
      this.pendingControls.set(key, () => { clearTimeout(timer); resolve(); });
      // Pausing flushes the partial chunk before the acknowledgment arrives.
      node.port.postMessage({ type: 'pause', paused, requestId });
    })));
  }

  async pause() {
    if (this.status !== 'recording') return;
    if (this._pausePromise) return this._pausePromise;
    this._freezeClock();
    this._pausePromise = (async () => {
      await this._control(true);
      for (const source of this.client.sources.values()) this.client.setSourceState(source.sourceId, 'muted');
      this.levels = {};
      if (this.status === 'recording') this._set('paused', this.error);
    })();
    try { await this._pausePromise; }
    finally { this._pausePromise = null; }
  }

  async resume() {
    if (this.status !== 'paused') return;
    if (this._resumePromise) return this._resumePromise;
    if (this.client._fatal) throw new Error('Соединение закрыто сервером. Сохраните запись перед новой попыткой.');
    if (this.client.unackedSeconds() >= 120) throw new Error('Дождитесь восстановления соединения: записанный звук ещё сохраняется.');
    this._resumePromise = (async () => {
      await this.ctx.resume();
      if (this.status !== 'paused') return;
      await this._control(false);
      if (this.status !== 'paused') return;
      for (const source of this.client.sources.values()) this.client.setSourceState(source.sourceId, 'unmuted');
      this.runningSince = performance.now();
      this._set('recording');
    })();
    try { await this._resumePromise; }
    finally { this._resumePromise = null; }
  }

  async stop() {
    if (this._stopPromise) return this._stopPromise;
    if (!this.client || this.status === 'stopped') return this.snapshot();
    this._stopPromise = this._save();
    try { return await this._stopPromise; }
    finally { this._stopPromise = null; }
  }

  retryStop() { return this.stop(); }

  async _save() {
    this._freezeClock();
    this._set('saving');
    try {
      if (this._pausePromise) await this._pausePromise;
      if (this._resumePromise) await this._resumePromise;
      if (this.nodes.length) await this._control(true);
      await this._releaseMedia();
      const saved = await this.client.stop('user_stop', 20000);
      if (!saved) {
        this._set('recoverable', 'Сервер ещё не подтвердил сохранение записи. Звук остаётся в этой вкладке. Проверьте соединение и нажмите «Повторить сохранение». Не закрывайте страницу.');
      } else this._complete();
      return this.snapshot();
    } catch (error) {
      if (this.status === 'stopped') return this.snapshot();
      this._set('recoverable', recordingError(error));
      return this.snapshot();
    }
  }

  _transportStatus(event) {
    if (event.type === 'connected') {
      this.connected = true;
      if (this.status !== 'recoverable') this.error = null;
    } else if (event.type === 'disconnected' && this.status !== 'stopped') {
      this.connected = false;
      if (!this.client?._fatal) this.error = 'Связь с сервером прервана. Звук временно сохраняется в этой вкладке; подключение восстановится автоматически.';
    } else if (event.type === 'error') {
      this.error = event.message || 'Ошибка сохранения звука.';
      if (event.fatal) {
        this.pause().then(() => this._set('recoverable', event.message)).catch(error => this._set('recoverable', recordingError(error)));
      }
    } else if (event.type === 'buffer' && event.seconds >= 120 && this.status === 'recording' && !this._pausePromise) {
      this.error = 'Запись приостановлена: больше двух минут звука ожидают сохранения. Дождитесь соединения и продолжите запись.';
      this.pause().catch(error => this._set('recoverable', recordingError(error)));
    } else if (event.type === 'overflow' || event.type === 'gap') {
      this.error = 'Часть звука не удалось передать. В протоколе будет отмечен пропуск; проверьте запись после обработки.';
    } else if (event.type === 'stop_requested') {
      this.stop().catch(error => this._set('recoverable', recordingError(error)));
    } else if (event.type === 'session_stopped') {
      this._complete();
    }
    this._emit();
  }

  _complete() {
    this._freezeClock();
    clearInterval(this.timer);
    window.removeEventListener('beforeunload', this._unload);
    this.connected = false;
    // The server may finalize independently (for example after its stop timeout).
    // Stop device tracks immediately, even if the UI never called stop().
    this._releaseMedia().catch(error => {
      this.error = `Запись сохранена, но браузер сообщил об ошибке освобождения звука: ${recordingError(error)}`;
      this._emit();
    });
    this._set('stopped');
  }

  async _releaseMedia() {
    this._releasing = true;
    for (const stream of this.streams) for (const track of stream.getTracks()) track.stop();
    this.streams = [];
    for (const { input, node } of this.nodes) { input.disconnect(); node.disconnect(); node.port.close(); }
    this.nodes = [];
    this.sink?.disconnect();
    this.sink = null;
    const ctx = this.ctx;
    this.ctx = null;
    if (ctx && ctx.state !== 'closed') await ctx.close();
    this._releasing = false;
  }
}
