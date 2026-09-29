'use strict';

// One state object, one drawing function per section. It isn't a homemade
// framework: it's the minimum to avoid scattering getElementById everywhere.
const state = { engine: 'off', discord: 'off', voices: [], guild: null,
                voice_channel: null, queue: 0, exit_code: null, environment: [] };

const $ = (sel) => document.querySelector(sel);
const make = (tag, props = {}) => Object.assign(document.createElement(tag), props);

// ----------------------------------------------------------------- drawing

function drawStatus() {
  const engineOn = state.engine === 'on';
  const starting = state.engine === 'starting';

  $('#light-engine').className = 'light ' + (engineOn ? 'on' : starting ? 'starting' : '');
  $('#label-engine').textContent = engineOn ? 'on' : starting ? 'starting…' : 'off';
  $('#btn-engine').textContent = engineOn || starting ? 'Stop' : 'Start';
  $('#detail-engine').textContent = engineOn
    ? `${state.voices.length} voice(s) · queue: ${state.queue}`
    : state.exit_code != null && state.exit_code !== 0
      ? `the engine crashed (code ${state.exit_code}) — see the log below`
      : 'off';

  const connected = state.discord === 'on';
  $('#light-discord').className = 'light ' + (connected ? 'on' : '');
  $('#label-discord').textContent = connected ? 'connected' : 'offline';
  $('#btn-discord').textContent = connected ? 'Disconnect' : 'Connect';
  $('#detail-discord').textContent = connected
    ? `${state.guild ?? '—'}${state.voice_channel ? ' · ' + state.voice_channel : ' · not in a voice channel'}`
    : 'connecting also starts the engine, if it is off';
}

function drawVoices() {
  const list = $('#voice-list');
  list.replaceChildren(...state.voices.map((voice) => {
    const li = make('li', { className: voice.hidden ? 'hidden' : '' });
    const left = make('span');
    left.append(
      make('span', { className: 'name', textContent: voice.name }),
      make('span', { className: 'size', textContent: ` ${(voice.bytes / 1e6).toFixed(1)} MB` }),
    );
    const button = make('button', { textContent: voice.hidden ? 'Show' : 'Hide' });
    button.onclick = () => send(`/api/voices/${encodeURIComponent(voice.name)}`,
                                { hidden: !voice.hidden });
    li.append(left, button);
    return li;
  }));
}

// Says what each missing piece is for and how to get it, instead of a bare
// "not found": the person reading this just installed the project.
function drawEnvironment() {
  const missing = (state.environment ?? []).filter((c) => !c.ok);
  const box = $('#environment-warning');
  box.hidden = missing.length === 0;
  if (missing.length === 0) return;
  const items = missing.map((c) => {
    const item = make('div', { className: 'missing' });
    const title = make('p');
    title.append(make('b', { textContent: c.what }), ` — ${c.why}. `,
                 make('span', { className: 'detail', textContent: `(${c.detail})` }));
    item.append(title, make('pre', { textContent: c.fix.join('\n') }));
    return item;
  });
  const docs = make('p', { className: 'detail' });
  docs.append('Step by step in ',
              make('a', { href: missing[0].docs, target: '_blank', textContent: 'Setup, in the README' }),
              '. The warning goes away by itself once everything is in place.');
  box.replaceChildren(
    make('b', { textContent: "Setup isn't complete yet" }),
    make('p', { className: 'detail', textContent:
      'The engine, Discord and Generate work without these; cloning voices needs them.' }),
    ...items, docs);
}

// The <select> is rebuilt on every new state, but the choice of whoever is
// typing can't vanish halfway through the sentence.
function drawGenerateVoices() {
  const select = $('#generate-voice');
  const before = select.value;
  const visible = state.voices.filter((v) => !v.hidden);
  select.replaceChildren(...visible.map((v) =>
    make('option', { value: v.name, textContent: v.name })));
  if (visible.some((v) => v.name === before)) select.value = before;

  $('#generate-note').textContent = state.engine === 'on'
    ? `${visible.length} voice(s) available`
    : 'start the engine on the Status tab before generating';
}

function drawAll() {
  drawStatus();
  drawVoices();
  drawGenerateVoices();
  drawEnvironment();
}

function appendLog(line) {
  const log = $('#log');
  const stuck = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.append(line + '\n');
  // A cap on the DOM: a panel left open all week can't turn into a leak.
  while (log.childNodes.length > 500) log.firstChild.remove();
  if (stuck) log.scrollTop = log.scrollHeight;
}

// -------------------------------------------------------------------- network

// Starting the engine takes seconds and connecting to Discord can take more.
// Without locking the buttons, the natural reaction is to click again - and
// that's how two 1.3 GB engines got started or two Discord sessions opened.
// The server also defends itself with locks; this removes the trigger.
let busy = false;

function lockButtons(lock) {
  for (const b of document.querySelectorAll('main button')) b.disabled = lock;
}

