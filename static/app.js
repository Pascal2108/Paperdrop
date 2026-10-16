// Paperdrop, Copyright (C) 2026 Pascal2108
// SPDX-License-Identifier: AGPL-3.0-only
(() => {
  'use strict';

  const boot = JSON.parse(document.getElementById('boot').textContent);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const make = id => document.getElementById(id).content.firstElementChild.cloneNode(true);

  const app = $('.app'), queue = $('[data-queue]'), sentList = $('[data-sent]'),
        staged = $('[data-staged]'), alerts = $('[data-alerts]'), compressBtn = $('[data-compress]'),
        viewForm = $('#view');

  let picked = [];            // files waiting to be compressed
  let quality = boot.quality;
  let merge = 'images';       // off, images (all photos into one PDF) or all (every file into one PDF)
  let working = false;        // a compress or "upload all" run is in progress
  const pending = new Map();  // Paperless task id -> pill element still waiting for a final status

  // ---------- session ----------
  // The login is kept in a cookie. Once it has run out, '/' shows the login page again.
  const leave = () => location.replace('/');
  // A longer delay than setTimeout can hold (about 24.8 days) would fire at once and reload forever
  setTimeout(leave, Math.min(boot.expiresIn * 1000, 2 ** 31 - 1));

  async function api(path, data = {}) {
    const body = new FormData();
    for (const [key, value] of Object.entries(data))
      for (const v of [].concat(value)) body.append(key, v);
    let res;
    try { res = await fetch(path, { method: 'POST', headers: { 'X-Auth': boot.auth }, body }); }
    catch { return { error: 'No connection to the server.' }; }
    if (res.status === 401) { leave(); return new Promise(() => {}); }
    let json = null;
    try { json = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) return { error: (json && json.error) || `Server error (${res.status}).` };
    return json || {};
  }

  // ---------- helpers ----------
  const mb = n => (n / 1048576).toFixed(2) + ' MB';
  const stem = name => name.replace(/\.[^.]+$/, '');
  const isPdf = f => f.type === 'application/pdf' || /\.pdf$/i.test(f.name);
  const isImage = f => /^image\/(jpeg|png)$/.test(f.type) || /\.(jpe?g|png)$/i.test(f.name);
  const joinedTitle = stems => stems.length <= 2 ? stems.join(' + ') : `${stems[0]} + ${stems.length - 1} more`;
  const clock = () => new Date().toTimeString().slice(0, 5);
  const span = (text, cls) => { const s = document.createElement('span'); s.textContent = text; if (cls) s.className = cls; return s; };

  function alertMsg(text) {
    const el = make('t-alert');
    $('span', el).textContent = text;
    alerts.append(el);
  }

  function refresh() {
    const docs = $$('.doc', queue);
    const ready = docs.filter(d => !d.matches('.busy, .sending')).length;
    $('[data-count]').textContent = docs.length;
    $('[data-queue-empty]').hidden = docs.length > 0;
    $$('[data-n]').forEach(n => n.textContent = ready);
    $$('[data-all]').forEach(b => { b.disabled = !ready || working; b.classList.toggle('gone', !ready); });
    $('[data-dock]').hidden = !ready;
    $('[data-sent-empty]').hidden = sentList.children.length > 0;
    sentList.hidden = !sentList.children.length;
    compressBtn.disabled = !picked.length || working;
    compressBtn.textContent = working ? 'Compressing …'
      : picked.length ? `Compress ${picked.length} file${picked.length > 1 ? 's' : ''}` : 'Add files to compress';
  }

  // ---------- choosing files ----------
  function renderPicked() {
    staged.replaceChildren(...picked.map((file, i) => {
      const chip = make('t-chip');
      chip.dataset.i = i;
      $('.n', chip).textContent = file.name;
      $('.s', chip).textContent = mb(file.size);
      return chip;
    }));
    refresh();
  }

  // The list is the order in which merged files end up in the PDF. A file is moved by
  // dragging it (with a finger: by its grip) or with the arrow keys on its grip.
  let dragged = null;
  staged.addEventListener('pointerdown', e => {
    const chip = e.target.closest('.chip');
    if (!chip || picked.length < 2 || e.button !== 0 || e.target.closest('[data-remove]')) return;
    if (e.pointerType !== 'mouse' && !e.target.closest('[data-grip]')) return;
    e.preventDefault();
    staged.setPointerCapture(e.pointerId);
    dragged = chip;
    chip.classList.add('dragging');
    staged.classList.add('sorting');
  });
  staged.addEventListener('pointermove', e => {
    if (!dragged) return;
    const over = $$('.chip', staged).find(c => {
      const r = c.getBoundingClientRect();
      return e.clientY >= r.top && e.clientY < r.bottom;
    });
    if (!over || over === dragged) return;
    // Take the place of the row under the pointer
    if (dragged.compareDocumentPosition(over) & Node.DOCUMENT_POSITION_FOLLOWING) over.after(dragged);
    else over.before(dragged);
  });
  const endDrag = () => {
    if (!dragged) return;
    dragged = null;
    staged.classList.remove('sorting');
    picked = $$('.chip', staged).map(chip => picked[Number(chip.dataset.i)]);
    renderPicked();
  };
  staged.addEventListener('pointerup', endDrag);
  staged.addEventListener('pointercancel', endDrag);
  staged.addEventListener('keydown', e => {
    const step = { ArrowUp: -1, ArrowDown: 1 }[e.key];
    const grip = e.target.closest('[data-grip]');
    if (!step || !grip) return;
    e.preventDefault();
    const i = Number(grip.closest('.chip').dataset.i), j = i + step;
    if (j < 0 || j >= picked.length) return;
    [picked[i], picked[j]] = [picked[j], picked[i]];
    renderPicked();
    $$('[data-grip]', staged)[j].focus();
  });

  function addFiles(files, fromCamera) {
    for (let file of files) {
      if (!isPdf(file) && !isImage(file) && !fromCamera) { alertMsg(`${file.name}: only PDF, JPG and PNG are allowed`); continue; }
      if (file.size > boot.maxMb * 1048576) { alertMsg(`${file.name}: larger than ${boot.maxMb} MB`); continue; }
      if (fromCamera) {
        // Phones call every photo "image.jpg"; give it a name that works as a title
        const d = new Date(), p = n => String(n).padStart(2, '0');
        const name = `Scan ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}.${p(d.getMinutes())}.${p(d.getSeconds())}`;
        file = new File([file], name + (file.type === 'image/png' ? '.png' : '.jpg'), { type: file.type });
      }
      picked.push(file);
    }
    renderPicked();
  }

  for (const id of ['files', 'camera']) {
    const input = document.getElementById(id);
    input.addEventListener('change', async () => {
      const chosen = [...input.files];
      input.value = '';
      // A camera photo first goes through the document scanner, unless it is switched off
      const scanner = id === 'camera' && boot.scanner && window.PaperdropScanner;
      if (!scanner || !scanner.supported) { addFiles(chosen, id === 'camera'); return; }
      for (const photo of chosen) {
        const page = await scanner.edit(photo);
        if (page) addFiles([page], true);
      }
    });
  }

  const drop = $('[data-drop]');
  drop.addEventListener('click', e => {
    const pick = e.target.closest('[data-pick]');
    if (pick) document.getElementById(pick.dataset.pick).click();
    else if (!e.target.closest('input')) document.getElementById('files').click();
  });
  // Without this, a file dropped next to the drop zone would replace the page
  for (const type of ['dragover', 'drop']) document.addEventListener(type, e => e.preventDefault());
  drop.addEventListener('dragover', () => drop.classList.add('over'));
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { drop.classList.remove('over'); addFiles(e.dataTransfer.files, false); });

  // ---------- review cards ----------
  function fillDoc(doc, item) {
    doc.dataset.id = item.id;
    doc.classList.remove('busy');
    $('.title', doc).value = item.title;
    $('.title', doc).disabled = false;
    $('.thumb', doc).disabled = false;
    $('.pg', doc).textContent = item.pages || '';
    $('.pg', doc).hidden = !item.pages;
    $('.prog', doc).hidden = true;
    const pct = item.before ? Math.round((item.after / item.before - 1) * 100) : 0;
    const meta = [span(`${mb(item.before)} → ${mb(item.after)}`)];
    if (pct) meta.push(span(`${pct > 0 ? '+' : '−'}${Math.abs(pct)} %`, 'save'));
    const files = item.files || Math.max(item.images, 1);  // documents staged before "files" existed
    if (files > 1) meta.push(span(`merged from ${files} ${item.images === files ? 'images' : 'files'}`));
    else if (item.images === 1) meta.push(span('image converted to PDF'));
    $('.meta', doc).replaceChildren(...meta);
    return doc;
  }

  function busyDoc(group) {
    const doc = make('t-doc');
    doc.classList.add('busy');
    $('.title', doc).value = joinedTitle(group.map(f => stem(f.name)));
    $('.title', doc).disabled = true;
    $('.thumb', doc).disabled = true;
    $('.pg', doc).hidden = group.length < 2;
    $('.pg', doc).textContent = group.length;
    $('.prog', doc).hidden = false;
    const what = group.every(isImage) ? 'images' : 'files';
    $('.meta', doc).replaceChildren(span(group.length > 1 ? `Compressing · merging ${group.length} ${what}` : 'Compressing'));
    return doc;
  }

  const fail = (doc, text) => {
    $$('.fail', doc).forEach(el => el.remove());
    if (text) $('.meta', doc).append(span(text, 'fail'));
  };

  compressBtn.addEventListener('click', async () => {
    if (!picked.length || working) return;
    // One request per resulting document, its files in the order of the list:
    // everything merged, all images merged where the first one is, or each file alone
    const images = picked.filter(isImage);
    const groups = [];
    if (merge === 'all') groups.push(picked);
    else for (const file of picked) {
      if (merge === 'images' && isImage(file)) { if (file === images[0]) groups.push(images); }
      else groups.push([file]);
    }
    picked = [];
    working = true;
    const jobs = groups.map(group => { const doc = busyDoc(group); queue.append(doc); return { group, doc }; });
    renderPicked();
    for (const { group, doc } of jobs) {
      const res = await api('/api/compress', { files: group, quality });
      if (res.item) fillDoc(doc, res.item);
      else { doc.remove(); alertMsg(res.error); }
      refresh();
    }
    working = false;
    refresh();
  });

  async function upload(doc) {
    if (doc.matches('.busy, .sending')) return;
    fail(doc, '');
    doc.classList.add('sending');
    $$('button, input', doc).forEach(el => el.disabled = true);
    refresh();
    const res = await api(`/api/item/${doc.dataset.id}/upload`, { title: $('.title', doc).value });
    if (res.error) {
      doc.classList.remove('sending');
      $$('button, input', doc).forEach(el => el.disabled = false);
      fail(doc, res.error);
    } else {
      addSent(res.title, res.task);
      doc.remove();
    }
    refresh();
  }

  async function remove(doc) {
    $$('button', doc).forEach(el => el.disabled = true);
    const res = await api(`/api/item/${doc.dataset.id}/delete`);
    if (res.error && !/no longer exists/.test(res.error)) {
      $$('button', doc).forEach(el => el.disabled = false);
      fail(doc, res.error);
    } else doc.remove();
    refresh();
  }

  // ---------- sent list ----------
  const PILL = { consumed: 'ok', 'in queue': 'wait', sent: 'wait', failed: 'err', duplicate: 'err' };

  function setPill(pill, state, detail) {
    pill.textContent = state;
    pill.className = 'pill ' + (PILL[state] || 'wait');
    pill.title = detail || '';
  }

  function addSent(title, task) {
    const li = make('t-sent');
    $('.n', li).textContent = title;
    $('time', li).textContent = clock();
    setPill($('.pill', li), task ? 'in queue' : 'sent');
    sentList.prepend(li);
    if (task) { pending.set(task, $('.pill', li)); schedulePoll(); }
  }

  // Ask what Paperless did with the uploads until each one has a final status
  let pollTimer = null;
  function schedulePoll() { if (!pollTimer && pending.size) pollTimer = setTimeout(poll, 4000); }
  async function poll() {
    pollTimer = null;
    const res = await api('/api/tasks', { ids: [...pending.keys()].join(',') });
    for (const [task, info] of Object.entries(res.tasks || {})) {
      const pill = pending.get(task);
      if (!pill) continue;
      setPill(pill, info.state, info.detail);
      if (info.final) pending.delete(task);
    }
    schedulePoll();
  }

  // ---------- buttons ----------
  app.addEventListener('click', async e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const doc = btn.closest('.doc');
    if (btn.matches('[data-up]')) upload(doc);
    else if (btn.matches('[data-del]')) remove(doc);
    else if (btn.matches('[data-view]')) {
      viewForm.action = `/item/${doc.dataset.id}/view`;
      viewForm.elements.auth.value = boot.auth;
      viewForm.submit();
    }
    else if (btn.matches('[data-all]')) {
      working = true;
      for (const d of $$('.doc', queue)) await upload(d);
      working = false;
      refresh();
    }
    else if (btn.matches('[data-q]')) {
      quality = btn.dataset.q;
      $$('[data-q]').forEach(b => b.setAttribute('aria-pressed', b === btn));
    }
    else if (btn.matches('[data-m]')) {
      merge = btn.dataset.m;
      $$('[data-m]').forEach(b => b.setAttribute('aria-pressed', b === btn));
    }
    else if (btn.matches('[data-remove]')) { picked.splice(Number(btn.closest('.chip').dataset.i), 1); renderPicked(); }
    else if (btn.matches('[data-dismiss]')) btn.closest('.alert').remove();
    else if (btn.matches('[data-out]')) { await Promise.race([api('/api/logout'), new Promise(r => setTimeout(r, 2000))]); leave(); }
  });

  // ---------- start ----------
  $$('[data-q]').forEach(b => b.setAttribute('aria-pressed', b.dataset.q === quality));
  for (const item of boot.items) queue.append(fillDoc(make('t-doc'), item));
  refresh();

  // Connection to Paperless, checked now and once a minute
  const conn = $('[data-conn]');
  async function checkPaperless() {
    const res = await api('/api/ping');
    conn.classList.toggle('on', !!res.ok);
    conn.classList.toggle('off', !res.ok);
    // The label is the same on every screen; which server it is shows on hover
    conn.title = `${res.detail || res.error || ''} (${conn.dataset.host})`;
    $('[data-conn-label]', conn).textContent = res.ok ? 'Paperless' : 'Paperless off';
  }
  checkPaperless();
  setInterval(checkPaperless, 60000);
})();
