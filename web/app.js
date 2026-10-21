// RigUp dashboard. Polls /api/state; every action is a POST the server runs in the background.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const FIT = { measured: 'measured', works: 'runs', estimate: 'estimate' };
const ENGINE = { vllm: 'vLLM', llamacpp: 'llama.cpp', mlx: 'MLX' };
let S = null, tier = null, armed = null, armTimer, note = null;

const host = location.hostname;
const post = async (path, body) => {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'rigup' }, body: JSON.stringify(body || {}) });
  note = r.ok ? null : (await r.json().catch(() => ({}))).error || 'HTTP ' + r.status;
  load();
};
const gb = n => n == null ? '—' : n >= 100 ? Math.round(n) : n;
const ctx = n => n ? (n >= 1000 ? Math.round(n / 1024) + 'K' : n) : '—';
const dots = n => n ? `<span class="dots" title="${n}/5">${[1, 2, 3, 4, 5].map(i => `<i class="${n >= i ? 'f' : n >= i - .5 ? 'h' : ''}"></i>`).join('')}</span>` : '—';
const fitBadge = m => { const f = m.fit[tier]; return f ? `<span class="badge fit-${f}" title="${f === 'works' ? 'Runs on' : f === 'measured' ? 'Measured on' : 'Not run yet on'} a ${tier} GB card">${FIT[f]}</span>` : ''; };
const tooBig = () => S?.tier && tier > S.tier;

// Arm-then-confirm: the first tap shows what will happen, the second does it.
const act = (key, label, confirm, extra = '') => `<button class="lab-go ${armed === key ? 'arm' : ''}" data-act="${key}" ${S?.busy || tooBig() ? 'disabled' : ''} ${extra}>${armed === key ? confirm : label}</button>`;

function progress(m) {
  const d = S?.dl;
  if (d?.id !== m.id) return S?.busy && S.target === m.id ? `<span class="lab-go busy">${esc(S.busy)}…</span>` : null;
  const pct = d.total ? Math.floor(d.done / d.total * 100) : 0;
  return `<span class="lab-go busy" title="${esc(d.file)}">${pct}% · ${esc(d.file.slice(0, 18))}</span>`;
}

// Auto-tune result for this GPU: badge plus the numbers measured on it, replacing the recipe's.
function tunedBadge(m) {
  const t = S.tuned[m.id];
  if (!t) return '';
  return t.error ? `<span class="badge fit-estimate" title="${esc(t.error)}">no fit</span>`
    : `<span class="badge fit-measured" title="Auto-tuned on your ${esc(t.gpu)}, ${esc(t.date)}">tuned</span>`;
}

function llmRow(m) {
  const on = S.mode === m.id, k = 'switch:' + m.id, t = S.tuned[m.id]?.tps ? S.tuned[m.id] : null;
  const btn = progress(m) || (on ? `<span class="lab-go on">${S.healthy ? 'Active' : 'Loading…'}</span>`
    : act(k, m.installed ? `Switch · ${m.start}` : `Get ${gb(m.disk_gb)} GB + switch`, S.mode === 'comfy' ? 'Tap again · ComfyUI stops' : 'Tap again to switch'));
  const rm = m.installed && !on ? act('rm:' + m.id, 'Remove', `Delete ${gb(m.disk_gb)} GB?`, 'data-small') : '';
  return `<div class="lab-row ${on ? 'on' : ''}" title="${esc(m.quant + ' · ' + m.license)}">
    <span class="lab-name"><span class="led ${on && S.healthy ? '' : 'idle'}"></span><b>${esc(m.name)}${m.vision ? ' <span class="badge vis">vision</span>' : ''} ${m.new ? '<span class="badge vis">new</span>' : ''} ${S.tuned[m.id] ? tunedBadge(m) : fitBadge(m)}</b>
      <span class="mono dim">${esc(ENGINE[m.engine] || m.engine)} · ${esc(m.quant)}${m.installed ? '' : ` · ${gb(m.disk_gb)} GB download`}</span>
      <span class="lab-note">${esc(m.note)}</span></span>
    <span class="lab-stat"><b>${ctx(t ? t.ctx : m.ctx)}</b><i>context</i></span>
    <span class="lab-stat"><b>${t ? t.tps : m.tps ?? '—'}</b><i>tok/s</i></span>
    <span class="lab-stat">${dots(m.smart)}<i>smarts</i></span>
    <span class="lab-stat"><b>${t ? t.vram : m.vram ? (m.fit[tier] === 'estimate' ? '≈' : '') + m.vram : '—'}</b><i>GB VRAM</i></span>
    <span class="lab-acts">${btn}${rm}</span></div>`;
}

