// Paperdrop, Copyright (C) 2026 Pascal2108
// SPDX-License-Identifier: AGPL-3.0-only
//
// Document scanner for camera photos: finds the page in the photo, lets the user move its
// corners with a magnifier, and straightens it. Everything runs here in the browser with
// plain canvas code; nothing is loaded from elsewhere, and the server only receives the
// finished JPEG, which it checks like any other upload.
(() => {
  'use strict';

  const MAX_SOURCE = 4000;  // longest side worked on; keeps phones within their canvas limits
  const MAX_OUTPUT = 3508;  // longest side of the result: A4 at 300 dpi
  const DETECT = 400;       // longest side of the copy the page is searched in

  const canvas = (w, h) => Object.assign(document.createElement('canvas'), { width: w, height: h });
  const ctx = c => c.getContext('2d', { willReadFrequently: true });
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const area = q => Math.abs(q.reduce((s, p, i) => s + cross([0, 0], p, q[(i + 1) % q.length]), 0)) / 2;
  const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r)));

  // ---------- loading ----------
  async function load(file) {
    const bmp = await createImageBitmap(file);  // the browser applies the camera's rotation
    const s = Math.min(1, MAX_SOURCE / Math.max(bmp.width, bmp.height));
    const c = canvas(Math.round(bmp.width * s), Math.round(bmp.height * s));
    ctx(c).drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close();
    return c;
  }

  // ---------- finding the page ----------
  function boxBlur(src, w, h, r) {
    const tmp = new Float32Array(src.length), out = new Float32Array(src.length);
    for (let y = 0; y < h; y++) {
      let sum = 0;
      for (let x = -r; x <= r; x++) sum += src[y * w + Math.min(w - 1, Math.max(0, x))];
      for (let x = 0; x < w; x++) {
        tmp[y * w + x] = sum / (2 * r + 1);
        sum += src[y * w + Math.min(w - 1, x + r + 1)] - src[y * w + Math.max(0, x - r)];
      }
    }
    for (let x = 0; x < w; x++) {
      let sum = 0;
      for (let y = -r; y <= r; y++) sum += tmp[Math.min(h - 1, Math.max(0, y)) * w + x];
      for (let y = 0; y < h; y++) {
        out[y * w + x] = sum / (2 * r + 1);
        sum += tmp[Math.min(h - 1, y + r + 1) * w + x] - tmp[Math.max(0, y - r) * w + x];
      }
    }
    return out;
  }

  // Threshold that best separates two groups of brightness (Otsu), among the values in [lo, hi]
  function otsu(gray, lo = 0, hi = 255) {
    const hist = new Array(256).fill(0);
    let total = 0;
    for (const v of gray) { const i = Math.min(255, v | 0); if (i >= lo && i <= hi) { hist[i]++; total++; } }
    let sum = 0;
    hist.forEach((n, i) => { sum += n * i; });
    let sumB = 0, wB = 0, best = 0, t = Math.round((lo + hi) / 2);
    for (let i = 0; i < 256; i++) {
      wB += hist[i];
      const wF = total - wB;
      if (!wB || !wF) continue;
      sumB += i * hist[i];
      const between = wB * wF * (sumB / wB - (sum - sumB) / wF) ** 2;
      if (between > best) { best = between; t = i; }
    }
    return t;
  }

  // Largest connected area of the mask, as its pixel count and its outline points
  function largestArea(mask, w, h) {
    const seen = new Uint8Array(w * h), queue = new Int32Array(w * h);
    let best = null;
    for (let start = 0; start < mask.length; start++) {
      if (!mask[start] || seen[start]) continue;
      let head = 0, tail = 0, count = 0;
      const edge = [];
      queue[tail++] = start; seen[start] = 1;
      while (head < tail) {
        const i = queue[head++], x = i % w, y = (i - x) / w;
        count++;
        let outline = false;
        for (const [nx, ny] of [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]]) {
          if (nx < 0 || ny < 0 || nx >= w || ny >= h) { outline = true; continue; }
          const j = ny * w + nx;
          if (!mask[j]) { outline = true; continue; }
          if (!seen[j]) { seen[j] = 1; queue[tail++] = j; }
        }
        if (outline) edge.push([x, y]);
      }
      if (!best || count > best.count) best = { count, edge };
    }
    return best;
  }

  function convexHull(points) {
    const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const half = list => {
      const out = [];
      for (const pt of list) {
        while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], pt) <= 0) out.pop();
        out.push(pt);
      }
      out.pop();
      return out;
    };
    return [...half(p), ...half([...p].reverse())];
  }

  // The four outline points that enclose the largest area: the page's corners
  function corners(hull) {
    if (hull.length < 4) return null;
    const c = hull.reduce((s, p) => [s[0] + p[0] / hull.length, s[1] + p[1] / hull.length], [0, 0]);
    const far = (from, pts) => pts.reduce((b, p) => (dist(p, from) > dist(b, from) ? p : b));
    const a = far(c, hull), b = far(a, hull);
    let p = null, q = null, dp = 0, dq = 0;
    for (const pt of hull) {
      const s = cross(a, b, pt);
      if (s > dp) { dp = s; p = pt; }
      if (-s > dq) { dq = -s; q = pt; }
    }
    if (!p || !q) return null;
    const quad = [a, p, b, q];
    for (let pass = 0; pass < 3; pass++) {
      for (let k = 0; k < 4; k++) {
        for (const pt of hull) {
          const trial = quad.slice(); trial[k] = pt;
          if (area(trial) > area(quad)) quad[k] = pt;
        }
      }
    }
    return order(quad);
  }

  // Clockwise, starting top left
  function order(q) {
    const c = q.reduce((s, p) => [s[0] + p[0] / 4, s[1] + p[1] / 4], [0, 0]);
    const sorted = [...q].sort((a, b) => Math.atan2(a[1] - c[1], a[0] - c[0]) - Math.atan2(b[1] - c[1], b[0] - c[0]));
    const first = sorted.reduce((m, p, i) => (p[0] + p[1] < sorted[m][0] + sorted[m][1] ? i : m), 0);
    return [...sorted.slice(first), ...sorted.slice(0, first)];
  }

  // Corners of the page as fractions of the photo, or null if no page stands out
  function detect(src) {
    const s = DETECT / Math.max(src.width, src.height);
    const w = Math.max(8, Math.round(src.width * s)), h = Math.max(8, Math.round(src.height * s));
    const small = canvas(w, h);
    ctx(small).drawImage(src, 0, 0, w, h);
    const d = ctx(small).getImageData(0, 0, w, h).data;
    let gray = new Float32Array(w * h);
    for (let i = 0; i < gray.length; i++) gray[i] = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2];
    gray = boxBlur(boxBlur(gray, w, h, 2), w, h, 2);
    // Usually a light page on a darker background. A second split inside the bright part tells
    // white paper from a light table; one inside the dark part handles a dark page on light ground.
    const t = otsu(gray);
    const tries = [[t, true], [otsu(gray, t + 1, 255), true], [t, false], [otsu(gray, 0, t - 1), false]];
    const edge = 0.02;  // corners closer than this to the photo's edge
    let best = null;
    for (const [limit, bright] of tries) {
      const mask = new Uint8Array(w * h);
      for (let i = 0; i < mask.length; i++) mask[i] = (gray[i] > limit) === bright ? 1 : 0;
      const part = largestArea(mask, w, h);
      if (!part || part.count < 0.12 * w * h) continue;
      const quad = corners(convexHull(part.edge));
      if (!quad) continue;
      const a = area(quad);
      const fill = part.count / a;  // close to 1 for a page, low for the background around it
      if (a > 0.97 * w * h || fill < 0.82) continue;
      // A shape running into the photo's edge with two or more corners is a shadow or the
      // background rather than a page
      const atEdge = quad.filter(([x, y]) => x < edge * w || y < edge * h || x > (1 - edge) * w || y > (1 - edge) * h);
      if (atEdge.length >= 2) continue;
      const score = fill * Math.min(1, a / (0.35 * w * h));
      if (!best || score > best.score) best = { score, quad };
    }
    if (!best) return null;
    // Pulled in by 1 % towards the middle, so that no sliver of the table stays at the edges
    const c = best.quad.reduce((s, p) => [s[0] + p[0] / 4, s[1] + p[1] / 4], [0, 0]);
    return best.quad.map(([x, y]) => [(c[0] + (x - c[0]) * 0.99) / w, (c[1] + (y - c[1]) * 0.99) / h]);
  }

  // ---------- straightening ----------
  // Projection that maps the corners of the output rectangle onto the page corners in the photo
  function homography(from, to) {
    const a = [], b = [];
    from.forEach(([x, y], i) => {
      const [u, v] = to[i];
      a.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      a.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    });
    for (let c = 0; c < 8; c++) {  // Gaussian elimination
      let p = c;
      for (let r = c + 1; r < 8; r++) if (Math.abs(a[r][c]) > Math.abs(a[p][c])) p = r;
      [a[c], a[p]] = [a[p], a[c]]; [b[c], b[p]] = [b[p], b[c]];
      for (let r = 0; r < 8; r++) {
        if (r === c) continue;
        const f = a[r][c] / a[c][c];
        for (let k = c; k < 8; k++) a[r][k] -= f * a[c][k];
        b[r] -= f * b[c];
      }
    }
    return b.map((v, i) => v / a[i][i]);
  }

  // The page's real width : height. Edges further from the camera look shorter, so this is
  // worked out from the perspective of all four corners (Zhang & He, "Whiteboard scanning and
  // image enhancement"), assuming the photo's centre is the camera's centre. Null if the
  // corners give no believable camera, e.g. for a page photographed straight from above.
  function pageRatio(pts, cx, cy, size) {
    const [m1, m2, m4, m3] = pts.map(([x, y]) => [x - cx, y - cy, 1]);  // TL, TR, BR, BL
    const crs = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const k2 = dot(crs(m1, m4), m3) / dot(crs(m2, m4), m3);
    const k3 = dot(crs(m1, m4), m2) / dot(crs(m3, m4), m2);
    const n2 = m2.map((v, i) => k2 * v - m1[i]), n3 = m3.map((v, i) => k3 * v - m1[i]);
    const f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2]);
    if (!(f2 > 0) || !Number.isFinite(f2) || Math.sqrt(f2) < 0.3 * size || Math.sqrt(f2) > 4 * size) return null;
    const r = Math.sqrt((n2[0] ** 2 + n2[1] ** 2 + f2 * n2[2] ** 2) / (n3[0] ** 2 + n3[1] ** 2 + f2 * n3[2] ** 2));
    return Number.isFinite(r) && r > 0.2 && r < 5 ? r : null;
  }

  function straighten(src, q) {
    const pts = q.map(([x, y]) => [x * src.width, y * src.height]);
    let w0 = Math.max(dist(pts[0], pts[1]), dist(pts[3], pts[2]));
    let h0 = Math.max(dist(pts[0], pts[3]), dist(pts[1], pts[2]));
    const ratio = pageRatio(pts, src.width / 2, src.height / 2, Math.max(src.width, src.height));
    if (ratio) {  // keep the larger measured side, derive the other from the real proportions
      if (w0 / ratio >= h0) h0 = w0 / ratio; else w0 = h0 * ratio;
    }
    const s = Math.min(1, MAX_OUTPUT / Math.max(w0, h0));
    const W = Math.max(1, Math.round(w0 * s)), H = Math.max(1, Math.round(h0 * s));
    const m = homography([[0, 0], [W, 0], [W, H], [0, H]], pts);
    const sw = src.width, sh = src.height, sd = ctx(src).getImageData(0, 0, sw, sh).data;
    const out = canvas(W, H), img = ctx(out).createImageData(W, H), od = img.data;
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const den = m[6] * x + m[7] * y + 1;
        let u = (m[0] * x + m[1] * y + m[2]) / den, v = (m[3] * x + m[4] * y + m[5]) / den;
        u = Math.min(sw - 1.001, Math.max(0, u)); v = Math.min(sh - 1.001, Math.max(0, v));
        const x0 = u | 0, y0 = v | 0, fx = u - x0, fy = v - y0;
        const i00 = (y0 * sw + x0) * 4, i10 = i00 + 4, i01 = i00 + sw * 4, i11 = i01 + 4;
        const o = (y * W + x) * 4;
        for (let c = 0; c < 3; c++) {
          const top = sd[i00 + c] + (sd[i10 + c] - sd[i00 + c]) * fx;
          const bottom = sd[i01 + c] + (sd[i11 + c] - sd[i01 + c]) * fx;
          od[o + c] = top + (bottom - top) * fy;
        }
        od[o + 3] = 255;
      }
    }
    ctx(out).putImageData(img, 0, 0);
    return out;
  }

  // ---------- the editor ----------
  const FULL = [[0, 0], [1, 0], [1, 1], [0, 1]];
  const INSET = [[0.06, 0.06], [0.94, 0.06], [0.94, 0.94], [0.06, 0.94]];
  const LOUPE = 112, ZOOM = 3;  // magnifier while a corner is moved: size in CSS pixels, enlargement

  function edit(file) {
    const box = document.querySelector('[data-scan]');
    const stage = box.querySelector('[data-scan-stage]');
    const view = box.querySelector('[data-scan-view]');
    const svg = box.querySelector('[data-scan-frame]');
    const shape = svg.querySelector('polygon');
    const handles = [...svg.querySelectorAll('[data-corner]')];
    const note = box.querySelector('[data-scan-note]');
    const done = box.querySelector('[data-scan-done]');
    const loupe = box.querySelector('[data-scan-loupe]');
    const pic = box.querySelector('.scan-pic');
    const result = box.querySelector('[data-scan-result]');
    const editOnly = [...box.querySelectorAll('[data-scan-full], [data-scan-done]')];
    const previewOnly = [...box.querySelectorAll('[data-scan-back], [data-scan-use]')];

    return new Promise(resolve => {
      let src = null, quad = INSET.map(p => [...p]), dragging = -1, busy = false;
      let page = null;  // the straightened page while it is previewed
      let shownW = 1, shownH = 1;  // size of the photo on screen, in CSS pixels
      const cleanup = [];
      const on = (el, type, fn) => { el.addEventListener(type, fn); cleanup.push(() => el.removeEventListener(type, fn)); };
      const close = result => {
        cleanup.forEach(f => f());
        box.hidden = true;
        document.documentElement.classList.remove('scanning');
        resolve(result);
      };

      const draw = () => {
        // In screen pixels, so that the corner circles keep a finger-sized radius on every phone
        const w = shownW, h = shownH;
        svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
        const pts = quad.map(([x, y]) => [x * w, y * h]);
        shape.setAttribute('points', pts.map(p => p.join(',')).join(' '));
        handles.forEach((el, i) => { el.setAttribute('cx', pts[i][0]); el.setAttribute('cy', pts[i][1]); });
      };
      // Draws an image as large as the stage allows; returns its size on screen in CSS pixels
      const show = (image, target) => {
        const room = getComputedStyle(stage);
        const s = Math.min((stage.clientWidth - parseFloat(room.paddingLeft) - parseFloat(room.paddingRight)) / image.width,
                           (stage.clientHeight - parseFloat(room.paddingTop) - parseFloat(room.paddingBottom)) / image.height, 1);
        const w = Math.max(1, Math.round(image.width * s)), h = Math.max(1, Math.round(image.height * s));
        const sharp = Math.min(2, devicePixelRatio || 1);
        target.width = Math.round(w * sharp);
        target.height = Math.round(h * sharp);
        target.style.width = `${w}px`;
        target.style.height = `${h}px`;
        ctx(target).drawImage(image, 0, 0, target.width, target.height);
        return [w, h];
      };
      const fit = () => {
        if (page) { show(page, result); return; }
        if (!src) return;
        [shownW, shownH] = show(src, view);
        svg.style.width = `${shownW}px`;
        svg.style.height = `${shownH}px`;
        draw();
      };
      // Editing the corners, or looking at the straightened page before taking it
      const mode = previewing => {
        pic.hidden = previewing;
        result.hidden = !previewing;
        editOnly.forEach(b => { b.hidden = previewing; });
        previewOnly.forEach(b => { b.hidden = !previewing; });
      };
      const setBusy = (on, text) => {
        busy = on;
        note.textContent = text;
        box.querySelectorAll('button').forEach(b => { b.disabled = on && !b.matches('[data-scan-cancel]'); });
      };

      // The magnifier shows the photo around the corner being moved, enlarged, with the two frame
      // edges that meet there. It sits above the finger, which would otherwise cover the spot,
      // and below it when the corner is near the top.
      const magnify = () => {
        const sharp = Math.min(3, devicePixelRatio || 1), px = Math.round(LOUPE * sharp);
        if (loupe.width !== px) { loupe.width = loupe.height = px; loupe.style.width = loupe.style.height = `${LOUPE}px`; }
        const [x, y] = quad[dragging], g = ctx(loupe);
        const span = LOUPE / ZOOM / shownW * src.width;  // photo pixels shown across the magnifier
        g.fillStyle = '#0b0c0d'; g.fillRect(0, 0, px, px);
        g.drawImage(src, x * src.width - span / 2, y * src.height - span / 2, span, span, 0, 0, px, px);
        const at = ([qx, qy]) => [px / 2 + (qx - x) * shownW * ZOOM * sharp, px / 2 + (qy - y) * shownH * ZOOM * sharp];
        g.lineWidth = 2 * sharp; g.strokeStyle = '#f5a623';
        g.beginPath();
        g.moveTo(...at(quad[(dragging + 3) % 4])); g.lineTo(px / 2, px / 2); g.lineTo(...at(quad[(dragging + 1) % 4]));
        g.stroke();
        g.lineWidth = sharp; g.strokeStyle = '#ffffff';
        g.beginPath();
        g.moveTo(px / 2 - 10 * sharp, px / 2); g.lineTo(px / 2 + 10 * sharp, px / 2);
        g.moveTo(px / 2, px / 2 - 10 * sharp); g.lineTo(px / 2, px / 2 + 10 * sharp);
        g.stroke();
        const cx = x * shownW, cy = y * shownH, gap = 34;
        const left = Math.min(shownW - LOUPE / 2, Math.max(LOUPE / 2, cx)) - LOUPE / 2;
        const top = cy - gap - LOUPE >= -40 ? cy - gap - LOUPE : cy + gap;
        loupe.style.transform = `translate(${left}px, ${top}px)`;
        loupe.hidden = false;
      };

      on(svg, 'pointerdown', e => {
        const el = e.target.closest('[data-corner]');
        if (!el || busy) return;
        e.preventDefault();
        dragging = handles.indexOf(el);
        svg.setPointerCapture(e.pointerId);
        magnify();
      });
      on(svg, 'pointermove', e => {
        if (dragging < 0) return;
        const r = svg.getBoundingClientRect();
        quad[dragging] = [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
                          Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
        draw();
        magnify();
      });
      const drop = () => { dragging = -1; loupe.hidden = true; };
      on(svg, 'pointerup', drop);
      on(svg, 'pointercancel', drop);
      on(stage, 'contextmenu', e => e.preventDefault());  // long press on the photo: no browser menu
      on(window, 'resize', fit);
      on(document, 'keydown', e => { if (e.key === 'Escape') close(null); });

      on(box, 'click', async e => {
        const btn = e.target.closest('button');
        if (!btn) return;
        if (btn.matches('[data-scan-cancel]')) close(null);
        else if (btn.matches('[data-scan-full]')) { quad = FULL.map(p => [...p]); draw(); }
        else if (btn.matches('[data-scan-done]') && src && !busy) {
          setBusy(true, 'Straightening the page …');
          await frame();
          try {
            page = straighten(src, order(quad));
            mode(true);
            fit();
            setBusy(false, 'This is how the page will look.');
            box.querySelector('[data-scan-use]').focus();
          } catch {
            page = null;
            setBusy(false, 'This photo could not be processed. Use the whole photo or try again.');
          }
        } else if (btn.matches('[data-scan-back]') && !busy) {
          page = null;  // the corners stay where they were
          mode(false);
          fit();
          setBusy(false, 'Drag the corners if they are not on the page edges.');
        } else if (btn.matches('[data-scan-use]') && page && !busy) {
          setBusy(true, 'Saving the page …');
          const blob = await new Promise(r => page.toBlob(r, 'image/jpeg', 0.9));
          close(new File([blob], 'scan.jpg', { type: 'image/jpeg' }));
        }
      });

      mode(false);
      loupe.hidden = true;
      box.hidden = false;
      document.documentElement.classList.add('scanning');
      setBusy(true, 'Looking for the page …');
      done.focus();
      (async () => {
        try {
          src = await load(file);
        } catch {
          close(file);  // the browser cannot open it here: upload the photo as taken
          return;
        }
        await frame();
        const found = detect(src);
        quad = found || INSET.map(p => [...p]);
        fit();
        setBusy(false, found ? 'Drag the corners if they are not on the page edges.'
                             : 'No page found. Drag the corners to the page edges.');
      })();
    });
  }

  window.PaperdropScanner = { edit, detect: c => detect(c), supported: typeof createImageBitmap === 'function' };
})();
