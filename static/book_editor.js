/* Shared Word-style book editor widget.
 *
 * Usage:
 *   const ed = new BookEditorWidget(rootEl, {
 *     contentUrl: id => `/api/publications/${id}/content`,   // base; ?which= appended
 *     onStatus: msg => statusEl.textContent = msg,
 *   });
 *   ed.load(id, 'kindle', 'Title');   // fetch + render
 *   ed.save();                        // POST dirty paragraphs
 *
 * The widget builds: [toolbar] + [scrolling doc canvas] inside rootEl.
 * Inline formatting uses execCommand (pragmatic for contenteditable); paragraph
 * props (align/list/style/indent) are stored as data-* on each paragraph div and
 * sent on save. The backend round-trips everything into the .docx.
 */
(function () {
  const FONTS = ['Carlito', 'Calibri', 'Arial', 'Times New Roman', 'Georgia',
                 'Verdana', 'Tahoma', 'Garamond', 'Courier New', 'Comic Sans MS'];
  const SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 72];
  const STYLES = ['Normal', 'Heading 1', 'Heading 2', 'Heading 3', 'Title'];

  function el(tag, attrs, html) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === 'class') e.className = attrs[k];
      else if (k === 'style') e.style.cssText = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    if (html != null) e.innerHTML = html;
    return e;
  }
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  class BookEditorWidget {
    constructor(root, cfg) {
      this.root = root;
      this.cfg = cfg || {};
      this.id = null; this.which = 'final'; this.blocks = [];
      this._build();
    }

    status(msg) { if (this.cfg.onStatus) this.cfg.onStatus(msg); }

    _build() {
      this.root.innerHTML = '';
      this.root.classList.add('bke-root');
      this.toolbar = el('div', { class: 'bke-toolbar' });
      // Body = optional outline panel + the scrolling canvas, side by side.
      this.body = el('div', { class: 'bke-bodyrow' });
      this.outline = el('div', { class: 'bke-outline', style: 'display:none' });
      this.canvas = el('div', { class: 'bke-canvas doc-canvas' });
      this.body.appendChild(this.outline);
      this.body.appendChild(this.canvas);
      this.root.appendChild(this.toolbar);
      this.root.appendChild(this.body);
      this._buildToolbar();
    }

    _btn(icon, title, onClick, label) {
      const b = el('button', { class: 'bke-btn', title, type: 'button' },
                   label ? label : `<i class="fas ${icon}"></i>`);
      b.addEventListener('mousedown', e => e.preventDefault()); // keep selection
      b.addEventListener('click', e => { e.preventDefault(); onClick(); });
      return b;
    }
    _sep() { return el('span', { class: 'bke-sep' }); }

    _buildToolbar() {
      const t = this.toolbar;

      // Style dropdown
      this.styleSel = el('select', { class: 'bke-select', title: 'Paragraph style' });
      STYLES.forEach(s => this.styleSel.appendChild(el('option', { value: s }, s)));
      this.styleSel.addEventListener('change', () => this._setParaProp('style', this.styleSel.value));
      t.appendChild(this.styleSel);

      // Font family
      this.fontSel = el('select', { class: 'bke-select', title: 'Font' });
      FONTS.forEach(f => this.fontSel.appendChild(el('option', { value: f }, f)));
      this.fontSel.addEventListener('change', () => this._exec('fontName', this.fontSel.value));
      t.appendChild(this.fontSel);

      // Font size
      this.sizeSel = el('select', { class: 'bke-select bke-size', title: 'Font size' });
      SIZES.forEach(s => this.sizeSel.appendChild(el('option', { value: s }, s)));
      this.sizeSel.value = '11';
      this.sizeSel.addEventListener('change', () => this._setFontSize(this.sizeSel.value));
      t.appendChild(this.sizeSel);

      t.appendChild(this._sep());
      t.appendChild(this._btn('fa-bold', 'Bold (Ctrl+B)', () => this._exec('bold')));
      t.appendChild(this._btn('fa-italic', 'Italic (Ctrl+I)', () => this._exec('italic')));
      t.appendChild(this._btn('fa-underline', 'Underline (Ctrl+U)', () => this._exec('underline')));
      t.appendChild(this._btn('fa-strikethrough', 'Strikethrough', () => this._exec('strikeThrough')));
      t.appendChild(this._btn('fa-superscript', 'Superscript', () => this._exec('superscript')));
      t.appendChild(this._btn('fa-subscript', 'Subscript', () => this._exec('subscript')));

      t.appendChild(this._sep());
      // Text color
      this.fg = el('input', { type: 'color', class: 'bke-color', title: 'Text color', value: '#000000' });
      this.fg.addEventListener('input', () => this._exec('foreColor', this.fg.value));
      const fgWrap = el('label', { class: 'bke-colorbtn', title: 'Text color' }, '<i class="fas fa-font"></i>');
      fgWrap.appendChild(this.fg); t.appendChild(fgWrap);
      // Highlight
      this.bg = el('input', { type: 'color', class: 'bke-color', title: 'Highlight', value: '#ffff00' });
      this.bg.addEventListener('input', () => this._exec('hiliteColor', this.bg.value) || this._exec('backColor', this.bg.value));
      const bgWrap = el('label', { class: 'bke-colorbtn', title: 'Highlight color' }, '<i class="fas fa-highlighter"></i>');
      bgWrap.appendChild(this.bg); t.appendChild(bgWrap);

      t.appendChild(this._sep());
      t.appendChild(this._btn('fa-align-left', 'Align left', () => this._setParaProp('align', 'left')));
      t.appendChild(this._btn('fa-align-center', 'Center', () => this._setParaProp('align', 'center')));
      t.appendChild(this._btn('fa-align-right', 'Align right', () => this._setParaProp('align', 'right')));
      t.appendChild(this._btn('fa-align-justify', 'Justify', () => this._setParaProp('align', 'justify')));

      t.appendChild(this._sep());
      t.appendChild(this._btn('fa-list-ul', 'Bulleted list', () => this._toggleList('bullet')));
      t.appendChild(this._btn('fa-list-ol', 'Numbered list', () => this._toggleList('number')));
      t.appendChild(this._btn('fa-outdent', 'Decrease indent', () => this._indent(-1)));
      t.appendChild(this._btn('fa-indent', 'Increase indent', () => this._indent(1)));

      t.appendChild(this._sep());
      t.appendChild(this._btn('fa-link', 'Insert link', () => this._link()));
      t.appendChild(this._btn('fa-eraser', 'Clear formatting', () => { this._exec('removeFormat'); }));
      t.appendChild(this._btn('fa-rotate-left', 'Undo (Ctrl+Z)', () => this._exec('undo')));
      t.appendChild(this._btn('fa-rotate-right', 'Redo (Ctrl+Y)', () => this._exec('redo')));

      t.appendChild(this._sep());
      t.appendChild(this._btn('fa-magnifying-glass', 'Find & replace', () => this._toggleFind(), '<i class="fas fa-magnifying-glass"></i>'));

      t.appendChild(this._sep());
      const cBtn = this._btn('fa-list', 'Contents (outline)', () => this._toggleOutline(),
                             '<i class="fas fa-list-ul"></i> Contents');
      cBtn.classList.add('bke-btn-text');
      t.appendChild(cBtn);

      // Live word count for the whole manuscript, pinned to the right.
      this.wordCountEl = el('span', { class: 'bke-wordcount', title: 'Total words in this book' }, '');
      t.appendChild(this.wordCountEl);
      const tocBtn = this._btn(null, 'Insert / refresh a Table of Contents page in the document',
                               () => this._refreshTOC(), '<i class="fas fa-stream"></i> Insert/Refresh TOC');
      tocBtn.classList.add('bke-btn-text');
      this.tocBtn = tocBtn;
      t.appendChild(tocBtn);

      // Find & replace bar (hidden by default)
      this.findBar = el('div', { class: 'bke-findbar', style: 'display:none' });
      this.findInput = el('input', { class: 'bke-find', placeholder: 'Find' });
      this.replInput = el('input', { class: 'bke-find', placeholder: 'Replace with' });
      const doRepl = this._btn(null, 'Replace all', () => this._replaceAll(), 'Replace all');
      doRepl.classList.add('bke-btn-text');
      this.findBar.appendChild(this.findInput);
      this.findBar.appendChild(this.replInput);
      this.findBar.appendChild(doRepl);
    }

    _toggleFind() {
      const show = this.findBar.style.display === 'none';
      this.findBar.style.display = show ? 'flex' : 'none';
      if (show && !this.findBar.parentNode) this.toolbar.after(this.findBar);
      if (show) this.findInput.focus();
    }

    // ---- Outline (table of contents navigator) ----
    _toggleOutline() {
      const show = this.outline.style.display === 'none';
      this.outline.style.display = show ? 'flex' : 'none';
      if (show) this._renderOutline();
    }

    _renderOutline() {
      this.outline.innerHTML = '';
      this.outline.appendChild(el('div', { class: 'bke-outline-h' }, 'Contents'));
      const heads = this.blocks.filter(b => b.type === 'heading' || b.type === 'subheading');
      if (!heads.length) {
        this.outline.appendChild(el('div', { class: 'bke-outline-empty' }, 'No headings found.'));
        return;
      }
      heads.forEach(b => {
        const row = el('div', { class: 'bke-outline-row' + (b.type === 'subheading' ? ' sub' : '') });
        const jump = el('button', { class: 'bke-outline-jump', title: 'Jump to this heading', type: 'button' },
                        '<i class="fas fa-location-arrow"></i>');
        jump.addEventListener('click', () => this._jumpTo(b.index));
        const inp = el('input', { class: 'bke-outline-text', value: b.text || '' });
        inp.addEventListener('change', () => this._renameHeading(b.index, inp.value));
        row.appendChild(jump);
        row.appendChild(inp);
        this.outline.appendChild(row);
      });
    }

    // ---- word count ----
    // Counted off the rendered paragraphs rather than `this.blocks` so the
    // number tracks what the user is typing, not what was last loaded.
    wordCount() {
      let n = 0;
      this.canvas.querySelectorAll('[contenteditable]').forEach(p => {
        const words = (p.innerText || '').trim().match(/[^\s]+/g);
        if (words) n += words.length;
      });
      return n;
    }

    _refreshWordCount() {
      const n = this.wordCount();
      if (this.wordCountEl) {
        this.wordCountEl.textContent = `${n.toLocaleString()} words`;
      }
      if (this.cfg.onWordCount) this.cfg.onWordCount(n);
      return n;
    }

    _paraByIndex(idx) {
      return this.canvas.querySelector(`[data-index="${idx}"]`);
    }

    _jumpTo(idx) {
      const p = this._paraByIndex(idx);
      if (!p) return;
      p.scrollIntoView({ behavior: 'smooth', block: 'center' });
      p.style.transition = 'background .2s';
      const old = p.style.background;
      p.style.background = '#fff3bf';
      setTimeout(() => { p.style.background = old; }, 800);
      p.focus();
    }

    _renameHeading(idx, text) {
      const p = this._paraByIndex(idx);
      if (!p) return;
      p.textContent = text;            // headings are plain text
      this._markDirty(p);
      const b = this.blocks.find(x => x.index === idx);
      if (b) b.text = text;
    }

    _paraAncestor(node) {
      while (node && node !== this.canvas) {
        if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute('contenteditable')) return node;
        node = node.parentNode;
      }
      return null;
    }

    // Where should the TOC go? Cursor paragraph, or the range a selection spans.
    _tocAnchor() {
      const sel = window.getSelection();
      let startP = null, endP = null;
      if (sel && sel.rangeCount) {
        startP = this._paraAncestor(sel.anchorNode);
        endP = this._paraAncestor(sel.focusNode);
      }
      startP = startP || this._lastPara;
      if (sel && !sel.isCollapsed && startP && endP &&
          startP.dataset.index != null && endP.dataset.index != null) {
        let a = parseInt(startP.dataset.index, 10), b = parseInt(endP.dataset.index, 10);
        if (a > b) { const t = a; a = b; b = t; }
        return { replace_start: a, replace_end: b };
      }
      if (startP && startP.dataset.index != null) return { at_index: parseInt(startP.dataset.index, 10) };
      return {};   // backend falls back to top
    }

    async _refreshTOC() {
      // Capture the cursor/selection BEFORE saving (save doesn't reload, so the
      // indices stay valid).
      const anchor = this._tocAnchor();
      await this.save(true);
      const where = anchor.replace_start != null ? 'replacing selection'
                  : anchor.at_index != null ? 'at cursor' : 'at top';
      this.status(`Building Table of Contents (${where})…`);
      try {
        const base = this.cfg.contentUrl(this.id).replace(/\/content$/, '/toc');
        const r = await fetch(`${base}?which=${encodeURIComponent(this.which)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(anchor),
        });
        const data = await r.json();
        if (!r.ok || data.ok === false) { this.status(data.error || ('TOC failed (' + r.status + ')')); return; }
        this.status(`Table of Contents updated (${data.entries} entries, ${where}).`);
        await this.load(this.id, this.which, this.title);   // reload to show the TOC
        if (this.outline.style.display !== 'none') this._renderOutline();
      } catch (e) { this.status(e.message || String(e)); }
    }

    // ---- selection helpers ----
    _focusedPara() {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return this._lastPara || null;
      let node = sel.anchorNode;
      while (node && node !== this.canvas) {
        if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute('contenteditable')) return node;
        node = node.parentNode;
      }
      return this._lastPara || null;
    }
    _markDirty(p) { if (p) { p.dataset.dirty = '1'; p.classList.add('dirty'); } }

    _exec(cmd, val) {
      try { document.execCommand('styleWithCSS', false, true); } catch (e) {}
      let ok = false;
      try { ok = document.execCommand(cmd, false, val == null ? null : val); } catch (e) {}
      this._markDirty(this._focusedPara());
      return ok;
    }

    _setFontSize(pt) {
      // execCommand fontSize only does 1-7; apply size 7 then rewrite to pt.
      try { document.execCommand('styleWithCSS', false, true); } catch (e) {}
      document.execCommand('fontSize', false, '7');
      const p = this._focusedPara();
      if (p) {
        p.querySelectorAll('font[size="7"], span[style*="xxx-large"]').forEach(node => {
          const span = document.createElement('span');
          span.style.fontSize = pt + 'pt';
          span.innerHTML = node.innerHTML;
          node.replaceWith(span);
        });
        this._markDirty(p);
      }
    }

    _setParaProp(prop, val) {
      const p = this._focusedPara();
      if (!p) return;
      if (prop === 'align') { p.style.textAlign = val; p.dataset.align = val; }
      else if (prop === 'style') {
        p.dataset.style = val;
        p.classList.remove('doc-h', 'doc-sub', 'doc-p');
        p.classList.add(val === 'Heading 1' || val === 'Title' ? 'doc-h'
                        : val === 'Heading 2' || val === 'Heading 3' ? 'doc-sub' : 'doc-p');
      }
      this._markDirty(p);
    }

    _toggleList(kind) {
      const p = this._focusedPara();
      if (!p) return;
      p.dataset.list = (p.dataset.list === kind) ? '' : kind;
      p.classList.toggle('bke-li-bullet', p.dataset.list === 'bullet');
      p.classList.toggle('bke-li-number', p.dataset.list === 'number');
      this._markDirty(p);
    }

    _indent(delta) {
      const p = this._focusedPara();
      if (!p) return;
      const cur = parseInt(p.dataset.indent || '0', 10);
      const next = Math.max(0, Math.min(8, cur + delta));
      p.dataset.indent = next;
      p.style.paddingLeft = next ? (next * 0.5) + 'in' : '';
      this._markDirty(p);
    }

    _link() {
      const url = prompt('Link URL:', 'https://');
      if (url) this._exec('createLink', url);
    }

    _replaceAll() {
      const find = this.findInput.value;
      if (!find) return;
      const repl = this.replInput.value;
      let n = 0;
      this.canvas.querySelectorAll('[contenteditable]').forEach(p => {
        if (p.innerText.includes(find)) {
          // Replace in text nodes only (preserve inline formatting where possible)
          const before = p.innerHTML;
          const after = before.split(esc(find)).join(esc(repl));
          if (after !== before) { p.innerHTML = after; this._markDirty(p); n++; }
        }
      });
      this._refreshWordCount();
      this.status(`Replaced in ${n} paragraph(s).`);
    }

    // ---- load / render / save ----
    async load(id, which, titleHint) {
      this.id = id; this.which = which || this.which;
      this.canvas.innerHTML = '<div style="text-align:center;padding:40px;color:#888"><i class="fas fa-circle-notch fa-spin"></i> Loading…</div>';
      try {
        const base = this.cfg.contentUrl(id);
        const r = await fetch(`${base}?which=${encodeURIComponent(this.which)}`);
        const data = await r.json();
        if (!r.ok) { this.canvas.innerHTML = `<div style="text-align:center;padding:40px;color:#c00">${esc(data.error || ('HTTP ' + r.status))}</div>`; return; }
        this.blocks = data.blocks || [];
        this.title = data.title || titleHint || 'Book';
        this._render();
      } catch (e) {
        this.canvas.innerHTML = `<div style="text-align:center;padding:40px;color:#c00">${esc(e.message || e)}</div>`;
      }
    }

    _render() {
      this.canvas.innerHTML = '';
      const page = el('div', { class: 'doc-page' });
      this.blocks.forEach(b => {
        if (b.type === 'image') {
          const img = el('img', { class: 'doc-img' });
          img.src = `${this.cfg.contentUrl(this.id)}/image/${b.index}?which=${encodeURIComponent(this.which)}`;
          img.onerror = () => img.replaceWith(el('div', { style: 'text-align:center;color:#aaa;font-size:12px;padding:8px' }, '🖼 image'));
          page.appendChild(img);
          return;
        }
        // `type` comes from the writer's own heading detection and is the
        // reliable signal: these books format headings directly (bold/size)
        // rather than with named Word styles, so `style` is "Normal" for
        // nearly every paragraph. Fall back to `style` when type is absent.
        const cls = (b.type === 'heading' || b.style === 'Heading 1' || b.style === 'Title') ? 'doc-h'
                  : (b.type === 'subheading' || b.style === 'Heading 2' || b.style === 'Heading 3') ? 'doc-sub'
                  : 'doc-p';
        const div = el('div', { class: cls, contenteditable: 'true' });
        div.dataset.index = b.index;
        div.dataset.style = b.style || 'Normal';
        if (b.align) { div.dataset.align = b.align; div.style.textAlign = b.align; }
        if (b.list) { div.dataset.list = b.list; div.classList.add(b.list === 'number' ? 'bke-li-number' : 'bke-li-bullet'); }
        if (b.indent) { div.dataset.indent = b.indent; div.style.paddingLeft = (b.indent * 0.5) + 'in'; }
        div.innerHTML = b.html || esc(b.text || '');
        div.addEventListener('input', () => { this._markDirty(div); this._queueWordCount(); });
        div.addEventListener('focus', () => { this._lastPara = div; this._syncToolbar(div); });
        div.addEventListener('keyup', () => this._syncToolbar(div));
        div.addEventListener('mouseup', () => this._syncToolbar(div));
        page.appendChild(div);
      });
      this.canvas.appendChild(page);
      this._refreshWordCount();
    }

    // Recounting walks every paragraph, so keep it off the keystroke path.
    _queueWordCount() {
      clearTimeout(this._wcTimer);
      this._wcTimer = setTimeout(() => this._refreshWordCount(), 300);
    }

    _syncToolbar(p) {
      if (!p) return;
      if (this.styleSel) this.styleSel.value = STYLES.includes(p.dataset.style) ? p.dataset.style : 'Normal';
    }

    async save(silent) {
      const dirty = Array.from(this.canvas.querySelectorAll('[contenteditable][data-dirty="1"]'));
      if (!dirty.length) { if (!silent) this.status('No changes to save.'); return; }
      const edits = dirty.map(p => ({
        index: parseInt(p.dataset.index, 10),
        html: p.innerHTML,
        align: p.dataset.align || '',
        list: p.dataset.list || '',
        style: p.dataset.style || 'Normal',
        indent: parseInt(p.dataset.indent || '0', 10),
      }));
      this.status('Saving…');
      try {
        const base = this.cfg.contentUrl(this.id);
        const r = await fetch(`${base}?which=${encodeURIComponent(this.which)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ edits }),
        });
        const data = await r.json();
        if (!r.ok) { this.status(data.error || ('Save failed (' + r.status + ')')); return; }
        this.status(`Saved ${data.changed} paragraph(s).`);
        dirty.forEach(p => { delete p.dataset.dirty; p.classList.remove('dirty'); });
      } catch (e) { this.status(e.message || String(e)); }
    }
  }

  window.BookEditorWidget = BookEditorWidget;
})();