function comfyRow(m) {
  const k = (m.installed ? 'rm:' : 'get:') + m.id;
  const btn = progress(m) || (m.installed ? act(k, 'Installed · Remove', `Delete ${gb(m.disk_gb)} GB?`) : act(k, `Install · ${gb(m.disk_gb)} GB`, 'Tap again to download'));
  const lic = /research/i.test(m.license) ? 'warn-t' : '';
  return `<div class="lab-row info ${m.installed ? '' : 'missing'}" title="${esc(m.open + ' · ' + m.license)}">
    <span class="lab-name"><span class="led ${m.installed && S.mode === 'comfy' ? '' : 'idle'}"></span><b>${esc(m.name)} <span class="badge">${esc(m.kind)}</span> ${m.new ? '<span class="badge vis">new</span>' : ''} ${fitBadge(m)}</b>
      <span class="lab-note">${esc(m.note)}</span><span class="mono dim open-hint">${esc(m.open)}</span></span>
    <span class="lab-stat"><b>${m.steps ?? '—'}</b><i>steps</i></span>
    <span class="lab-stat"><b>${m.secs ? m.secs + 's' : '—'}</b><i>sec</i></span>
    <span class="lab-stat"><b>${m.max ?? '—'}</b><i>max size</i></span>
    <span class="lab-stat"><b class="mono ${lic}" style="font-size:10px">${esc(m.license.split(' (')[0].replace(/ license$/i, ''))}</b><i>license</i></span>
    <span class="lab-acts">${btn}</span></div>`;
}

// Recent Hugging Face models from the watch list that have no recipe yet (filled in by auto-tune).
function releases() {
  const r = S.releases;
  if (!r?.hf?.length) return r?.checked ? `<div class="mono dim mode-spec">New releases checked ${esc(r.checked)}: nothing new that fits.</div>` : '';
  return `<div class="releases"><div class="mono dim mode-spec">New on Hugging Face, no recipe yet · checked ${esc(r.checked)} · ≈ size at 4-bit</div>
    ${r.hf.map(x => `<a class="rel mono" href="https://huggingface.co/${esc(x.id)}" target="_blank" rel="noopener"><span>${esc(x.id)}</span>
      <span class="dim">${esc(x.created)}${x.params_b ? ` · ${x.params_b}B · ≈${x.q4_gb} GB` : ''}</span></a>`).join('')}</div>`;
}

// Opens a prefilled GitHub issue with this GPU's auto-tune results. Nothing is sent until the user submits it.
function share() {
  const g = S.gpu, body = { rigup: 1, gpu: g?.name, vram_mb: g?.mem_total, driver: g?.driver, results: S.tuned };
  const url = `https://github.com/${S.repo}/issues/new?title=${encodeURIComponent('Auto-tune results: ' + (g?.name || 'GPU'))}`
    + `&body=${encodeURIComponent('```json\n' + JSON.stringify(body, null, 1) + '\n```')}`;
  window.open(url, '_blank', 'noopener');
}

const head = cols => `<div class="lab-row head mono"><span>Model</span>${cols.map(c => `<span>${c}</span>`).join('')}</div>`;