// Keeps the last error body for whoever needs to look at one of its fields
// (save's `exists`, for example) without turning everything into try/catch.
let lastError = {};

async function send(route, body) {
  if (busy) return null;
  busy = true;
  lockButtons(true);
  lastError = {};
  try {
    const resp = await fetch(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) {
      lastError = data;
      appendLog(`[panel] error: ${data.error ?? resp.status}`);
      return null;
    }
    // A response that carries state updates the cards; the others (generated
    // audio, saved config) go straight to the caller.
    if ('engine' in data) {
      Object.assign(state, data);
      drawAll();
    }
    return data;
  } catch (e) {
    appendLog(`[panel] couldn't reach the panel: ${e.message}`);
    return null;
  } finally {
    busy = false;
    lockButtons(false);
  }
}

function connectSSE() {
  const source = new EventSource('/events');
  source.addEventListener('log', (e) => appendLog(e.data));
  source.addEventListener('prep', (e) => {
    const log = $('#clone-log');
    log.append(e.data + '\n');
    log.scrollTop = log.scrollHeight;
  });
  source.addEventListener('status', (e) => {
    Object.assign(state, JSON.parse(e.data));
    drawAll();
  });
  // EventSource reconnects by itself; we only say so, so the log doesn't lie.
  source.onerror = () => appendLog('[panel] connection lost, retrying…');
}

// -------------------------------------------------------------------- history

async function loadHistory(period) {
  const h = await (await fetch(`/api/history?period=${period}`)).json();

  $('#history-summary').textContent = h.total
    ? `${h.total} utterance(s) from ${h.first_day} to ${h.last_day} · `
      + `${h.chars} characters, ${h.avg_chars} per utterance`
    : 'No utterances in this period.';

  const rank = (target, map) => {
    const items = Object.entries(map).sort((a, b) => b[1] - a[1]).slice(0, 8);
    $(target).replaceChildren(...items.map(([key, n]) =>
      make('li', { textContent: `${key} — ${n}` })));
  };
  rank('#rank-users', h.by_user);
  rank('#rank-voices', h.by_voice);

  $('#utterance-table tbody').replaceChildren(...h.latest.map((u) => {
    const tr = make('tr', { className: u.event === 'failure' ? 'failure' : '' });
    tr.append(
      make('td', { textContent: (u.time ?? '').slice(0, 16).replace('T', ' ') }),
      make('td', { textContent: u.user ?? '—' }),
      make('td', { textContent: u.voice ?? '—' }),
      make('td', { textContent: u.error ?? u.text ?? u.event }),
    );
    return tr;
  }));
}

// ------------------------------------------------------------- generate audio

function showAudio(prefix, data) {
  const url = `/api/audio/${encodeURIComponent(data.file)}`;
  $(`#${prefix}-player`).src = url;
  $(`#${prefix}-detail`).textContent =
    `${data.file} · ${data.seconds}s · ${(data.bytes / 1024).toFixed(0)} KB`;
  return url;
}

async function generate() {
  const voice = $('#generate-voice').value;
  const text = $('#generate-text').value.trim();
  if (!voice || !text) { appendLog('[panel] pick a voice and write the text'); return; }

  $('#generate-note').textContent = 'generating… this takes about as long as the audio';
  const data = await send('/api/say', { voice, text });
  if (!data) { drawGenerateVoices(); return; }

  const url = showAudio('generate', data);
  $('#download-wav').href = url;
  $('#download-ogg').href = url.replace(/\.wav$/, '.ogg');
  $('#generate-output').hidden = false;
  drawGenerateVoices();
}

// ---------------------------------------------------------------------- clone

const clone = { file: null, ref: null, name: null };

async function uploadSample(file) {
  if (!file) return;
  const body = new FormData();
  body.append('audio', file, file.name);

  $('#clone-upload').textContent = `uploading ${file.name}…`;
  lockButtons(true);
  try {
    const resp = await fetch('/api/clone/upload', { method: 'POST', body });
    const data = await resp.json();
    if (!resp.ok) { $('#clone-upload').textContent = `error: ${data.error}`; return; }
    clone.file = data.file;
    $('#clone-upload').textContent =
      `${data.file} · ${(data.bytes / 1e6).toFixed(1)} MB in the pending folder`;
  } finally {
    lockButtons(false);
    $('#btn-prepare').disabled = clone.file === null;
  }
}

async function prepare() {
  $('#clone-step-prep').hidden = false;
  $('#clone-log').textContent = '';
  const data = await send('/api/clone/prepare',
                          { file: clone.file, trim: $('#clone-trim').checked });
  if (!data) return;

  // SSE already showed it line by line; this covers the case of the tab
  // opening halfway through the preparation.
  if (!$('#clone-log').textContent) $('#clone-log').textContent = data.output.join('\n');

  if (!data.ok) {
    appendLog(`[panel] the preparation failed (code ${data.code})`);
    return;
  }
  clone.ref = data.ref;
  $('#clone-step-name').hidden = false;
  $('#clone-name').focus();
}

