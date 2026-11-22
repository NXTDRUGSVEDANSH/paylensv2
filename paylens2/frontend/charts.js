/* Hand-rolled SVG charts.
   No chart library: full control over the mark specs, and nothing to fetch at runtime.

   Mark specs follow the project's visualisation rules — 2px lines, 4px rounded
   data-ends anchored to the baseline, a 2px surface gap between adjacent fills,
   >=8px hover targets, recessive grid, legend whenever there are 2+ series, and
   selective direct labels rather than a number on every mark. */
(function (global) {
  const NS = 'http://www.w3.org/2000/svg';
  const SERIES = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7'];
  const MAX_SERIES = 7;         // an 8th folds into "Other", never a generated hue

  const el = (n, a = {}) => {
    const e = document.createElementNS(NS, n);
    for (const k in a) if (a[k] != null) e.setAttribute(k, a[k]);
    return e;
  };
  const cssVar = (v) => getComputedStyle(document.documentElement)
    .getPropertyValue(v).trim() || '#888';
  const seriesColor = (i) => cssVar(SERIES[i % SERIES.length]);

  const fmt = (v, kind) => {
    if (v == null || Number.isNaN(v)) return '—';
    if (kind === 'percent') return (v * 100).toFixed(2) + '%';
    if (kind === 'pp') return (v >= 0 ? '+' : '') + v.toFixed(2) + 'pp';
    if (kind === 'currency') return '₹' + Math.round(v).toLocaleString('en-IN');
    if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString('en-IN');
    return (Math.round(v * 100) / 100).toLocaleString('en-IN');
  };
  const niceMax = (v) => {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    return Math.ceil(v / p * 2) / 2 * p;
  };

  // ── tooltip ──────────────────────────────────────────────────────────────
  const tip = () => document.getElementById('tip');
  function bindTip(node, html) {
    node.addEventListener('pointerenter', (e) => {
      const t = tip(); t.innerHTML = html; t.classList.add('on');
      t.style.left = Math.min(e.clientX + 14, innerWidth - 300) + 'px';
      t.style.top = (e.clientY - 12) + 'px';
    });
    node.addEventListener('pointermove', (e) => {
      const t = tip();
      t.style.left = Math.min(e.clientX + 14, innerWidth - 300) + 'px';
      t.style.top = (e.clientY - 12) + 'px';
    });
    node.addEventListener('pointerleave', () => tip().classList.remove('on'));
  }

  function legend(items) {
    const d = document.createElement('div');
    d.className = 'legend';
    items.forEach(([label, color]) => {
      const s = document.createElement('span');
      s.innerHTML = `<i style="background:${color}"></i>${label}`;
      d.appendChild(s);
    });
    return d;
  }

  /* Fold beyond MAX_SERIES into a single "Other" rather than inventing hues. */
  function foldSeries(rows, keyCol, valCol) {
    if (rows.length <= MAX_SERIES) return rows;
    const sorted = [...rows].sort((a, b) => Math.abs(b[valCol]) - Math.abs(a[valCol]));
    const keep = sorted.slice(0, MAX_SERIES - 1);
    const rest = sorted.slice(MAX_SERIES - 1);
    const other = { [keyCol]: `Other (${rest.length})`, [valCol]:
      rest.reduce((s, r) => s + (r[valCol] || 0), 0) / rest.length, __other: true };
    return [...keep, other];
  }

  // ── bar / horizontal bar ─────────────────────────────────────────────────
  function bar(rows, { x, y, kind, horizontal = false }) {
    const data = foldSeries(rows.filter(r => r[y] != null), x, y);
    if (!data.length) return null;
    const wrap = document.createDocumentFragment();
    const W = 760, barH = 30, gap = 10;

    if (horizontal) {
      const padL = Math.min(210, Math.max(...data.map(d => String(d[x]).length)) * 7.6 + 16);
      const H = data.length * (barH + gap) + 26;
      const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
      const max = niceMax(Math.max(...data.map(d => d[y])));
      const plotW = W - padL - 86;
      for (let i = 0; i <= 4; i++) {
        const gx = padL + plotW * i / 4;
        svg.appendChild(el('line', { x1: gx, y1: 0, x2: gx, y2: H - 26, class: 'gridline' }));
        const t = el('text', { x: gx, y: H - 10, class: 'tick', 'text-anchor': 'middle' });
        t.textContent = fmt(max * i / 4, kind); svg.appendChild(t);
      }
      data.forEach((d, i) => {
        const yy = i * (barH + gap);
        const w = Math.max(2, plotW * (d[y] / max));
        const lab = el('text', { x: padL - 10, y: yy + barH / 2 + 4, class: 'tick',
                                 'text-anchor': 'end' });
        lab.textContent = String(d[x]).slice(0, 26); svg.appendChild(lab);
        // 4px rounded data-end, square against the baseline
        const r = el('path', {
          d: `M${padL},${yy} H${padL + w - 4} a4,4 0 0 1 4,4 V${yy + barH - 4}
              a4,4 0 0 1 -4,4 H${padL} Z`.replace(/\s+/g, ' '),
          fill: d.__other ? cssVar('--other') : seriesColor(i),
          stroke: cssVar('--card'), 'stroke-width': 2 });
        bindTip(r, `<b>${d[x]}</b><br>${y.replace(/_/g,' ')}: ${fmt(d[y], kind)}` +
          (d.attempts != null ? `<br>attempts: ${fmt(d.attempts)}` : ''));
        svg.appendChild(r);
        const v = el('text', { x: padL + w + 9, y: yy + barH / 2 + 4, class: 'dlabel' });
        v.textContent = fmt(d[y], kind); svg.appendChild(v);
      });
      wrap.appendChild(svg);
      return wrap;
    }

    const H = 300, padB = 46, padL = 62;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    const max = niceMax(Math.max(...data.map(d => d[y])));
    const plotH = H - padB - 14, plotW = W - padL - 18;
    const bw = Math.min(64, plotW / data.length - 12);
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 18, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = fmt(max * i / 4, kind); svg.appendChild(t);
    }
    data.forEach((d, i) => {
      const cx = padL + (plotW / data.length) * (i + 0.5);
      const h = Math.max(2, plotH * (d[y] / max));
      const yy = 14 + plotH - h;
      const r = el('path', {
        d: `M${cx - bw / 2},${14 + plotH} V${yy + 4} a4,4 0 0 1 4,-4 H${cx + bw / 2 - 4}
            a4,4 0 0 1 4,4 V${14 + plotH} Z`.replace(/\s+/g, ' '),
        fill: d.__other ? cssVar('--other') : seriesColor(i),
        stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(r, `<b>${d[x]}</b><br>${y.replace(/_/g,' ')}: ${fmt(d[y], kind)}` +
        (d.attempts != null ? `<br>attempts: ${fmt(d.attempts)}` : ''));
      svg.appendChild(r);
      const v = el('text', { x: cx, y: yy - 7, class: 'dlabel', 'text-anchor': 'middle' });
      v.textContent = fmt(d[y], kind); svg.appendChild(v);
      const lb = el('text', { x: cx, y: H - 24, class: 'tick', 'text-anchor': 'middle' });
      lb.textContent = String(d[x]).slice(0, 14); svg.appendChild(lb);
    });
    wrap.appendChild(svg);
    return wrap;
  }

  // ── line (multi-series) ──────────────────────────────────────────────────
  function line(rows, { x, y, series, kind }) {
    const wrap = document.createDocumentFragment();
    const groups = new Map();
    rows.forEach(r => {
      const k = series ? String(r[series] ?? '—') : (y || 'value');
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    });
    let keys = [...groups.keys()];
    if (keys.length > MAX_SERIES) {
      keys = keys.slice(0, MAX_SERIES);                 // fold, never cycle hues
    }
    const xs = [...new Set(rows.map(r => String(r[x])))].sort();
    const vals = rows.map(r => r[y]).filter(v => v != null);
    if (!vals.length || xs.length < 2) return null;
    const min = Math.min(...vals), max = Math.max(...vals);
    const lo = min - (max - min) * 0.12, hi = max + (max - min) * 0.12;

    const W = 760, H = 310, padL = 62, padB = 42;
    const plotW = W - padL - 18, plotH = H - padB - 14;
    const sx = (v) => padL + plotW * (xs.indexOf(String(v)) / (xs.length - 1));
    const sy = (v) => 14 + plotH * (1 - (v - lo) / (hi - lo || 1));

    if (keys.length >= 2) {
      wrap.appendChild(legend(keys.map((k, i) => [k, seriesColor(i)])));
    }
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 18, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = fmt(lo + (hi - lo) * i / 4, kind); svg.appendChild(t);
    }
    const step = Math.max(1, Math.ceil(xs.length / 7));
    xs.forEach((v, i) => {
      if (i % step && i !== xs.length - 1) return;
      const t = el('text', { x: sx(v), y: H - 20, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = String(v).slice(0, 10); svg.appendChild(t);
    });
    keys.forEach((k, i) => {
      const pts = groups.get(k).filter(r => r[y] != null)
        .sort((a, b) => String(a[x]).localeCompare(String(b[x])));
      if (pts.length < 2) return;
      const col = seriesColor(i);
      svg.appendChild(el('path', {
        d: pts.map((p, j) => `${j ? 'L' : 'M'}${sx(p[x])},${sy(p[y])}`).join(' '),
        fill: 'none', stroke: col, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      pts.forEach(p => {
        const c = el('circle', { cx: sx(p[x]), cy: sy(p[y]), r: 4.5, fill: col,
                                 stroke: cssVar('--card'), 'stroke-width': 2 });
        // hit target larger than the mark
        const hit = el('circle', { cx: sx(p[x]), cy: sy(p[y]), r: 11, fill: 'transparent' });
        bindTip(hit, `<b>${k}</b><br>${p[x]}<br>${fmt(p[y], kind)}`);
        svg.appendChild(c); svg.appendChild(hit);
      });
      if (keys.length <= 4) {                       // direct-label up to 4 series
        const last = pts[pts.length - 1];
        const t = el('text', { x: sx(last[x]) + 8, y: sy(last[y]) + 4, class: 'dlabel',
                               fill: col });
        t.textContent = k.slice(0, 12); svg.appendChild(t);
      }
    });
    wrap.appendChild(svg);
    return wrap;
  }

  // ── waterfall: baseline -> mix -> rate -> target ──────────────────────────
  function waterfall(dec, kind = 'percent') {
    const wrap = document.createDocumentFragment();
    const steps = [
      { label: 'Baseline', value: dec.baseline_value, kind: 'abs' },
      { label: 'Mix effect', value: dec.mix_pp, kind: 'delta' },
      { label: 'Rate effect', value: dec.rate_pp, kind: 'delta' },
      { label: 'Current', value: dec.target_value, kind: 'abs' },
    ];
    const W = 760, H = 320, padL = 62, padB = 54;
    const plotH = H - padB - 26, plotW = W - padL - 24;
    const all = [dec.baseline_value, dec.target_value];
    const lo = Math.min(...all) - 0.9, hi = Math.max(...all) + 0.5;
    const sy = (v) => 26 + plotH * (1 - (v - lo) / (hi - lo || 1));
    const bw = Math.min(96, plotW / steps.length - 26);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    for (let i = 0; i <= 4; i++) {
      const gy = 26 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 24, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = (lo + (hi - lo) * i / 4).toFixed(1) + '%'; svg.appendChild(t);
    }
    let running = dec.baseline_value;
    steps.forEach((s, i) => {
      const cx = padL + (plotW / steps.length) * (i + 0.5);
      let top, bot, col, label;
      if (s.kind === 'abs') {
        top = sy(s.value); bot = sy(lo); col = cssVar('--s4');
        label = s.value.toFixed(2) + '%';
        running = s.value;
      } else {
        const from = running, to = running + s.value;
        top = sy(Math.max(from, to)); bot = sy(Math.min(from, to));
        col = s.value < 0 ? cssVar('--s5') : cssVar('--s1');
        label = (s.value >= 0 ? '+' : '') + s.value.toFixed(2) + 'pp';
        running = to;
        svg.appendChild(el('line', { x1: cx - bw / 2 - 14, y1: sy(from),
          x2: cx - bw / 2, y2: sy(from), stroke: cssVar('--border'),
          'stroke-width': 1, 'stroke-dasharray': '3 3' }));
      }
      const h = Math.max(3, bot - top);
      const r = el('rect', { x: cx - bw / 2, y: top, width: bw, height: h, rx: 4,
        fill: col, stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(r, `<b>${s.label}</b><br>${label}` + (s.kind === 'delta'
        ? `<br>${s.label === 'Mix effect'
            ? 'traffic moved between segments' : 'segments changed their own rate'}` : ''));
      svg.appendChild(r);
      const v = el('text', { x: cx, y: top - 9, class: 'dlabel', 'text-anchor': 'middle' });
      v.textContent = label; svg.appendChild(v);
      const lb = el('text', { x: cx, y: H - 30, class: 'tick', 'text-anchor': 'middle' });
      lb.textContent = s.label; svg.appendChild(lb);
    });
    const note = el('text', { x: padL, y: H - 8, class: 'tick' });
    note.textContent = `${Math.abs(dec.mix_share_pct).toFixed(0)}% of the `
      + `${dec.delta_pp.toFixed(2)}pp move is traffic mix, not changed success rates`;
    svg.appendChild(note);
    wrap.appendChild(legend([['Level', cssVar('--s4')],
                             ['Negative effect', cssVar('--s5')],
                             ['Positive effect', cssVar('--s1')]]));
    wrap.appendChild(svg);
    return wrap;
  }

  // ── 100% stacked area: share over time (mix shift made visible) ──────────
  function shareArea(rows, { x, series, weightCol = 'attempts' }) {
    const xs = [...new Set(rows.map(r => String(r[x])))].sort();
    let keys = [...new Set(rows.map(r => String(r[series])))];
    if (keys.length > MAX_SERIES) keys = keys.slice(0, MAX_SERIES);
    if (xs.length < 2 || keys.length < 2) return null;
    const idx = new Map();
    rows.forEach(r => idx.set(`${r[x]}|${r[series]}`, r[weightCol] || 0));

    const W = 760, H = 300, padL = 48, padB = 40;
    const plotW = W - padL - 18, plotH = H - padB - 14;
    const sx = (i) => padL + plotW * (i / (xs.length - 1));
    const wrap = document.createDocumentFragment();
    wrap.appendChild(legend(keys.map((k, i) => [k, seriesColor(i)])));
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    const cum = xs.map(() => 0);
    keys.forEach((k, ki) => {
      const top = [], bot = [];
      xs.forEach((xv, i) => {
        const total = keys.reduce((s, kk) => s + (idx.get(`${xv}|${kk}`) || 0), 0) || 1;
        const share = (idx.get(`${xv}|${k}`) || 0) / total;
        bot.push(cum[i]); cum[i] += share; top.push(cum[i]);
      });
      const path = top.map((v, i) => `${i ? 'L' : 'M'}${sx(i)},${14 + plotH * (1 - v)}`)
        .concat(bot.map((v, i) => `L${sx(bot.length - 1 - i)},${14 + plotH * (1 - bot[bot.length - 1 - i])}`))
        .join(' ') + ' Z';
      const p = el('path', { d: path, fill: seriesColor(ki), 'fill-opacity': .92,
                             stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(p, `<b>${k}</b><br>share of attempts over time`);
      svg.appendChild(p);
    });
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      const t = el('text', { x: padL - 8, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = (i * 25) + '%'; svg.appendChild(t);
    }
    const step = Math.max(1, Math.ceil(xs.length / 7));
    xs.forEach((v, i) => {
      if (i % step && i !== xs.length - 1) return;
      const t = el('text', { x: sx(i), y: H - 18, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = String(v).slice(0, 10); svg.appendChild(t);
    });
    wrap.appendChild(svg);
    return wrap;
  }

  /* ── share shift: a dumbbell per segment ──────────────────────────────────
     The canonical form for "this moved between two states". A grouped bar would
     double the marks and make the reader diff two heights; a dumbbell shows the
     move itself as length and direction, which is the whole point when the story
     is that traffic moved rather than that rates changed. */
  function shareShift(dec) {
    const segs = [...dec.segments]
      .filter(s => Math.abs(s.weight_target - s.weight_base) > 1e-6 ||
                   s.weight_base > 0.001)
      .sort((a, b) => (b.weight_target - b.weight_base) - (a.weight_target - a.weight_base));
    if (segs.length < 2) return null;

    const wrap = document.createDocumentFragment();
    wrap.appendChild(legend([['share before', cssVar('--other')],
                             ['share after', cssVar('--s4')],
                             ['moved toward a worse rail', cssVar('--s5')]]));

    const W = 760, rowH = 38;
    const padL = Math.min(190, Math.max(...segs.map(s => s.key.length)) * 7.6 + 16);
    const H = segs.length * rowH + 34;
    const max = niceMax(Math.max(...segs.map(s => Math.max(s.weight_base, s.weight_target))));
    const plotW = W - padL - 118;
    const sx = (v) => padL + plotW * (v / max);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });

    for (let i = 0; i <= 4; i++) {
      const gx = padL + plotW * i / 4;
      svg.appendChild(el('line', { x1: gx, y1: 0, x2: gx, y2: H - 30, class: 'gridline' }));
      const t = el('text', { x: gx, y: H - 12, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = (max * i / 4 * 100).toFixed(0) + '%';
      svg.appendChild(t);
    }

    segs.forEach((s, i) => {
      const y = i * rowH + rowH / 2;
      const d = s.weight_target - s.weight_base;
      // Colour by whether the move HURT, not by the segment's identity: a rail
      // gaining share is only bad if that rail converts worse than the average.
      const worse = (d > 0 && s.value_base < dec.baseline_value / 100) ||
                    (d < 0 && s.value_base > dec.baseline_value / 100);
      const col = Math.abs(d) < 1e-4 ? cssVar('--other')
                : worse ? cssVar('--s5') : cssVar('--s1');

      const lab = el('text', { x: padL - 11, y: y + 4, class: 'tick', 'text-anchor': 'end' });
      lab.textContent = s.key.slice(0, 22);
      svg.appendChild(lab);

      svg.appendChild(el('line', { x1: sx(s.weight_base), y1: y, x2: sx(s.weight_target), y2: y,
                                   stroke: col, 'stroke-width': 3, 'stroke-linecap': 'round' }));
      svg.appendChild(el('circle', { cx: sx(s.weight_base), cy: y, r: 5,
                                     fill: cssVar('--card'), stroke: cssVar('--other'),
                                     'stroke-width': 2 }));
      svg.appendChild(el('circle', { cx: sx(s.weight_target), cy: y, r: 5.5, fill: col,
                                     stroke: cssVar('--card'), 'stroke-width': 2 }));

      const hit = el('rect', { x: padL, y: y - 14, width: plotW, height: 28,
                               fill: 'transparent' });
      bindTip(hit, `<b>${s.key}</b><br>share ${(s.weight_base * 100).toFixed(2)}% → ` +
        `${(s.weight_target * 100).toFixed(2)}%<br>own success rate ` +
        `${(s.value_base * 100).toFixed(2)}% → ${(s.value_target * 100).toFixed(2)}%` +
        `<br>mix effect ${s.mix_pp >= 0 ? '+' : ''}${s.mix_pp.toFixed(2)}pp`);
      svg.appendChild(hit);

      const v = el('text', { x: W - 108, y: y + 4, class: 'dlabel', fill: col });
      // Rounding to 2dp turns a genuinely unchanged share into "-0.00pp", which
      // reads as a tiny loss rather than as nothing happening.
      v.textContent = Math.abs(d) < 5e-5 ? 'unchanged'
        : `${d >= 0 ? '+' : ''}${(d * 100).toFixed(2)}pp share`;
      if (Math.abs(d) < 5e-5) v.setAttribute('fill', cssVar('--ink-3'));
      svg.appendChild(v);
    });
    wrap.appendChild(svg);
    return wrap;
  }

  /* ── per-segment mix vs rate, diverging from zero ─────────────────────────
     Answers "which segment did this, and through which mechanism" in one read. */
  function segmentEffects(dec) {
    const segs = [...dec.segments]
      .filter(s => Math.abs(s.mix_pp) > 1e-3 || Math.abs(s.rate_pp) > 1e-3)
      .sort((a, b) => Math.abs(b.total_pp) - Math.abs(a.total_pp));
    if (!segs.length) return null;

    const wrap = document.createDocumentFragment();
    wrap.appendChild(legend([['mix effect (traffic moved)', cssVar('--s2')],
                             ['rate effect (segment changed)', cssVar('--s3')]]));

    const W = 760, rowH = 46, barH = 15;
    const padL = Math.min(190, Math.max(...segs.map(s => s.key.length)) * 7.6 + 16);
    const H = segs.length * rowH + 34;
    const mx = Math.max(...segs.flatMap(s => [Math.abs(s.mix_pp), Math.abs(s.rate_pp)]), 0.1);
    const max = niceMax(mx);
    const plotW = W - padL - 96;
    const zero = padL + plotW / 2;
    const sx = (v) => zero + (plotW / 2) * (v / max);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });

    for (let i = -2; i <= 2; i++) {
      const gx = zero + (plotW / 2) * (i / 2);
      svg.appendChild(el('line', { x1: gx, y1: 0, x2: gx, y2: H - 30,
                                   class: i === 0 ? 'axis' : 'gridline' }));
      const t = el('text', { x: gx, y: H - 12, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = (max * i / 2).toFixed(1) + 'pp';
      svg.appendChild(t);
    }

    segs.forEach((s, i) => {
      const top = i * rowH + 6;
      const lab = el('text', { x: padL - 11, y: top + rowH / 2 - 2, class: 'tick',
                               'text-anchor': 'end' });
      lab.textContent = s.key.slice(0, 22);
      svg.appendChild(lab);

      [['mix', s.mix_pp, cssVar('--s2'), top],
       ['rate', s.rate_pp, cssVar('--s3'), top + barH + 2]].forEach(([name, v, col, y]) => {
        const x = Math.min(zero, sx(v));
        const w = Math.max(2, Math.abs(sx(v) - zero));
        const r = el('rect', { x, y, width: w, height: barH, rx: 3, fill: col,
                               stroke: cssVar('--card'), 'stroke-width': 2 });
        bindTip(r, `<b>${s.key}</b> — ${name} effect<br>` +
          `${v >= 0 ? '+' : ''}${v.toFixed(3)}pp`);
        svg.appendChild(r);
      });
      const tot = el('text', { x: W - 86, y: top + rowH / 2 - 1, class: 'dlabel',
                               fill: s.total_pp < 0 ? cssVar('--s5') : cssVar('--s1') });
      tot.textContent = `${s.total_pp >= 0 ? '+' : ''}${s.total_pp.toFixed(2)}pp`;
      svg.appendChild(tot);
    });
    wrap.appendChild(svg);

    // If one mechanism is ~0 its bars are sub-pixel and the legend promises a
    // series the reader cannot find. Saying so IS the finding, so state it.
    const maxRate = Math.max(...segs.map(s => Math.abs(s.rate_pp)));
    const maxMix = Math.max(...segs.map(s => Math.abs(s.mix_pp)));
    if (maxRate < max * 0.04 || maxMix < max * 0.04) {
      const quiet = maxRate < maxMix ? 'rate' : 'mix';
      const val = quiet === 'rate' ? maxRate : maxMix;
      const n = document.createElement('p');
      n.style.cssText = 'color:var(--ink-3);font-size:12.5px;margin:8px 0 0';
      n.innerHTML = `The <b>${quiet} effect</b> bars are too small to see: the largest `
        + `is ${val.toFixed(3)}pp. That is the result, not a rendering fault — `
        + (quiet === 'rate'
            ? 'no rail meaningfully changed its own success rate.'
            : 'traffic composition barely moved.');
      wrap.appendChild(n);
    }
    return wrap;
  }

  function kpi(value, kind, sub) {
    const d = document.createElement('div');
    d.innerHTML = `<div class="kpi">${fmt(value, kind)}</div>
                   <div class="kpi-sub">${sub || ''}</div>`;
    return d;
  }


  /* ═══════════════════════════════════════════════════════════════════════
     KPI Lab renderers
     ═══════════════════════════════════════════════════════════════════════ */

  /* Sequential ramp for the magnitude charts (heatmap, matrix). ONE hue,
     light to dark — never a rainbow, and never the categorical series colours,
     because those encode identity and these encode quantity. Interpolated in
     sRGB between two theme tokens so it re-steps correctly in dark mode instead
     of being an inverted light ramp. */
  function seqColor(tFrac) {
    const t = Math.max(0, Math.min(1, tFrac || 0));
    const lo = cssVar('--seq-lo'), hi = cssVar('--seq-hi');
    const h2r = (h) => {
      const v = h.replace('#', '');
      return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16),
              parseInt(v.slice(4, 6), 16)];
    };
    const a = h2r(lo), b = h2r(hi);
    const c = a.map((x, i) => Math.round(x + (b[i] - x) * t));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }
  /* Readable ink on top of a ramp step: the dark end of any sequential ramp
     cannot carry dark text. */
  const seqInk = (t) => (t > 0.58 ? cssVar('--on-seq') : cssVar('--ink'));

  /* Failures by journey step.
     Deliberately a BAR and not a funnel. A funnel's width means "how many of the
     original cohort are still here", and that is not what this is: a payment that
     dies at step 4 never arrives at step 8, so these bars are counts of where
     attempts died, not survivors passing through. Drawing it as a funnel would
     invite reading step 8's bar as a share of step 7's, which is false. */
  function journeySteps(rows, { x = 'step_label', y = 'failures' } = {}) {
    const wrap = document.createElement('div');
    wrap.className = 'jsteps';
    const max = Math.max(...rows.map(r => r[y] || 0), 1);
    rows.forEach(r => {
      const pct = (r[y] || 0) / max;
      const row = document.createElement('div');
      row.className = 'jstep';
      row.innerHTML = `
        <span class="jstep-n">${r.step}</span>
        <span class="jstep-l">${r[x]}</span>
        <span class="jstep-bar"><i style="width:${(pct * 100).toFixed(1)}%"></i></span>
        <span class="jstep-v">${fmt(r[y])}</span>
        <span class="jstep-s">${(r.share_pct || 0).toFixed(1)}%</span>`;
      bindTip(row, `<b>Step ${r.step} — ${r[x]}</b><br>${fmt(r[y])} failures
        &middot; ${(r.share_pct || 0).toFixed(1)}% of all failures<br>
        <span class="t-dim">${r.codes} distinct failure code${r.codes === 1 ? '' : 's'}
        at this step</span>`);
      wrap.appendChild(row);
    });
    return wrap;
  }

  /* Donut. A bar compares shares better, but this one is asked for by name and
     the question ("who owns it") is about composition of a whole, which is the
     one job a donut is actually for. Every slice is directly labelled, so colour
     is never the only channel. */
  function donut(rows, { x = 'owner', y = 'failures' } = {}) {
    const W = 300, H = 230, R = 82, r0 = 50, cx = 108, cy = H / 2;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart donut' });
    const total = rows.reduce((s, d) => s + (d[y] || 0), 0) || 1;
    const sorted = [...rows].sort((a, b) => (b[y] || 0) - (a[y] || 0));
    let ang = -Math.PI / 2;
    const items = [];
    sorted.forEach((d, i) => {
      const frac = (d[y] || 0) / total;
      const sweep = frac * Math.PI * 2;
      // 2px surface gap between adjacent fills, as a small angular inset.
      const gap = Math.min(0.018, sweep / 4);
      const a0 = ang + gap / 2, a1 = ang + sweep - gap / 2;
      const col = i < MAX_SERIES ? seriesColor(i) : cssVar('--other');
      const pt = (a, rad) => `${cx + Math.cos(a) * rad} ${cy + Math.sin(a) * rad}`;
      const big = (a1 - a0) > Math.PI ? 1 : 0;
      const path = el('path', {
        d: `M ${pt(a0, R)} A ${R} ${R} 0 ${big} 1 ${pt(a1, R)}
            L ${pt(a1, r0)} A ${r0} ${r0} 0 ${big} 0 ${pt(a0, r0)} Z`,
        fill: col, stroke: cssVar('--card'), 'stroke-width': 2,
      });
      bindTip(path, `<b>${d[x]}</b><br>${fmt(d[y])} failures &middot;
        ${(frac * 100).toFixed(1)}%<br><span class="t-dim">${fmt(d.technical || 0)}
        of them technical</span>`);
      svg.appendChild(path);
      items.push([`${d[x]} · ${(frac * 100).toFixed(0)}%`, col]);
      ang += sweep;
    });
    const t1 = el('text', { x: cx, y: cy - 4, 'text-anchor': 'middle',
                            class: 'donut-c1' });
    t1.textContent = fmt(total);
    const t2 = el('text', { x: cx, y: cy + 14, 'text-anchor': 'middle',
                            class: 'donut-c2' });
    t2.textContent = 'failures';
    svg.appendChild(t1); svg.appendChild(t2);
    const box = document.createElement('div');
    box.className = 'donut-wrap';
    box.appendChild(svg);
    box.appendChild(legend(items));
    return box;
  }

  /* Two rates with the SAME denominator, stacked. That is the only situation in
     which stacking rates is arithmetically legal: business + technical declines
     over resolved attempts add up to the total decline rate. If the denominators
     differed the stack height would be meaningless. */
  function stackedArea(rows, { x = 'day', series = [] }) {
    const W = 860, H = 260, m = { t: 14, r: 16, b: 30, l: 48 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart' });
    const keys = Array.isArray(series) ? series : String(series).split(',');
    const tot = rows.map(r => keys.reduce((s, k) => s + (r[k] || 0), 0));
    const yMax = niceMax(Math.max(...tot, 0.001));
    const X = (i) => m.l + (rows.length < 2 ? iw / 2 : (i / (rows.length - 1)) * iw);
    const Y = (v) => m.t + ih - (v / yMax) * ih;

    [0, .25, .5, .75, 1].forEach(f => {
      const yy = m.t + ih - f * ih;
      svg.appendChild(el('line', { x1: m.l, x2: m.l + iw, y1: yy, y2: yy,
                                   stroke: cssVar('--grid'), 'stroke-width': 1 }));
      const tx = el('text', { x: m.l - 8, y: yy + 4, 'text-anchor': 'end',
                              class: 'ax' });
      tx.textContent = (yMax * f * 100).toFixed(1) + '%';
      svg.appendChild(tx);
    });

    let base = rows.map(() => 0);
    keys.forEach((k, ki) => {
      const top = rows.map((r, i) => base[i] + (r[k] || 0));
      const up = top.map((v, i) => `${X(i)} ${Y(v)}`).join(' L ');
      const dn = base.map((v, i) => `${X(i)} ${Y(v)}`).reverse().join(' L ');
      svg.appendChild(el('path', {
        d: `M ${up} L ${dn} Z`, fill: seriesColor(ki), 'fill-opacity': 0.72,
        stroke: cssVar('--card'), 'stroke-width': 2 }));
      base = top;
    });

    // Crosshair + one tooltip per day, with a hit target far wider than the mark.
    const cross = el('line', { y1: m.t, y2: m.t + ih, stroke: cssVar('--ink-3'),
                               'stroke-width': 1, 'stroke-dasharray': '3 3',
                               opacity: 0 });
    svg.appendChild(cross);
    rows.forEach((r, i) => {
      const bw = Math.max(8, iw / rows.length);
      const hit = el('rect', { x: X(i) - bw / 2, y: m.t, width: bw, height: ih,
                               fill: 'transparent' });
      const lines = keys.map((k, ki) =>
        `<span style="color:${seriesColor(ki)}">&#9632;</span>
         ${k.replace(/_/g, ' ')}: <b>${fmt(r[k], 'percent')}</b>`).join('<br>');
      bindTip(hit, `<b>${r[x]}</b><br>${lines}<br>
        <span class="t-dim">${fmt(r.resolved)} resolved attempts</span>`);
      hit.addEventListener('pointerenter', () => {
        cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i));
        cross.setAttribute('opacity', 1);
      });
      hit.addEventListener('pointerleave', () => cross.setAttribute('opacity', 0));
      svg.appendChild(hit);
    });

    const step = Math.ceil(rows.length / 7);
    rows.forEach((r, i) => {
      if (i % step && i !== rows.length - 1) return;
      const tx = el('text', { x: X(i), y: H - 9, 'text-anchor': 'middle', class: 'ax' });
      tx.textContent = String(r[x]).slice(5);
      svg.appendChild(tx);
    });

    const box = document.createElement('div');
    box.appendChild(svg);
    box.appendChild(legend(keys.map((k, i) =>
      [k.replace(/_/g, ' ').replace(' rate', ''), seriesColor(i)])));
    return box;
  }

  /* Grouped bars: one group per primary category, one bar per series member.
     Series colours are assigned from a FIXED order supplied by the caller, so
     filtering the chart down to three entry modes does not repaint them. */
  function groupedBar(rows, { x, series, y, kind = 'percent', order = null }) {
    const groups = [...new Set(rows.map(r => r[x]))];
    const members = order || [...new Set(rows.map(r => r[series]))].sort();
    const W = 860, H = 280, m = { t: 14, r: 16, b: 44, l: 52 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart' });
    const vals = rows.map(r => r[y] || 0);
    const yMax = niceMax(Math.max(...vals, 0.001));
    const gw = iw / groups.length;
    const bw = Math.max(4, (gw - 16) / members.length - 2);

    [0, .25, .5, .75, 1].forEach(f => {
      const yy = m.t + ih - f * ih;
      svg.appendChild(el('line', { x1: m.l, x2: m.l + iw, y1: yy, y2: yy,
                                   stroke: cssVar('--grid'), 'stroke-width': 1 }));
      const tx = el('text', { x: m.l - 8, y: yy + 4, 'text-anchor': 'end', class: 'ax' });
      tx.textContent = kind === 'percent' ? (yMax * f * 100).toFixed(0) + '%'
                                          : fmt(yMax * f);
      svg.appendChild(tx);
    });

    groups.forEach((g, gi) => {
      members.forEach((mem, mi) => {
        const row = rows.find(r => r[x] === g && r[series] === mem);
        if (!row || row[y] == null) return;
        const h = Math.max(1, (row[y] / yMax) * ih);
        const bx = m.l + gi * gw + 8 + mi * (bw + 2);
        const rect = el('rect', { x: bx, y: m.t + ih - h, width: bw, height: h,
                                  rx: 4, fill: seriesColor(mi) });
        bindTip(rect, `<b>${g} &middot; ${mem}</b><br>
          ${y.replace(/_/g, ' ')}: <b>${fmt(row[y], kind)}</b><br>
          <span class="t-dim">${fmt(row.resolved)} resolved attempts</span>`);
        svg.appendChild(rect);
      });
      const tx = el('text', { x: m.l + gi * gw + gw / 2, y: H - 22,
                              'text-anchor': 'middle', class: 'ax-k' });
      tx.textContent = g;
      svg.appendChild(tx);
    });

    const box = document.createElement('div');
    box.appendChild(svg);
    box.appendChild(legend(members.map((mem, i) => [mem, seriesColor(i)])));
    return box;
  }

  /* Hour x weekday magnitude grid. Sequential single hue; the value is also
     printed in each cell on hover, and a scale strip states what the shading
     means so the colour is never the only way to read it. */
  function heatmap(rows, { x = 'hour', series = 'weekday', y = 'failure_rate' } = {}) {
    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const W = 860, H = 232, m = { t: 16, r: 14, b: 26, l: 44 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const cw = iw / 24, chh = ih / 7;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart' });
    const vals = rows.map(r => r[y]).filter(v => v != null);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = (hi - lo) || 1;

    rows.forEach(r => {
      const d = r[series], h = r[x];
      if (d == null || h == null || r[y] == null) return;
      const t = (r[y] - lo) / span;
      const cell = el('rect', {
        x: m.l + h * cw + 1, y: m.t + d * chh + 1,
        width: cw - 2, height: chh - 2, rx: 3, fill: seqColor(t) });
      bindTip(cell, `<b>${DAYS[d]} ${String(h).padStart(2, '0')}:00</b><br>
        failure rate <b>${fmt(r[y], 'percent')}</b><br>
        technical ${fmt(r.technical_decline_rate, 'percent')}<br>
        <span class="t-dim">${fmt(r.resolved)} resolved attempts</span>`);
      svg.appendChild(cell);
    });
    DAYS.forEach((d, i) => {
      const tx = el('text', { x: m.l - 8, y: m.t + i * chh + chh / 2 + 4,
                              'text-anchor': 'end', class: 'ax' });
      tx.textContent = d;
      svg.appendChild(tx);
    });
    for (let h = 0; h < 24; h += 3) {
      const tx = el('text', { x: m.l + h * cw + cw / 2, y: H - 8,
                              'text-anchor': 'middle', class: 'ax' });
      tx.textContent = String(h).padStart(2, '0');
      svg.appendChild(tx);
    }
    const box = document.createElement('div');
    box.appendChild(svg);
    const strip = document.createElement('div');
    strip.className = 'seq-scale';
    strip.innerHTML = `<span>${fmt(lo, 'percent')}</span>` +
      [0, .2, .4, .6, .8, 1].map(t => `<i style="background:${seqColor(t)}"></i>`).join('') +
      `<span>${fmt(hi, 'percent')}</span><em>failure rate</em>`;
    box.appendChild(strip);
    return box;
  }

  /* Small labelled grid (connectivity x health). Values printed, not just shaded. */
  function matrix(rows, { x = 'connectivity', series = 'health',
                          y = 'failure_rate' } = {}) {
    const cols = [...new Set(rows.map(r => r[x]))];
    const ks = [...new Set(rows.map(r => r[series]))];
    const vals = rows.map(r => r[y]).filter(v => v != null);
    const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
    const t = document.createElement('table');
    t.className = 'matrix';
    t.innerHTML = `<thead><tr><th></th>${cols.map(c =>
      `<th>${c}</th>`).join('')}</tr></thead>`;
    const tb = document.createElement('tbody');
    ks.forEach(k => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<th>${k}</th>`;
      cols.forEach(c => {
        const r = rows.find(z => z[x] === c && z[series] === k);
        const td = document.createElement('td');
        if (!r || r[y] == null) { td.innerHTML = '<span class="t-dim">—</span>'; }
        else {
          const f = (r[y] - lo) / span;
          td.style.background = seqColor(f);
          td.style.color = seqInk(f);
          td.innerHTML = `<b>${fmt(r[y], 'percent')}</b>
            <small>${fmt(r.terminals)} terminals</small>`;
          bindTip(td, `<b>${c} &middot; ${k}</b><br>
            failure rate <b>${fmt(r[y], 'percent')}</b><br>
            technical ${fmt(r.technical_decline_rate, 'percent')}<br>
            <span class="t-dim">${fmt(r.terminals)} terminals,
            ${fmt(r.resolved)} resolved attempts</span>`);
        }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    const box = document.createElement('div');
    box.appendChild(t);
    return box;
  }

  global.PLCharts = { bar, line, waterfall, shareArea, shareShift, segmentEffects,
                    kpi, fmt, legend, seriesColor,
                    journeySteps, donut, stackedArea, groupedBar, heatmap, matrix,
                    seqColor, bindTip };
})(window);