function render() {
  if (!S) return;
  const g = S.gpu, mode = S.mode, models = S.models.filter(m => m.fit[tier]), mac = S.platform === 'mac';
  const llms = models.filter(m => m.group === 'llm'), packs = models.filter(m => m.group === 'comfy');
  const active = S.models.find(m => m.id === mode);
  $('#host').textContent = host;
  $('#tiers').innerHTML = S.tiers.map(t => `<a role="tab" data-tier="${t}" aria-selected="${t === tier}">${t} GB${t === S.tier ? ' · yours' : ''}</a>`).join('');

  $('#g-title').innerHTML = S.busy === 'switching' ? `Switching to <em>${esc(S.target === 'comfy' ? 'ComfyUI' : S.models.find(m => m.id === S.target)?.name)}</em>`
    : mode === 'comfy' ? 'Running <em>ComfyUI</em>' : active ? `Running <em>${esc(active.name)}</em>` : g ? 'GPU is <em>free</em>' : 'No GPU found';
  $('#g-lede').innerHTML = (g ? `<span class="pill"><span class="led ${mode && S.healthy ? '' : 'idle'}"></span>${esc(g.name)}<span class="why">${mode ? (S.healthy ? 'serving' : 'loading') : 'idle'}</span></span>` : '')
    + (mode ? ` <button class="pill stop" data-act="stop" ${S.busy ? 'disabled' : ''}>${armed === 'stop' ? 'Tap again to free the GPU' : 'Free GPU'}</button>` : '');

  const d = S.disk;
  $('#meters').innerHTML = (g ? [
    [mac ? `Model memory · ${g.ram_gb} GB Mac` : 'VRAM', `${(g.mem_used / 1024).toFixed(1)}<span class="dim u"> / ${(g.mem_total / 1024).toFixed(0)} GB</span>`, g.mem_used / g.mem_total],
    ['GPU load', `${g.util}<span class="dim u">%</span>`, g.util / 100, g.util],
    ['Temperature', `${g.temp}<span class="dim u">°C</span>`, g.temp / 90, g.temp],
    ['Power', `${g.power}<span class="dim u"> W</span>`, g.power / 450, g.power],
  ].filter(r => r.length < 4 || r[3] != null) : []).concat([[`Disk · ${d.free_gb} GB free`, `${d.total_gb - d.free_gb}<span class="dim u"> / ${d.total_gb} GB</span>`, 1 - d.free_gb / d.total_gb]])
    .map(([l, v, p]) => `<div class="meter"><div class="stat-num">${v}</div><div class="mono stat-lbl">${l}</div><div class="bar"><i style="width:${Math.min(100, Math.round(p * 100))}%"></i></div></div>`).join('');

  const warn = tooBig() ? `<p class="warn-t mono tier-warn">Your card has ${S.tier} GB. These are the ${tier} GB recipes, shown for reference.</p>` : '';
  $('#lab').innerHTML = `<div class="mode-meta mono"><span>01</span><span>${active ? '● Active' : ''}</span></div>
    <h3>Lab · LLM</h3><p>One model at a time serves an OpenAI-compatible API on :${S.ports.llm}, used by ${mac ? 'chat apps' : 'Open WebUI'} and coding agents. Missing files download on the first switch.</p>${warn}
    <div class="lab-rows">${llms.length ? head(['context', 'tok/s', 'smarts', mac ? 'GB used' : 'GB VRAM', '']) + llms.map(llmRow).join('') : `<span class="muted">No LLM recipes for ${tier} GB yet.</span>`}</div>
    <div class="mono dim mode-spec">tok/s and ${mac ? 'memory measured on an M4 Pro Mac (24 GB)' : 'VRAM measured on an RTX 3090 (24 GB)'}; "tuned" rows show numbers from your own ${mac ? 'Mac' : 'card'}. ≈ = estimate. Smarts: rough 1–5. Hover a row for quant and license.</div>
    <div class="comfy-go">${act('tune', 'Auto-tune', 'Tap again · tests every installed model, a few min each')}
      ${Object.keys(S.tuned).length ? `<button class="lab-go" data-share>Share results</button>` : ''}</div>${releases()}`;

  const comfyOn = mode === 'comfy', ck = 'switch:comfy';
  $('#comfy').innerHTML = `<div class="mode-meta mono"><span>02</span><span>${comfyOn ? (S.healthy ? '● Active' : 'Loading…') : ''}</span></div>
    <h3>ComfyUI · ${mac ? 'images' : 'images &amp; video'}</h3><p>ComfyUI takes the whole ${mac ? 'GPU' : 'card'} on :${S.ports.comfy}. Install the model packs you want, then open the template or workflow named under each one.</p>${warn}
    <div class="lab-rows">${packs.length ? head(['steps', 'sec', 'max size', 'license', '']) + packs.map(comfyRow).join('') : `<span class="muted">No image recipes for ${tier} GB yet.</span>`}</div>
    <div class="mono dim mode-spec">1024×1024, warm run${mac ? ', on an M4 Pro Mac (24 GB)' : ''}. Max size = largest native output. The first ComfyUI start installs ComfyUI itself and takes several minutes.</div>
    <div class="comfy-go">${comfyOn ? `<span class="lab-go on">${S.healthy ? 'Active' : 'Loading…'}</span>` : act(ck, 'Switch to ComfyUI', mode ? 'Tap again · the LLM stops' : 'Tap again to start')}</div>`;

  const llmUp = mode && mode !== 'comfy' && S.healthy, comfyUp = comfyOn && S.healthy;
  const links = [
    ['Dashboard', `http://${host}:${location.port || 80}`, true, 'this page'],
    ['Open WebUI', `http://${host}:${S.ports.webui}`, true, llmUp ? 'chat with the Lab model' : 'needs a Lab model for answers'],
    ['LLM API', `http://${host}:${S.ports.llm}/v1`, llmUp, llmUp ? `model "${mode}"` : 'needs a Lab model', true],
    ['ComfyUI', `http://${host}:${S.ports.comfy}`, comfyUp, comfyUp ? 'images and video' : 'needs ComfyUI mode'],
  ].filter(([n, url]) => Object.values(S.ports).includes(+url.split(':')[2].split('/')[0]) || n === 'Dashboard');
  $('#g-links').innerHTML = links.map(([n, url, on, sub, copy]) => `<div class="link-row ${on ? '' : 'off'}">
    <span class="port mono">:${esc(url.split(':')[2].split('/')[0])}</span>
    <div style="min-width:0"><div class="app-name"><span class="led ${on ? '' : 'idle'}"></span>${esc(n)}</div><div class="app-host">${esc(url.replace('http://', ''))} · ${esc(sub)}</div></div>
    ${copy ? `<button class="link-act" data-copy="${esc(url)}">Copy URL</button>` : `<a class="link-act" href="${esc(url)}" target="_blank" rel="noopener">Open ↗</a>`}</div>`).join('');
  $('#tok-state').textContent = S.hf_token ? 'saved' : mac ? 'only needed for gated models' : 'needed for FLUX 3 Klein and LTX-2.5';
  $('#key-state').textContent = S.api_key ? `clients must send it on :${S.ports.llm} as a Bearer token. Applies from the next model start`
    : `none: anyone who can reach :${S.ports.llm} can use it. Save an empty key to remove one`;

  const log = $('#g-log');
  log.textContent = (S.log.join('\n') || 'Nothing yet.') + (S.error || note ? '\n\nERROR: ' + (note || S.error) : '');
  log.scrollTop = log.scrollHeight;
  $('#g-state').textContent = S.busy ? S.busy + '…' : '';
}