async function importVoice() {
  const name = $('#clone-name').value.trim();
  if (!name) { $('#clone-note').textContent = 'give the voice a name'; return; }

  $('#clone-note').textContent = 'importing and generating the test sentence…';
  const data = await send('/api/clone/import', { ref: clone.ref, name });
  if (!data) { $('#clone-note').textContent = '—'; return; }

  clone.name = data.name;
  $('#clone-note').textContent = '—';
  showAudio('clone', data);
  $('#clone-exists').textContent = data.exists
    ? `a voice "${data.name}" already exists — saving will ask for confirmation`
    : '';
  $('#clone-step-test').hidden = false;
}

async function saveVoice() {
  let data = await send('/api/clone/save', { name: clone.name });
  if (!data && lastError.exists) {
    if (!confirm(`Overwrite the existing voice "${clone.name}"?`)) return;
    data = await send('/api/clone/save', { name: clone.name, overwrite: true });
  }
  if (!data) return;
  appendLog(`[panel] voice "${clone.name}" saved in voices/`);
  resetClone();
}

async function discardVoice() {
  if (await send('/api/clone/discard', { name: clone.name })) {
    appendLog(`[panel] voice "${clone.name}" discarded`);
    resetClone();
  }
}

function resetClone() {
  clone.file = clone.ref = clone.name = null;
  $('#clone-file').value = '';
  $('#clone-name').value = '';
  $('#clone-upload').textContent = 'no file yet';
  $('#btn-prepare').disabled = true;
  for (const id of ['prep', 'name', 'test']) $(`#clone-step-${id}`).hidden = true;
}

// --------------------------------------------------------------------- config

async function loadConfig() {
  const c = await (await fetch('/api/config')).json();

  $('#config-fields').replaceChildren(...c.keys.map((key) => {
    const label = make('label', { textContent: key });
    label.append(make('input', {
      type: 'text', id: `cfg-${key}`, value: c.values[key] ?? '',
      placeholder: '(not in the .env)',
    }));
    if (c.descriptions?.[key]) {
      label.append(make('span', { className: 'detail', textContent: c.descriptions[key] }));
    }
    return label;
  }));
  $('#config-note').textContent = 'on Discord, the new value applies after restarting the engine';
  $('#btn-restart').hidden = true;
}

async function saveConfig() {
  const fields = [...document.querySelectorAll('#config-fields input')];
  const changes = Object.fromEntries(fields
    .filter((i) => i.value.trim() !== '')
    .map((i) => [i.id.slice(4), i.value.trim()]));

  if (Object.keys(changes).length === 0) {
    $('#config-note').textContent = 'nothing to save';
    return;
  }

  const data = await send('/api/config', { changes });
  if (!data) return;
  // The panel already uses the new value; what needs a restart is the bot, in the engine.
  const note = data.restart.length
    ? `saved. On Discord, ${data.restart.join(', ')} only apply after restarting the engine.`
    : 'saved.';
  $('#config-note').textContent = note;
  $('#btn-restart').hidden = data.restart.length === 0 || state.engine === 'off';
}

// -------------------------------------------------------------------- wiring

$('#btn-engine').onclick = () =>
  send('/api/engine', { action: state.engine === 'off' ? 'start' : 'stop' });

$('#btn-discord').onclick = () =>
  send('/api/discord', { action: state.discord === 'off' ? 'connect' : 'disconnect' });

$('#btn-generate').onclick = generate;
$('#btn-prepare').onclick = prepare;
$('#btn-import').onclick = importVoice;
$('#btn-save').onclick = saveVoice;
$('#btn-discard').onclick = discardVoice;
$('#btn-config').onclick = saveConfig;
$('#btn-restart').onclick = () => send('/api/engine', { action: 'restart' });

$('#clone-file').onchange = (e) => uploadSample(e.target.files[0]);

// Drag and drop: it's how a call recording will arrive here.
const drop = $('#drop');
for (const event of ['dragenter', 'dragover']) {
  drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.add('over'); });
}
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', (e) => {
  e.preventDefault();
  drop.classList.remove('over');
  uploadSample(e.dataTransfer.files[0]);
});

$('#tabs').onclick = (e) => {
  const target = e.target.dataset.tab;
  if (!target) return;
  for (const b of $('#tabs').children) b.classList.toggle('active', b.dataset.tab === target);
  for (const s of document.querySelectorAll('main section')) s.hidden = s.id !== `tab-${target}`;
  if (target === 'history') loadHistory('all');
  if (target === 'config') loadConfig();
};

$('#periods').onclick = (e) => {
  const period = e.target.dataset.period;
  if (!period) return;
  for (const b of $('#periods').children) b.classList.toggle('active', b.dataset.period === period);
  loadHistory(period);
};

// Loads once over HTTP: the page has to work even before SSE opens.
fetch('/api/state').then((r) => r.json()).then((data) => {
  Object.assign(state, data);
  drawAll();
  connectSSE();
});
