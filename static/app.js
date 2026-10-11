(() => {
  'use strict';

  const boot = JSON.parse(document.getElementById('boot').textContent);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const make = id => document.getElementById(id).content.firstElementChild.cloneNode(true);

  const app = $('.app'), queue = $('[data-queue]'), sentList = $('[data-sent]'),
        staged = $('[data-staged]'), alerts = $('[data-alerts]'), compressBtn = $('[data-compress]'),
        merge = $('[data-merge]'), viewForm = $('#view');

  let picked = [];            // files waiting to be compressed
  let quality = boot.quality;
  let working = false;        // a compress or "upload all" run is in progress
  const pending = new Map();  // Paperless task id -> pill element still waiting for a final status

  // ---------- session ----------
  // The session token lives only in this page. Leaving means going back to the login.
  const leave = () => location.replace('/');
  let idle;
  const restartIdle = () => { clearTimeout(idle); idle = setTimeout(leave, boot.sessionMinutes * 60000); };

  // touch=false for background requests, which do not count as activity
  async function api(path, data = {}, touch = true) {
    const body = new FormData();
    for (const [key, value] of Object.entries(data))
      for (const v of [].concat(value)) body.append(key, v);
    let res;
    try { res = await fetch(path, { method: 'POST', headers: { 'X-Auth': boot.auth }, body }); }
    catch { return { error: 'No connection to the server.' }; }
    if (res.status === 401) { leave(); return new Promise(() => {}); }
    if (touch) restartIdle();
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
      $('.n', chip).textContent = file.name;
      $('.s', chip).textContent = mb(file.size);
      $('[data-remove]', chip).dataset.i = i;
      return chip;
    }));
    refresh();
  }

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
    input.addEventListener('change', () => { addFiles(input.files, id === 'camera'); input.value = ''; });
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
    if (item.images === 1) meta.push(span('image converted to PDF'));
    else if (item.images > 1) meta.push(span(`merged from ${item.images} images`));
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
    $('.meta', doc).replaceChildren(span(group.length > 1 ? `Compressing · merging ${group.length} images` : 'Compressing'));
    return doc;
  }

  const fail = (doc, text) => {
    $$('.fail', doc).forEach(el => el.remove());
    if (text) $('.meta', doc).append(span(text, 'fail'));
  };

  compressBtn.addEventListener('click', async () => {
    if (!picked.length || working) return;
    // One request per resulting document: each PDF alone, images alone or all merged
    const images = picked.filter(isImage);
    const together = merge.checked && images.length > 1;
    const groups = [];
    for (const file of picked) {
      if (together && isImage(file)) { if (file === images[0]) groups.push(images); }
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
    const res = await api('/api/tasks', { ids: [...pending.keys()].join(',') }, false);
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
      restartIdle();
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
    else if (btn.matches('[data-remove]')) { picked.splice(Number(btn.dataset.i), 1); renderPicked(); }
    else if (btn.matches('[data-dismiss]')) btn.closest('.alert').remove();
    else if (btn.matches('[data-out]')) { await Promise.race([api('/api/logout'), new Promise(r => setTimeout(r, 2000))]); leave(); }
  });

  // ---------- start ----------
  $$('[data-q]').forEach(b => b.setAttribute('aria-pressed', b.dataset.q === quality));
  for (const item of boot.items) queue.append(fillDoc(make('t-doc'), item));
  restartIdle();
  refresh();

  // Connection to Paperless, checked now and once a minute
  const conn = $('[data-conn]');
  async function checkPaperless() {
    const res = await api('/api/ping', {}, false);
    conn.classList.toggle('on', !!res.ok);
    conn.classList.toggle('off', !res.ok);
    conn.title = res.detail || res.error || '';
    $('[data-conn-short]', conn).textContent = res.ok ? 'Paperless' : 'Paperless off';
  }
  checkPaperless();
  setInterval(checkPaperless, 60000);
})();