async function load() {
  try { S = await (await fetch('api/state')).json(); } catch { $('#g-title').textContent = 'Server unreachable'; return; }
  tier ??= S.tier || 24;
  render();
}

document.addEventListener('click', async ev => {
  const t = ev.target.closest('[data-tier]');
  if (t) { tier = +t.dataset.tier; return render(); }
  if (ev.target.closest('[data-share]')) return share();
  const c = ev.target.closest('[data-copy]');
  if (c) { try { await navigator.clipboard.writeText(c.dataset.copy); } catch {} c.textContent = 'Copied'; return; }
  const a = ev.target.closest('[data-act]');
  if (!a || a.disabled) return;
  const k = a.dataset.act;
  if (armed !== k) { armed = k; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = null; render(); }, 4000); return render(); }
  armed = null;
  const [verb, id] = k.split(':');
  if (verb === 'stop') return post('api/stop');
  if (verb === 'tune') return post('api/autotune');
  post({ switch: 'api/switch', get: 'api/install', rm: 'api/uninstall' }[verb], { to: id, id });
});

for (const [form, input, field] of [['#token', '#tok', 'hf_token'], ['#apikey', '#key', 'api_key']])
  $(form).onsubmit = async e => {
    e.preventDefault();
    await post('api/token', { [field]: $(input).value });
    $(input).value = '';
  };

document.addEventListener('pointermove', e => {
  const glow = $('#cursorGlow'); glow.style.left = e.clientX + 'px'; glow.style.top = e.clientY + 'px';
});
const tick = () => $('#clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
tick(); setInterval(tick, 10000);
(function loop() { load().finally(() => setTimeout(loop, S?.busy ? 1500 : 5000)); })();
