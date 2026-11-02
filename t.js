
  var __wrap = document.getElementById('wrap');
  var __banner = document.getElementById('errbanner');
  window.__seq = 0;
  window.__lastBg = '#ffffff';
  window.__hasDiagram = false;
  var NS = 'http://www.w3.org/2000/svg';
  var view = { zoom: 1, x: 0, y: 0, fit: true };

  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }

  // ---- Qt bridge -------------------------------------------------------
  function bridge_call(name) {
    var b = window.bridge;
    if (b && typeof b[name] === 'function') {
      b[name].apply(b, Array.prototype.slice.call(arguments, 1));
    }
  }
  var __chanReady = false, __winLoaded = false, __readySent = false;
  function maybe_ready() {
    if (__chanReady && __winLoaded && !__readySent) {
      __readySent = true;
      bridge_call('ready');
    }
  }
  window.addEventListener('load', function () { __winLoaded = true; maybe_ready(); });
  if (typeof QWebChannel !== 'undefined' && window.qt && qt.webChannelTransport) {
    new QWebChannel(qt.webChannelTransport, function (ch) {
      window.bridge = ch.objects.bridge;
      __chanReady = true;
      maybe_ready();
    });
  }

  // ---- pan / zoom ------------------------------------------------------
  function apply_view() {
    __wrap.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.zoom + ')';
  }
  function report_view(user) {
    bridge_call('viewState', view.zoom, view.x, view.y, view.fit, !!user);
  }
  function content_size() {
    return { w: __wrap.offsetWidth, h: __wrap.offsetHeight };
  }
  function fit_view(user) {
    var c = content_size();
    if (!c.w || !c.h) { apply_view(); return; }
    var vw = window.innerWidth, vh = window.innerHeight, pad = 24;
    var z = Math.min((vw - pad * 2) / c.w, (vh - pad * 2) / c.h, 2);
    z = Math.max(z, 0.05);
    view.zoom = z;
    view.x = (vw - c.w * z) / 2;
    view.y = (vh - c.h * z) / 2;
    view.fit = true;
    apply_view();
    report_view(user);
  }
  function actual_size(user) {
    var c = content_size();
    view.zoom = 1;
    view.x = (window.innerWidth - c.w) / 2;
    view.y = (window.innerHeight - c.h) / 2;
    view.fit = false;
    apply_view();
    report_view(user);
  }
  function zoom_at(factor, cx, cy) {
    var nz = Math.max(0.05, Math.min(8, view.zoom * factor));
    var k = nz / view.zoom;
    view.x = cx - (cx - view.x) * k;
    view.y = cy - (cy - view.y) * k;
    view.zoom = nz;
    view.fit = false;
    apply_view();
    report_view(true);
  }
  function zoom_by(factor) { zoom_at(factor, window.innerWidth / 2, window.innerHeight / 2); }
  function set_view(z, x, y, fit) {
    view = { zoom: z, x: x, y: y, fit: !!fit };
    if (view.fit) fit_view(false); else apply_view();
  }

  var drag = null;
  document.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
  });
  window.addEventListener('mousemove', function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    drag.moved = true;
    document.body.classList.add('dragging');
    view.x = drag.vx + dx;
    view.y = drag.vy + dy;
    view.fit = false;
    apply_view();
  });
  window.addEventListener('mouseup', function () {
    if (drag && drag.moved) { report_view(true); window.__dragEnd = Date.now(); }
    drag = null;
    document.body.classList.remove('dragging');
  });
  document.addEventListener('wheel', function (e) {
    e.preventDefault();
    if (e.ctrlKey) {
      zoom_at(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
    } else {
      view.x -= e.deltaX;
      view.y -= e.deltaY;
      view.fit = false;
      apply_view();
      report_view(true);
    }
  }, { passive: false });
  document.addEventListener('dblclick', function (e) {
    if (e.target.closest && e.target.closest('#proppanel')) return;
    fit_view(true);
  });

  // ---- element selection & property editing ----------------------------
  // Overrides are keyed by the element's child-index path inside the SVG so
  // they survive re-renders as long as the diagram structure is unchanged.
  var __overrides = {};
  var __selEl = null, __selKey = null;
  var SHAPES = 'rect,path,polygon,circle,ellipse,line,polyline';
  var PICK = 'g.node, g.cluster, g.edgeLabel, g.actor, g.label, g.classGroup, '
           + '.actor, .messageText, .messageLine0, .messageLine1, '
           + '.flowchart-link, .edgePath, g.slice, .task, .section';

  function cur_svg() { return __wrap.querySelector('svg'); }
  function key_of(el) {
    var s = cur_svg(), parts = [];
    while (el && el !== s) {
      var p = el.parentNode;
      parts.unshift(Array.prototype.indexOf.call(p.children, el));
      el = p;
    }
    return parts.join('/');
  }
  function el_of(key) {
    var el = cur_svg();
    if (!el) return null;
    if (key === '') return null;
    var parts = key.split('/');
    for (var i = 0; i < parts.length; i++) {
      el = el.children[parseInt(parts[i], 10)];
      if (!el) return null;
    }
    return el;
  }
  function shapes_of(el) {
    var list = el.matches(SHAPES) ? [el] : Array.prototype.slice.call(el.querySelectorAll(SHAPES));
    return list.filter(function (x) { return !x.closest('foreignObject, defs, marker'); });
  }
  function texts_of(el) {
    var tag = el.tagName.toLowerCase();
    if (tag === 'text' || tag === 'tspan') return { svg: [el], html: [] };
    return {
      svg: Array.prototype.slice.call(el.querySelectorAll('text, tspan')),
      html: Array.prototype.slice.call(el.querySelectorAll('foreignObject span, foreignObject p, foreignObject div'))
    };
  }
  function to_hex(c) {
    var m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(c || '');
    if (!m) return null;
    return '#' + [m[1], m[2], m[3]].map(function (n) {
      return ('0' + parseInt(n, 10).toString(16)).slice(-2);
    }).join('');
  }
  function apply_props(el, p) {
    var sh = shapes_of(el), tx = texts_of(el);
    sh.forEach(function (x) {
      if (p.fill && getComputedStyle(x).fill !== 'none') x.style.setProperty('fill', p.fill, 'important');
      if (p.stroke) x.style.setProperty('stroke', p.stroke, 'important');
      if (p.strokeWidth) x.style.setProperty('stroke-width', p.strokeWidth + 'px', 'important');
      if (p.opacity !== undefined && p.opacity !== '') x.style.setProperty('opacity', p.opacity, 'important');
    });
    tx.svg.forEach(function (x) {
      if (p.textColor) x.style.setProperty('fill', p.textColor, 'important');
      if (p.fontSize) x.style.setProperty('font-size', p.fontSize + 'px', 'important');
    });
    tx.html.forEach(function (x) {
      if (p.textColor) x.style.setProperty('color', p.textColor, 'important');
      if (p.fontSize) x.style.setProperty('font-size', p.fontSize + 'px', 'important');
    });
  }
  function clear_props(el) {
    var all = [el].concat(Array.prototype.slice.call(el.querySelectorAll('*')));
    all.forEach(function (x) {
      ['fill', 'stroke', 'stroke-width', 'opacity', 'font-size', 'color'].forEach(function (k) {
        if (x.style) x.style.removeProperty(k);
      });
    });
  }
  function reapply_overrides() {
    Object.keys(__overrides).forEach(function (k) {
      var el = el_of(k);
      if (el) apply_props(el, __overrides[k]);
    });
    if (__selKey !== null) {
      var e2 = el_of(__selKey);
      if (e2) { __selEl = e2; e2.classList.add('mm-sel'); } else deselect();
    }
  }
  function strip_selection(root) {
    root.querySelectorAll('.mm-sel').forEach(function (x) { x.classList.remove('mm-sel'); });
  }

  var __panel = document.getElementById('proppanel');
  function pf(id) { return document.getElementById(id); }
  function deselect() {
    if (__selEl) __selEl.classList.remove('mm-sel');
    __selEl = null; __selKey = null;
    __panel.style.display = 'none';
  }
  function select_el(el) {
    if (__selEl) __selEl.classList.remove('mm-sel');
    __selEl = el;
    __selKey = key_of(el);
    el.classList.add('mm-sel');
    var sh = shapes_of(el), tx = texts_of(el);
    var s0 = sh[0] ? getComputedStyle(sh[0]) : null;
    var t0 = tx.svg[0] ? getComputedStyle(tx.svg[0]).fill
           : (tx.html[0] ? getComputedStyle(tx.html[0]).color : null);
    var fs = tx.svg[0] ? getComputedStyle(tx.svg[0]).fontSize
           : (tx.html[0] ? getComputedStyle(tx.html[0]).fontSize : '');
    pf('pp_title').textContent = (el.id ? el.id : el.tagName.toLowerCase()
        + (el.getAttribute('class') ? '.' + el.getAttribute('class').split(/\s+/)[0] : ''));
    pf('pp_fill').value = (s0 && to_hex(s0.fill)) || '#ffffff';
    pf('pp_stroke').value = (s0 && to_hex(s0.stroke)) || '#000000';
    pf('pp_sw').value = s0 ? (parseFloat(s0.strokeWidth) || 0) : 1;
    pf('pp_text').value = to_hex(t0) || '#000000';
    pf('pp_fs').value = parseFloat(fs) || 14;
    pf('pp_op').value = s0 ? (parseFloat(s0.opacity) || 1) : 1;
    __panel.style.display = 'block';
  }
  function panel_changed(prop, val) {
    if (!__selEl) return;
    var o = __overrides[__selKey] || (__overrides[__selKey] = {});
    o[prop] = val;
    apply_props(__selEl, o);
  }
  [['pp_fill', 'fill'], ['pp_stroke', 'stroke'], ['pp_sw', 'strokeWidth'],
   ['pp_text', 'textColor'], ['pp_fs', 'fontSize'], ['pp_op', 'opacity']].forEach(function (p) {
    pf(p[0]).addEventListener('input', function () { panel_changed(p[1], this.value); });
  });
  pf('pp_reset').addEventListener('click', function () {
    if (!__selEl) return;
    delete __overrides[__selKey];
    clear_props(__selEl);
    select_el(__selEl);
  });
  pf('pp_resetall').addEventListener('click', function () {
    __overrides = {};
    var s = cur_svg();
    if (s) s.querySelectorAll('[style]').forEach(function (x) {
      ['fill', 'stroke', 'stroke-width', 'opacity', 'font-size', 'color'].forEach(function (k) {
        x.style.removeProperty(k);
      });
    });
    if (__selEl) select_el(__selEl);
  });
  pf('pp_close').addEventListener('click', deselect);
  ['mousedown', 'mouseup', 'wheel', 'click', 'keydown'].forEach(function (ev) {
    __panel.addEventListener(ev, function (e) { e.stopPropagation(); }, { passive: true });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') deselect();
  });
  document.addEventListener('click', function (e) {
    if (Date.now() - (window.__dragEnd || 0) < 80) return;
    var s = cur_svg(), t = e.target;
    if (!s || !(t instanceof Element) || t === s || !s.contains(t) || t.closest('defs, style, marker')) {
      deselect();
      return;
    }
    var el = t.closest(PICK);
    select_el(el && s.contains(el) ? el : t);
  });
  window.addEventListener('resize', function () { if (view.fit) fit_view(false); });
  __banner.addEventListener('click', function () { __banner.style.display = 'none'; });

  // ---- errors ----------------------------------------------------------
  function show_error(msg) {
    window.__seq++;
    window.__hasDiagram = false;
    deselect();
    __wrap.innerHTML = '<div class="errbox">Mermaid error<br>' + esc(msg) + '</div>';
    __banner.style.display = 'none';
    view.fit = true;
    fit_view(false);
  }
  function show_banner(msg) {
    __banner.textContent = msg;
    __banner.style.display = 'block';
  }
  function drop_stray(id) {
    // Mermaid leaves its own error SVG behind in <body> after a failed render.
    [id, 'd' + id].forEach(function (x) {
      var el = document.getElementById(x);
      if (el && !__wrap.contains(el)) el.remove();
    });
  }
  function normalise_svg() {
    var s = __wrap.querySelector('svg');
    if (!s) return;
    var vb = s.viewBox && s.viewBox.baseVal;
    var r = s.getBoundingClientRect();
    var w = (vb && vb.width) || r.width;
    var h = (vb && vb.height) || r.height;
    s.setAttribute('width', w);
    s.setAttribute('height', h);
    s.style.maxWidth = 'none';
    s.style.overflow = 'visible';
  }

  async function mermaid_render(code, opts) {
    const s = ++window.__seq;
    const id = 'mm' + s;
    try {
      let text = String(code).replace(/^\uFEFF/, '');
      text = text.trim().length === 0 ? '%% empty diagram' : text;
      const bg = (opts && opts.themeVariables && opts.themeVariables.background) || '#ffffff';
      window.__lastBg = bg;
      document.body.style.background = bg;
      const key = JSON.stringify(opts || {});
      if (key !== window.__optsKey) {
        mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', ...opts });
        window.__optsKey = key;
      }
      const { svg, bindFunctions } = await mermaid.render(id, text);
      if (window.__seq !== s) return;          // stale response
      __wrap.innerHTML = svg;
      drop_stray(id);
      __banner.style.display = 'none';
      window.__hasDiagram = true;
      normalise_svg();
      reapply_overrides();
      if (typeof bindFunctions === 'function') {
        try { bindFunctions(__wrap); } catch (e) {}
      }
      if (view.fit) fit_view(false); else apply_view();
      bridge_call('renderDone', s, true, '', 0);
    } catch (err) {
      drop_stray(id);
      if (window.__seq !== s) return;
      const msg = err && err.message ? err.message : String(err);
      const m = /line\s+(\d+)/i.exec(msg);
      const line = m ? parseInt(m[1], 10) : 0;
      if (window.__hasDiagram) show_banner(msg); else show_error(msg);
      bridge_call('renderDone', s, false, msg, line);
    }
  }

  // Browser-rendered PNG (matches the preview exactly). The result goes back
  // through the bridge because runJavaScript does not await promises.
  function export_png(token, scale, transparent, maxpx) {
    var s = document.querySelector('#wrap svg');
    if (!s) { bridge_call('pngDone', token, 'ERR:no diagram'); return; }
    var vb = s.viewBox && s.viewBox.baseVal;
    var w = (vb && vb.width) || parseFloat(s.getAttribute('width')) || 0;
    var h = (vb && vb.height) || parseFloat(s.getAttribute('height')) || 0;
    if (!w || !h) { bridge_call('pngDone', token, 'ERR:empty diagram'); return; }
    if (maxpx) scale = Math.min(scale, maxpx / Math.max(w, h));
    var clone = s.cloneNode(true);
    strip_selection(clone);
    clone.setAttribute('xmlns', NS);
    clone.setAttribute('width', w);
    clone.setAttribute('height', h);
    clone.style.maxWidth = '';
    var xml = new XMLSerializer().serializeToString(clone);
    var img = new Image();
    img.onload = function () {
      try {
        var c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(w * scale));
        c.height = Math.max(1, Math.round(h * scale));
        var ctx = c.getContext('2d');
        if (!transparent) {
          ctx.fillStyle = window.__lastBg || '#ffffff';
          ctx.fillRect(0, 0, c.width, c.height);
        }
        ctx.drawImage(img, 0, 0, c.width, c.height);
        bridge_call('pngDone', token, c.toDataURL('image/png'));
      } catch (e) {
        bridge_call('pngDone', token, 'ERR:' + e.message);
      }
    };
    img.onerror = function () { bridge_call('pngDone', token, 'ERR:image load failed'); };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
  }

  // Export-ready SVG: bake computed fill/stroke into attributes (mermaid's
  // CSS uses var(--c-*) which QSvgRenderer cannot resolve), drop <style>
  // blocks, and ensure a background rect.
  function export_svg() {
    var s = document.querySelector('#wrap svg');
    if (!s) return '';
    var clone = s.cloneNode(true);
    strip_selection(clone);
    var live = s.querySelectorAll('*');
    var cl = clone.querySelectorAll('*');
    var NS = 'http://www.w3.org/2000/svg';
    var foPairs = [];
    for (var i = 0; i < live.length; i++) {
      var cs = getComputedStyle(live[i]);
      var e = cl[i];
      var tag = live[i].tagName.toLowerCase();
      if (tag === 'foreignobject') foPairs.push([live[i], e]);
      if (tag === 'text' || tag === 'tspan') {
        e.setAttribute('font-family', cs.fontFamily.replace(/"/g, "'"));
        e.setAttribute('font-size', cs.fontSize);
        e.setAttribute('font-weight', cs.fontWeight);
        if (cs.fontStyle && cs.fontStyle !== 'normal') e.setAttribute('font-style', cs.fontStyle);
        if (cs.textAnchor && cs.textAnchor !== 'start') e.setAttribute('text-anchor', cs.textAnchor);
        // QSvgRenderer mishandles em units on x/y/dy.
        ['x', 'y', 'dx', 'dy'].forEach(function (a) {
          var v = e.getAttribute(a);
          if (v && /em$/.test(v)) e.setAttribute(a, parseFloat(v) * parseFloat(cs.fontSize));
        });
      }
      var f = cs.fill;
      if (f && f !== 'none' && f !== 'transparent') e.setAttribute('fill', f);
      var st = cs.stroke;
      if (st && st !== 'none' && st !== 'transparent') {
        e.setAttribute('stroke', st);
        if (cs.strokeWidth) e.setAttribute('stroke-width', cs.strokeWidth);
      }
    }
    // QSvgRenderer ignores <foreignObject> (HTML labels), so swap each one
    // for an equivalent <text> built from the label's computed style.
    var svgRootInv = s.getScreenCTM().inverse();
    foPairs.forEach(function (p) {
      var fo = p[0], target = p[1];
      var src = fo.querySelector('span, p, div') || fo;
      var lcs = getComputedStyle(src);
      var raw = (fo.innerText || fo.textContent || '').replace(/\u00a0/g, ' ');
      var lines = raw.split(/\r?\n/).map(function (t) { return t.trim(); })
                     .filter(function (t) { return t.length; });
      if (!lines.length) { target.remove(); return; }
      var fw = parseFloat(fo.getAttribute('width')) || fo.getBoundingClientRect().width;
      var fh = parseFloat(fo.getAttribute('height')) || fo.getBoundingClientRect().height;
      var fs = parseFloat(lcs.fontSize) || 16;
      var lh = fs * 1.2;
      var y0 = fh / 2 - (lines.length - 1) * lh / 2;
      var m = svgRootInv.multiply(fo.getScreenCTM());
      var g = document.createElementNS(NS, 'g');
      g.setAttribute('transform', 'matrix(' + [m.a, m.b, m.c, m.d, m.e, m.f].join(',')
                     + ') translate(' + (parseFloat(fo.getAttribute('x')) || 0)
                     + ',' + (parseFloat(fo.getAttribute('y')) || 0) + ')');
      // One <text> per line: QSvgRenderer mis-positions <tspan y=...>.
      lines.forEach(function (ln, k) {
        var t = document.createElementNS(NS, 'text');
        t.setAttribute('x', fw / 2);
        t.setAttribute('y', y0 + k * lh + fs * 0.35);
        t.setAttribute('text-anchor', 'middle');
        t.setAttribute('font-family', lcs.fontFamily.replace(/"/g, "'"));
        t.setAttribute('font-size', fs + 'px');
        t.setAttribute('font-weight', lcs.fontWeight);
        if (lcs.fontStyle && lcs.fontStyle !== 'normal') t.setAttribute('font-style', lcs.fontStyle);
        t.setAttribute('fill', lcs.color);
        t.setAttribute('stroke', 'none');
        t.textContent = ln;
        g.appendChild(t);
      });
      target.parentNode.removeChild(target);
      clone.appendChild(g);
    });
    clone.querySelectorAll('style').forEach(function (x) { x.remove(); });
    var vb = s.viewBox && s.viewBox.baseVal;
    var w = (vb && vb.width) || parseFloat(s.getAttribute('width')) || 0;
    var h = (vb && vb.height) || parseFloat(s.getAttribute('height')) || 0;
    if (w && h) {
      var rect = clone.querySelector('rect#canvas');
      if (!rect) {
        rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        clone.insertBefore(rect, clone.firstChild);
      }
      rect.setAttribute('x', vb ? vb.x : 0);
      rect.setAttribute('y', vb ? vb.y : 0);
      rect.setAttribute('width', w);
      rect.setAttribute('height', h);
      rect.setAttribute('fill', window.__lastBg || '#ffffff');
      clone.setAttribute('width', w);
      clone.setAttribute('height', h);
    }
    clone.style.maxWidth = '';
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    return new XMLSerializer().serializeToString(clone);
  }
