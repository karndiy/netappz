/* Edit PDF: an Acrobat-style editor for one PDF.
 *
 * The page images come from the server; everything the user adds lives in
 * `state` as plain objects in PDF points (displayed orientation, origin top-left).
 * The server applies `state` to the original PDF when downloading.
 */
(function () {
  "use strict";

  var app = document.getElementById("pe-app");
  var info = JSON.parse(document.getElementById("pe-data").textContent);
  var base = app.dataset.base;
  var csrf = app.dataset.csrf;
  var canvas = document.getElementById("pe-canvas");
  var docEl = document.getElementById("pe-doc");
  var props = document.getElementById("pe-props");
  var rail = document.getElementById("pe-rail");
  var pagesPanel = document.getElementById("pe-pages");
  var thumbs = document.getElementById("pe-thumbs");
  var hint = document.getElementById("pe-hint");
  var statusEl = document.getElementById("pe-status");
  var pageInfo = document.getElementById("pe-pageinfo");
  var nameInput = document.getElementById("pe-name");
  var imageInput = document.getElementById("pe-image-input");

  var FONTS = {
    sans: "Helvetica, Arial, 'Noto Sans Thai', sans-serif",
    serif: "'Times New Roman', Times, 'Noto Serif Thai', serif",
    mono: "'Courier New', Courier, monospace",
  };
  var RENDER_WIDTHS = [400, 800, 1200, 1600, 2400];
  var HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

  // ------------------------------------------------------------ icons
  function icon(paths) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + paths + "</svg>"; }
  var ICONS = {
    select: icon('<path d="M5 3.5l13 7-5.6 1.5L9.8 18z"/><path d="m13 13 4.5 5"/>'),
    edittext: icon('<path d="M4 6V4.5h11V6M9.5 4.5v13M7.5 17.5h4"/><path d="m14 19 1-3.5 5-5 2.5 2.5-5 5z"/>'),
    text: icon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 9V8h8v1M12 8v8M10.5 16h3"/>'),
    image: icon('<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9.5" r="1.8"/><path d="m4 18 5.5-5.5 3.5 3.5 2.5-2.5L20.5 18"/>'),
    signature: icon('<path d="M3 17c3-1 4.5-9 7-9 2 0-1.5 9 1 9 1.5 0 2.5-3 4-3 1 0 .8 2 2 2 .8 0 1.6-.6 3-1.5"/><path d="M3 20.5h18"/>'),
    draw: icon('<path d="M4 20c.5-2 1-3.5 2-4.5L16.5 5a2.1 2.1 0 0 1 3 3L9 18.5c-1 1-2.5 1.5-5 1.5z"/><path d="m14.5 7 3 3"/>'),
    shape: icon('<rect x="3" y="11" width="9" height="9" rx="1"/><circle cx="16.5" cy="7.5" r="4.5"/>'),
    highlight: icon('<path d="m9 15-3 3h-3l3-3M14.5 4.5l5 5-7 7-5-5z"/><path d="M3 21h18" stroke-width="3" stroke="#f5c400"/>'),
    note: icon('<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z"/><path d="M8 8.5h8M8 12h5"/>'),
    whiteout: icon('<path d="m4 16 8-8 6 6-4 4H8z"/><path d="M9 20h11"/><path d="m12 8 3-3 6 6-3 3"/>'),
    redact: icon('<rect x="3" y="6" width="18" height="5" rx="1" fill="currentColor"/><path d="M3 15h18M3 19h11"/>'),
    pages: icon('<rect x="4" y="3" width="11" height="14" rx="1.5"/><path d="M9 21h9.5a1.5 1.5 0 0 0 1.5-1.5V8"/>'),
    document: icon('<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12l6 4M9 16l6-4"/>'),
    rotateLeft: icon('<path d="M4 4v5h5"/><path d="M5 13a7 7 0 1 0 2-6l-3 2"/>'),
    rotateRight: icon('<path d="M20 4v5h-5"/><path d="M19 13a7 7 0 1 1-2-6l3 2"/>'),
    duplicate: icon('<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>'),
    trash: icon('<path d="M4 7h16M10 7V4.5h4V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5"/>'),
    comment: icon('<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 4v-4A1.5 1.5 0 0 1 4 14.5z"/>'),
  };

  var TOOLS = [
    { id: "select", label: "Select", key: "v", help: "Click an item to select it. Drag to move, use the handles to resize." },
    { id: "edittext", label: "Edit text", key: "e", help: "Click a line of text in the PDF to change it." },
    { id: "text", label: "Add text", key: "t", help: "Click where the text should go, then type." },
    { id: "image", label: "Image", key: "i", help: "Choose a picture, then click the page to place it. You can also drop a picture on a page." },
    { id: "signature", label: "Sign", key: "s", help: "Draw your signature, then click the page to place it." },
    { sep: true },
    { id: "draw", label: "Draw", key: "d", help: "Draw freehand on the page." },
    { id: "shape", label: "Shapes", key: "r", help: "Drag on the page to draw a shape." },
    { id: "highlight", label: "Highlight", key: "h", help: "Click a line of text or drag over an area." },
    { id: "note", label: "Comment", key: "c", help: "Click the page to add a comment." },
    { sep: true },
    { id: "whiteout", label: "Whiteout", key: "w", help: "Drag to cover something with white." },
    { id: "redact", label: "Redact", key: "x", help: "Drag over anything to remove it for good when you download." },
    { sep: true },
    { id: "pages", label: "Pages", panel: true, help: "" },
    { id: "document", label: "Watermark", panel: true, help: "" },
  ];

  // ------------------------------------------------------------ state
  function uid() { return Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4); }

  function freshState() {
    return {
      version: 1,
      pages: info.pages.map(function (page, index) {
        return { id: uid(), src: index, w: page.width, h: page.height, rotate: 0, objects: [] };
      }),
      assets: {},
      watermark: { enabled: false, text: "CONFIDENTIAL", size: 72, color: "#e5484d", opacity: 0.2, angle: 45, layer: "over" },
      pageNumbers: { enabled: false, format: "Page {n} of {total}", position: "bottom-center", size: 10, color: "#444444" },
    };
  }

  var state = info.state && info.state.version === 1 ? info.state : freshState();
  state.assets = state.assets || {};
  var defaults = {
    text: { font: "sans", size: 14, color: "#111111", bold: false, italic: false, align: "left" },
    draw: { color: "#e5484d", width: 3, opacity: 1 },
    shape: { kind: "rect", color: "#155eef", fill: "", width: 2, opacity: 1 },
    highlight: { color: "#ffd60a" },
    note: { color: "#ffd60a" },
  };
  var tool = "select";
  var panel = null; // "pages" | "document" | null
  var selected = null; // { page: pageId, id: objectId }
  var editing = null; // objectId being typed into
  var zoom = 1;
  var fitZoom = 1;
  var pending = null; // { assetId, ratio, kind } waiting to be placed
  var caretAt = null; // { x, y } screen point to put the caret at when editing starts
  var textCache = {}; // src -> lines (or a Promise)
  var slots = {}; // pageId -> { slot, page, img, layer }

  // ------------------------------------------------------------ history and autosave
  function snapshot() {
    return JSON.stringify({ pages: state.pages, watermark: state.watermark, pageNumbers: state.pageNumbers });
  }
  var lastSnap = snapshot();
  var undoStack = [];
  var redoStack = [];
  var saveTimer = null;
  var saving = false;
  var saveAgain = false;

  function commit() {
    var snap = snapshot();
    if (snap === lastSnap) return;
    undoStack.push(lastSnap);
    if (undoStack.length > 150) undoStack.shift();
    redoStack = [];
    lastSnap = snap;
    historyButtons();
    scheduleSave();
  }

  function restore(snap) {
    var data = JSON.parse(snap);
    state.pages = data.pages;
    state.watermark = data.watermark;
    state.pageNumbers = data.pageNumbers;
    lastSnap = snap;
    if (selected && !findObject(selected.page, selected.id)) selected = null;
    editing = null;
    renderAll();
    historyButtons();
    scheduleSave();
  }

  function undo() { if (undoStack.length) { finishEditing(); redoStack.push(snapshot()); restore(undoStack.pop()); } }
  function redo() { if (redoStack.length) { finishEditing(); undoStack.push(snapshot()); restore(redoStack.pop()); } }

  function historyButtons() {
    document.getElementById("pe-undo").disabled = !undoStack.length;
    document.getElementById("pe-redo").disabled = !redoStack.length;
  }

  function setStatus(text, error) {
    statusEl.textContent = text;
    statusEl.classList.toggle("error", !!error);
  }

  function scheduleSave() {
    setStatus("Unsaved changes…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveState, 1200);
  }

  function saveState() {
    if (saving) { saveAgain = true; return; }
    saving = true;
    setStatus("Saving…");
    var body = JSON.stringify(state);
    fetch(base + "/state", {
      method: "POST", body: body, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    })
      .then(function (response) {
        if (!response.ok) return response.json().catch(function () { return {}; }).then(function (data) { throw new Error(data.error || "Not saved"); });
        setStatus("All changes saved");
      })
      .catch(function (error) { setStatus(error.message === "Failed to fetch" ? "Offline: changes not saved" : error.message, true); })
      .then(function () {
        saving = false;
        if (saveAgain) { saveAgain = false; saveState(); }
      });
  }

  // ------------------------------------------------------------ helpers
  function findPage(id) {
    for (var i = 0; i < state.pages.length; i++) if (state.pages[i].id === id) return state.pages[i];
    return null;
  }
  function findObject(pageId, id) {
    var page = findPage(pageId);
    if (!page) return null;
    for (var i = 0; i < page.objects.length; i++) if (page.objects[i].id === id) return page.objects[i];
    return null;
  }
  function selectedObject() { return selected ? findObject(selected.page, selected.id) : null; }
  function rotation(page) { return ((page.rotate % 360) + 360) % 360; }
  function turned(page) { return rotation(page) % 180 !== 0; }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  function round(value) { return Math.round(value * 100) / 100; }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function toast(html, error) {
    var box = document.getElementById("pe-toast");
    box.innerHTML = html;
    box.classList.toggle("error", !!error);
    box.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { box.hidden = true; }, error ? 6000 : 4000);
  }
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }

  // Convert a pointer position to page points, undoing the page's on-screen rotation.
  function toPoint(event, page) {
    var box = slots[page.id].page.getBoundingClientRect();
    var angle = -rotation(page) * Math.PI / 180;
    var dx = event.clientX - (box.left + box.width / 2);
    var dy = event.clientY - (box.top + box.height / 2);
    var x = dx * Math.cos(angle) - dy * Math.sin(angle);
    var y = dx * Math.sin(angle) + dy * Math.cos(angle);
    return { x: x / zoom + page.w / 2, y: y / zoom + page.h / 2 };
  }

  // ------------------------------------------------------------ tool rail
  TOOLS.forEach(function (item) {
    if (item.sep) { rail.appendChild(el("span", "pe-rail-sep")); return; }
    var button = el("button", "pe-tool");
    button.type = "button";
    button.dataset.tool = item.id;
    button.innerHTML = ICONS[item.id] + "<span>" + item.label + "</span>";
    button.title = (item.id === "document" ? "Watermark & page numbers" : item.label) + (item.key ? " (" + item.key.toUpperCase() + ")" : "");
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", function () {
      if (item.panel) togglePanel(item.id);
      else setTool(item.id);
    });
    rail.appendChild(button);
  });

  function railState() {
    rail.querySelectorAll(".pe-tool").forEach(function (button) {
      var id = button.dataset.tool;
      var active = id === "pages" || id === "document" ? panel === id : tool === id;
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setTool(id) {
    finishEditing();
    if (id === "image") { imageInput.value = ""; imageInput.click(); }
    if (id === "signature") { openSignature(); }
    if (id !== "image" && id !== "signature") pending = null;
    tool = id;
    app.dataset.tool = id;
    app.classList.toggle("is-placing", !!pending);
    if (panel === "document") panel = null;
    if (id !== "select" && selected && tool !== "edittext") select(null);
    if (id === "edittext" || id === "highlight") loadAllText();
    railState();
    renderAll();
    showHint();
  }

  function togglePanel(id) {
    finishEditing();
    panel = panel === id ? null : id;
    pagesPanel.hidden = panel !== "pages";
    if (panel === "pages") renderThumbs();
    railState();
    renderProps();
  }

  function showHint() {
    var item = TOOLS.filter(function (t) { return t.id === tool; })[0];
    var text = pending ? (pending.kind === "signature" ? "Click the page to place your signature." : "Click the page to place the picture.")
      : item && tool !== "select" ? item.help : "";
    hint.hidden = !text;
    hint.innerHTML = escapeHtml(text) + (text ? '<button type="button" data-done>Done</button>' : "");
  }
  hint.addEventListener("click", function (event) { if (event.target.closest("[data-done]")) setTool("select"); });

  // ------------------------------------------------------------ rendering pages
  function renderWidth(page) {
    var needed = page.w * zoom * (window.devicePixelRatio || 1);
    for (var i = 0; i < RENDER_WIDTHS.length; i++) if (RENDER_WIDTHS[i] >= needed) return RENDER_WIDTHS[i];
    return RENDER_WIDTHS[RENDER_WIDTHS.length - 1];
  }

  function renderAll() {
    var keep = {};
    state.pages.forEach(function (page, index) {
      keep[page.id] = true;
      var parts = slots[page.id];
      if (!parts) {
        parts = { slot: el("div", "pe-slot"), page: el("div", "pe-page") };
        parts.slot.dataset.page = page.id;
        parts.page.dataset.page = page.id;
        parts.slot.appendChild(parts.page);
        slots[page.id] = parts;
      }
      docEl.appendChild(parts.slot); // Keeps DOM order in step with page order.
      layoutPage(page, index);
    });
    Object.keys(slots).forEach(function (id) {
      if (!keep[id]) { slots[id].slot.remove(); delete slots[id]; }
    });
    renderProps();
    if (panel === "pages") renderThumbs();
    updatePageInfo();
  }

  function layoutPage(page, index) {
    var parts = slots[page.id];
    var w = page.w * zoom;
    var h = page.h * zoom;
    var sideways = turned(page);
    parts.slot.style.width = (sideways ? h : w) + "px";
    parts.slot.style.height = (sideways ? w : h) + "px";
    parts.page.style.width = w + "px";
    parts.page.style.height = h + "px";
    parts.page.style.transform = "translate(-50%, -50%) rotate(" + rotation(page) + "deg)";

    if (page.src !== null && page.src !== undefined) {
      var width = renderWidth(page);
      var url = base + "/page/" + page.src + ".png?w=" + width;
      if (!parts.img) {
        parts.img = el("img");
        parts.img.alt = "";
        parts.img.loading = "lazy";
        parts.img.decoding = "async";
        parts.img.draggable = false;
      }
      if (parts.img.dataset.url !== url) { parts.img.src = url; parts.img.dataset.url = url; }
      if (parts.img.parentNode !== parts.page) parts.page.prepend(parts.img);
    } else if (parts.img) {
      parts.img.remove();
      parts.img = null;
    }
    renderLayer(page, index);
  }

  function renderLayer(page, index) {
    var parts = slots[page.id];
    if (index === undefined) index = state.pages.indexOf(page);
    Array.prototype.slice.call(parts.page.children).forEach(function (child) {
      if (child !== parts.img) child.remove();
    });
    Array.prototype.slice.call(parts.slot.children).forEach(function (child) {
      if (child !== parts.page) child.remove();
    });
    if (page.src === null || page.src === undefined) parts.page.appendChild(el("div", "pe-blank-label", "Blank page"));

    // Lines of existing text, for Edit text and Highlight.
    if ((tool === "edittext" || tool === "highlight") && typeof page.src === "number") {
      var lines = textCache[page.src];
      if (Array.isArray(lines)) {
        var replaced = {};
        page.objects.forEach(function (obj) { if (obj.origKey !== undefined) replaced[obj.origKey] = true; });
        lines.forEach(function (line, lineIndex) {
          if (tool === "edittext" && replaced[lineIndex]) return;
          var box = el("div", "pe-line");
          box.dataset.line = lineIndex;
          place(box, line.x - 1, line.y - 1, line.w + 2, line.h + 2);
          box.title = tool === "edittext" ? "Click to edit" : "Click to highlight";
          parts.page.appendChild(box);
        });
      }
    }

    page.objects.forEach(function (obj) { parts.page.appendChild(objectElement(obj, page)); });
    renderOverlays(page, index, parts.slot);
  }

  function place(node, x, y, w, h) {
    node.style.left = x * zoom + "px";
    node.style.top = y * zoom + "px";
    if (w !== undefined) node.style.width = Math.max(0, w) * zoom + "px";
    if (h !== undefined) node.style.height = Math.max(0, h) * zoom + "px";
  }

  // Watermark and page-number previews sit on the slot, so they follow the final page orientation.
  function renderOverlays(page, index, slot) {
    var width = (turned(page) ? page.h : page.w) * zoom;
    var height = (turned(page) ? page.w : page.h) * zoom;
    var mark = state.watermark;
    if (mark.enabled && mark.text.trim()) {
      var wm = el("div", "pe-watermark" + (mark.layer === "under" ? " is-under" : ""), mark.text);
      var size = mark.size * zoom;
      wm.style.fontSize = size + "px";
      wm.style.color = mark.color;
      wm.style.opacity = mark.opacity;
      slot.appendChild(wm);
      var textWidth = wm.offsetWidth;
      var textHeight = wm.offsetHeight;
      var radians = Math.abs(mark.angle) * Math.PI / 180;
      var tw = textWidth * Math.cos(radians) + textHeight * Math.sin(radians);
      var th = textWidth * Math.sin(radians) + textHeight * Math.cos(radians);
      var scale = Math.min(1, width * 0.9 / tw, height * 0.9 / th);
      wm.style.transform = "translate(-50%, -50%) rotate(" + (-mark.angle) + "deg) scale(" + scale + ")";
    }
    var numbers = state.pageNumbers;
    if (numbers.enabled) {
      var text = numbers.format.replace("{n}", index + 1).replace("{total}", state.pages.length);
      var label = el("div", "pe-pagenum", text);
      var parts = numbers.position.split("-");
      label.style.fontSize = numbers.size * zoom + "px";
      label.style.color = numbers.color;
      label.style.textAlign = parts[1] || "center";
      label.style.left = label.style.right = 24 * zoom + "px";
      if (parts[0] === "top") label.style.top = 24 * zoom + "px";
      else label.style.bottom = (24 * zoom) + "px";
      slot.appendChild(label);
    }
  }

  // ------------------------------------------------------------ objects
  function bounds(obj) {
    if (obj.type === "line") {
      var pad = Math.max(obj.width || 2, 6);
      return {
        x: Math.min(obj.x1, obj.x2) - pad, y: Math.min(obj.y1, obj.y2) - pad,
        w: Math.abs(obj.x2 - obj.x1) + pad * 2, h: Math.abs(obj.y2 - obj.y1) + pad * 2,
      };
    }
    if (obj.type === "note") return { x: obj.x, y: obj.y, w: 0, h: 0 };
    return { x: obj.x, y: obj.y, w: obj.w, h: obj.h };
  }

  function inkBounds(obj) {
    var xs = [], ys = [];
    obj.points.forEach(function (stroke) { stroke.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); }); });
    var pad = (obj.width || 2) / 2;
    obj.x = Math.min.apply(null, xs) - pad;
    obj.y = Math.min.apply(null, ys) - pad;
    obj.w = Math.max.apply(null, xs) - obj.x + pad;
    obj.h = Math.max.apply(null, ys) - obj.y + pad;
  }

  function textStyle(node, obj) {
    node.style.fontFamily = FONTS[obj.font] || FONTS.sans;
    node.style.fontSize = obj.size * zoom + "px";
    node.style.color = obj.color;
    node.style.fontWeight = obj.bold ? "700" : "400";
    node.style.fontStyle = obj.italic ? "italic" : "normal";
    node.style.textAlign = obj.align || "left";
  }

  function objectElement(obj, page) {
    var node = el("div", "pe-obj is-" + obj.type);
    node.dataset.id = obj.id;
    var box = bounds(obj);
    place(node, box.x, box.y, box.w, box.h);
    switch (obj.type) {
      case "text":
      case "textedit":
        node.classList.add("is-text");
        if (obj.autoWidth) node.classList.add("is-auto");
        textStyle(node, obj);
        node.textContent = obj.text;
        node.style.height = "auto";
        node.style.minHeight = obj.size * 1.2 * zoom + "px";
        if (obj.type === "textedit" && obj.orig) {
          // Cover the original line, which the server removes on download.
          node.style.minWidth = obj.orig.w * zoom + "px";
          node.style.minHeight = obj.orig.h * zoom + "px";
        }
        break;
      case "image":
      case "signature":
        node.classList.add("is-image");
        var img = el("img");
        img.alt = "";
        img.draggable = false;
        img.src = state.assets[obj.assetId] || "";
        node.appendChild(img);
        break;
      case "highlight":
        node.style.background = obj.color;
        break;
      case "rect":
      case "ellipse":
        node.style.border = (obj.border === false ? 0 : obj.width * zoom) + "px solid " + obj.color;
        node.style.background = obj.fill || "transparent";
        node.style.opacity = obj.opacity;
        if (obj.type === "ellipse") node.style.borderRadius = "50%";
        break;
      case "line":
        var svg = '<svg viewBox="' + [box.x, box.y, box.w, box.h].join(" ") + '" preserveAspectRatio="none">' +
          '<line x1="' + obj.x1 + '" y1="' + obj.y1 + '" x2="' + obj.x2 + '" y2="' + obj.y2 + '" stroke="' + obj.color +
          '" stroke-width="' + obj.width + '" stroke-linecap="round" opacity="' + obj.opacity + '"/>';
        if (obj.arrow) {
          var dx = obj.x2 - obj.x1, dy = obj.y2 - obj.y1, len = Math.hypot(dx, dy) || 1;
          var ux = dx / len, uy = dy / len, head = Math.max(6, obj.width * 4);
          var ax = obj.x2 - ux * head, ay = obj.y2 - uy * head;
          svg += '<path d="M' + (ax - uy * head * 0.6) + " " + (ay + ux * head * 0.6) + " L" + obj.x2 + " " + obj.y2 + " L" +
            (ax + uy * head * 0.6) + " " + (ay - ux * head * 0.6) + '" fill="none" stroke="' + obj.color + '" stroke-width="' +
            obj.width + '" stroke-linecap="round" stroke-linejoin="round" opacity="' + obj.opacity + '"/>';
        }
        node.innerHTML = svg + "</svg>";
        break;
      case "ink":
        node.innerHTML = '<svg viewBox="' + [obj.x, obj.y, Math.max(obj.w, 0.1), Math.max(obj.h, 0.1)].join(" ") + '" preserveAspectRatio="none">' +
          obj.points.map(function (stroke) {
            return '<polyline points="' + stroke.map(function (p) { return p[0] + "," + p[1]; }).join(" ") + '" fill="none" stroke="' +
              obj.color + '" stroke-width="' + obj.width + '" stroke-linecap="round" stroke-linejoin="round" opacity="' + obj.opacity + '"/>';
          }).join("") + "</svg>";
        break;
      case "note":
        node.innerHTML = ICONS.comment;
        node.style.background = obj.color;
        node.title = obj.text || "Comment";
        break;
    }
    if (selected && selected.id === obj.id) {
      node.classList.add("is-selected");
      addHandles(node, obj);
    }
    if (editing === obj.id) startEditingNode(node, obj, page);
    return node;
  }

  function addHandles(node, obj) {
    if (obj.type === "note" || editing === obj.id) return;
    if (obj.type === "line") {
      var box = bounds(obj);
      [["p1", obj.x1, obj.y1], ["p2", obj.x2, obj.y2]].forEach(function (end) {
        var handle = el("span", "pe-handle");
        handle.dataset.h = end[0];
        handle.style.left = (end[1] - box.x) * zoom + "px";
        handle.style.top = (end[2] - box.y) * zoom + "px";
        node.appendChild(handle);
      });
      return;
    }
    var textLike = obj.type === "text" || obj.type === "textedit";
    HANDLES.forEach(function (name) {
      if (textLike && (name === "n" || name === "s")) return;
      var handle = el("span", "pe-handle");
      handle.dataset.h = name;
      handle.style.left = (name.indexOf("w") >= 0 ? 0 : name.indexOf("e") >= 0 ? 100 : 50) + "%";
      handle.style.top = (name.indexOf("n") >= 0 ? 0 : name.indexOf("s") >= 0 ? 100 : 50) + "%";
      node.appendChild(handle);
    });
  }

  function select(target) {
    if (target && selected && target.id === selected.id) return;
    finishEditing();
    var previous = selected;
    selected = target;
    if (previous) { var p = findPage(previous.page); if (p) renderLayer(p); }
    if (target && (!previous || previous.page !== target.page)) renderLayer(findPage(target.page));
    renderProps();
  }

  function addObject(page, obj) {
    obj.id = uid();
    page.objects.push(obj);
    selected = { page: page.id, id: obj.id };
    renderLayer(page);
    renderProps();
    return obj;
  }

  function deleteSelected() {
    var obj = selectedObject();
    if (!obj) return;
    var page = findPage(selected.page);
    page.objects = page.objects.filter(function (item) { return item.id !== obj.id; });
    selected = null;
    editing = null;
    renderLayer(page);
    renderProps();
    commit();
  }

  function duplicateSelected() {
    var obj = selectedObject();
    if (!obj || obj.type === "textedit") return;
    var page = findPage(selected.page);
    var copy = JSON.parse(JSON.stringify(obj));
    translate(copy, 12, 12);
    addObject(page, copy);
    commit();
  }

  function reorderSelected(toFront) {
    var obj = selectedObject();
    if (!obj) return;
    var page = findPage(selected.page);
    page.objects = page.objects.filter(function (item) { return item.id !== obj.id; });
    if (toFront) page.objects.push(obj); else page.objects.unshift(obj);
    renderLayer(page);
    commit();
  }

  function translate(obj, dx, dy) {
    if (obj.type === "line") { obj.x1 += dx; obj.x2 += dx; obj.y1 += dy; obj.y2 += dy; return; }
    if (obj.type === "ink") {
      obj.points = obj.points.map(function (stroke) { return stroke.map(function (p) { return [round(p[0] + dx), round(p[1] + dy)]; }); });
    }
    obj.x = round(obj.x + dx);
    obj.y = round(obj.y + dy);
  }

  // ------------------------------------------------------------ text editing
  function startEditingNode(node, obj, page) {
    node.classList.add("is-editing");
    node.contentEditable = "plaintext-only";
    if (node.contentEditable !== "plaintext-only") node.contentEditable = "true";
    node.spellcheck = true;
    node.addEventListener("input", function () {
      obj.text = node.innerText.replace(/\n$/, "");
    });
    node.addEventListener("paste", function (event) {
      event.preventDefault();
      var text = (event.clipboardData || window.clipboardData).getData("text/plain");
      document.execCommand("insertText", false, text);
    });
    node.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { event.preventDefault(); finishEditing(); }
      event.stopPropagation();
    });
    node.addEventListener("blur", function () {
      setTimeout(function () {
        // Changing the font or color in the side panel keeps the text open for typing.
        var inPanel = Date.now() - propsTouched < 500 || props.contains(document.activeElement);
        if (editing === obj.id && !inPanel) finishEditing();
      }, 0);
    });
    requestAnimationFrame(function () {
      node.focus();
      var range = null;
      if (caretAt) {
        // Put the caret where the user clicked, like Acrobat.
        if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(caretAt.x, caretAt.y);
        else if (document.caretPositionFromPoint) {
          var position = document.caretPositionFromPoint(caretAt.x, caretAt.y);
          if (position) { range = document.createRange(); range.setStart(position.offsetNode, position.offset); }
        }
        if (range && !node.contains(range.startContainer)) range = null;
        caretAt = null;
      }
      if (!range) { range = document.createRange(); range.selectNodeContents(node); range.collapse(false); }
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    });
  }

  function editObject(page, obj) {
    finishEditing();
    selected = { page: page.id, id: obj.id };
    editing = obj.id;
    renderLayer(page);
    renderProps();
  }

  function finishEditing() {
    if (!editing) return;
    var id = editing;
    editing = null;
    var page = null, obj = null;
    for (var i = 0; i < state.pages.length && !obj; i++) {
      page = state.pages[i];
      obj = findObject(page.id, id);
    }
    if (!obj) return;
    var node = slots[page.id] && slots[page.id].page.querySelector('[data-id="' + id + '"]');
    if (node) {
      obj.text = node.innerText.replace(/\n$/, "");
      // Remember the size the text took, so the PDF gets the same box.
      obj.w = round(Math.max(node.offsetWidth / zoom, 8));
      obj.h = round(Math.max(node.offsetHeight / zoom, obj.size * 1.2));
    }
    if (!obj.text.trim() && obj.type === "text") {
      page.objects = page.objects.filter(function (item) { return item.id !== id; });
      if (selected && selected.id === id) selected = null;
    }
    renderLayer(page);
    renderProps();
    commit();
  }

  function loadText(src) {
    if (textCache[src]) return Promise.resolve(textCache[src]);
    textCache[src] = fetch(base + "/page/" + src + "/text", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : { lines: [] }; })
      .then(function (data) { textCache[src] = data.lines; return data.lines; })
      .catch(function () { delete textCache[src]; return []; });
    return textCache[src];
  }

  function loadAllText() {
    var wanted = {};
    state.pages.forEach(function (page) { if (typeof page.src === "number") wanted[page.src] = true; });
    Object.keys(wanted).forEach(function (src) {
      if (Array.isArray(textCache[src])) return;
      loadText(Number(src)).then(function () {
        state.pages.forEach(function (page) { if (page.src === Number(src) && slots[page.id]) renderLayer(page); });
      });
    });
  }

  function editLine(page, lineIndex) {
    var line = textCache[page.src][lineIndex];
    var existing = page.objects.filter(function (obj) { return obj.origKey === lineIndex; })[0];
    if (existing) { editObject(page, existing); return; }
    var obj = {
      type: "textedit", x: line.x, y: line.y, w: line.w, h: line.h, text: line.text,
      size: line.size, color: line.color, font: line.font, bold: line.bold, italic: line.italic, align: "left",
      autoWidth: true, origKey: lineIndex, orig: { x: line.x, y: line.y, w: line.w, h: line.h },
    };
    obj.id = uid();
    page.objects.push(obj);
    editObject(page, obj);
  }

  // ------------------------------------------------------------ pointer interaction
  var gesture = null;
  var propsTouched = 0;
  props.addEventListener("pointerdown", function () { propsTouched = Date.now(); }, true);

  docEl.addEventListener("pointerdown", function (event) {
    if (event.button !== 0 && event.pointerType === "mouse") return;
    var pageNode = event.target.closest(".pe-page");
    if (!pageNode) { if (!event.target.closest(".pe-obj")) select(null); return; }
    var page = findPage(pageNode.dataset.page);
    var point = toPoint(event, page);
    var objNode = event.target.closest(".pe-obj");
    var handle = event.target.closest(".pe-handle");

    if (objNode && editing === objNode.dataset.id) return; // Typing: let the caret move.

    if (pending) {
      event.preventDefault();
      placePending(page, point);
      return;
    }

    if (handle && objNode) {
      event.preventDefault();
      startGesture(event, page, { kind: "resize", handle: handle.dataset.h, obj: findObject(page.id, objNode.dataset.id), start: point });
      return;
    }

    if (objNode && tool !== "draw") {
      var obj = findObject(page.id, objNode.dataset.id);
      if (!obj) return;
      event.preventDefault();
      var textLike = obj.type === "text" || obj.type === "textedit";
      if (textLike && (tool === "edittext" || tool === "text" || (selected && selected.id === obj.id && event.detail >= 2))) {
        caretAt = { x: event.clientX, y: event.clientY };
        editObject(page, obj);
        return;
      }
      select({ page: page.id, id: obj.id });
      startGesture(event, page, { kind: "move", obj: obj, start: point, moved: false });
      return;
    }

    var lineNode = event.target.closest(".pe-line");
    if (lineNode && tool === "edittext") {
      event.preventDefault();
      caretAt = { x: event.clientX, y: event.clientY };
      editLine(page, Number(lineNode.dataset.line));
      return;
    }

    switch (tool) {
      case "select":
      case "edittext":
        select(null);
        return;
      case "text":
        event.preventDefault();
        var text = Object.assign({ type: "text", x: round(point.x), y: round(point.y - defaults.text.size * 0.6), w: 20, h: defaults.text.size * 1.2, text: "", autoWidth: true }, defaults.text);
        text.id = uid();
        page.objects.push(text);
        editObject(page, text);
        return;
      case "note":
        event.preventDefault();
        addObject(page, { type: "note", x: round(point.x), y: round(point.y), text: "", color: defaults.note.color });
        commit();
        setTimeout(function () { var area = props.querySelector("textarea"); if (area) area.focus(); }, 0);
        return;
      case "draw":
        event.preventDefault();
        select(null);
        startGesture(event, page, { kind: "ink", points: [[round(point.x), round(point.y)]] });
        return;
      case "shape":
      case "highlight":
      case "whiteout":
      case "redact":
        event.preventDefault();
        select(null);
        startGesture(event, page, { kind: "box", start: point, end: point, line: lineNode ? Number(lineNode.dataset.line) : null });
        return;
    }
  });

  function startGesture(event, page, data) {
    data.page = page;
    data.pointer = event.pointerId;
    data.node = slots[page.id].page;
    gesture = data;
    try { data.node.setPointerCapture(event.pointerId); } catch (error) {}
    if (data.kind === "ink" || data.kind === "box") {
      data.draft = el("div", data.kind === "box" ? "pe-draft" : "pe-obj is-ink");
      data.node.appendChild(data.draft);
      if (data.kind === "ink") updateInkDraft(data);
    }
  }

  function updateInkDraft(data) {
    var temp = { type: "ink", points: [data.points], color: defaults.draw.color, width: defaults.draw.width, opacity: defaults.draw.opacity };
    inkBounds(temp);
    var node = objectElement(temp, data.page);
    data.draft.replaceWith(node);
    data.draft = node;
  }

  window.addEventListener("pointermove", function (event) {
    if (!gesture || event.pointerId !== gesture.pointer) return;
    var page = gesture.page;
    var point = toPoint(event, page);
    if (gesture.kind === "move") {
      var dx = point.x - gesture.start.x, dy = point.y - gesture.start.y;
      if (!gesture.moved && Math.hypot(dx, dy) * zoom < 3) return;
      gesture.moved = true;
      translate(gesture.obj, dx, dy);
      gesture.start = point;
      redrawObject(page, gesture.obj);
    } else if (gesture.kind === "resize") {
      resize(gesture.obj, gesture.handle, point, event.shiftKey);
      redrawObject(page, gesture.obj);
    } else if (gesture.kind === "ink") {
      var last = gesture.points[gesture.points.length - 1];
      if (Math.hypot(point.x - last[0], point.y - last[1]) * zoom < 1.5) return;
      gesture.points.push([round(point.x), round(point.y)]);
      updateInkDraft(gesture);
    } else if (gesture.kind === "box") {
      gesture.end = point;
      var box = normalize(gesture.start, point);
      if (tool === "shape" && defaults.shape.kind !== "rect" && defaults.shape.kind !== "ellipse") {
        gesture.draft.style.borderStyle = "none";
        gesture.draft.style.background = "none";
        gesture.draft.innerHTML = '<svg viewBox="' + [box.x, box.y, Math.max(box.w, .1), Math.max(box.h, .1)].join(" ") + '" preserveAspectRatio="none"><line x1="' +
          gesture.start.x + '" y1="' + gesture.start.y + '" x2="' + point.x + '" y2="' + point.y + '" stroke="#155eef" stroke-width="2" stroke-dasharray="4 3"/></svg>';
      }
      place(gesture.draft, box.x, box.y, box.w, box.h);
    }
  });

  function endGesture(event) {
    if (!gesture || event.pointerId !== gesture.pointer) return;
    var data = gesture;
    gesture = null;
    var page = data.page;
    if (data.draft) data.draft.remove();
    if (data.kind === "move" || data.kind === "resize") {
      if (data.kind === "resize" && (data.obj.type === "text" || data.obj.type === "textedit")) {
        var node = slots[page.id].page.querySelector('[data-id="' + data.obj.id + '"]');
        if (node) data.obj.h = round(node.offsetHeight / zoom);
      }
      renderLayer(page);
      renderProps();
      commit();
    } else if (data.kind === "ink") {
      if (data.points.length < 2) data.points.push([data.points[0][0] + 0.5, data.points[0][1] + 0.5]);
      var ink = { type: "ink", points: [data.points], color: defaults.draw.color, width: defaults.draw.width, opacity: defaults.draw.opacity };
      inkBounds(ink);
      ink.id = uid();
      page.objects.push(ink);
      renderLayer(page);
      commit();
    } else if (data.kind === "box") {
      finishBox(page, data);
    }
  }
  window.addEventListener("pointerup", endGesture);
  window.addEventListener("pointercancel", endGesture);

  function normalize(a, b) {
    return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) };
  }

  function finishBox(page, data) {
    var box = normalize(data.start, data.end);
    var tiny = box.w * zoom < 4 && box.h * zoom < 4;
    if (tool === "highlight" && tiny) {
      if (data.line === null || typeof page.src !== "number") return;
      var line = textCache[page.src][data.line];
      box = { x: line.x, y: line.y, w: line.w, h: line.h };
    } else if (tiny) {
      if (tool === "shape") {
        var size = 80;
        box = { x: data.start.x - size / 2, y: data.start.y - size / 2, w: size, h: size };
        if (defaults.shape.kind !== "rect" && defaults.shape.kind !== "ellipse") { box.y = data.start.y; box.h = 0; }
      } else {
        return;
      }
    }
    box = { x: round(box.x), y: round(box.y), w: round(box.w), h: round(box.h) };
    var obj;
    if (tool === "shape") {
      var shape = defaults.shape;
      if (shape.kind === "line" || shape.kind === "arrow") {
        var end = tiny ? { x: data.start.x + 100, y: data.start.y } : data.end;
        obj = { type: "line", x1: round(data.start.x), y1: round(data.start.y), x2: round(end.x), y2: round(end.y), arrow: shape.kind === "arrow", color: shape.color, width: shape.width, opacity: shape.opacity };
      } else {
        obj = Object.assign({ type: shape.kind, color: shape.color, fill: shape.fill, width: shape.width, opacity: shape.opacity }, box);
      }
    } else if (tool === "highlight") {
      obj = Object.assign({ type: "highlight", color: defaults.highlight.color }, box);
    } else {
      obj = Object.assign({ type: tool }, box);
    }
    addObject(page, obj);
    commit();
  }

  function redrawObject(page, obj) {
    var old = slots[page.id].page.querySelector('[data-id="' + obj.id + '"]');
    var node = objectElement(obj, page);
    if (old) old.replaceWith(node); else slots[page.id].page.appendChild(node);
  }

  function resize(obj, handle, point, keepRatio) {
    if (obj.type === "line") {
      if (handle === "p1") { obj.x1 = round(point.x); obj.y1 = round(point.y); }
      else { obj.x2 = round(point.x); obj.y2 = round(point.y); }
      return;
    }
    var x0 = obj.x, y0 = obj.y, x1 = obj.x + obj.w, y1 = obj.y + obj.h;
    var before = { x: obj.x, y: obj.y, w: obj.w, h: obj.h };
    if (handle.indexOf("w") >= 0) x0 = Math.min(point.x, x1 - 4);
    if (handle.indexOf("e") >= 0) x1 = Math.max(point.x, x0 + 4);
    if (handle.indexOf("n") >= 0) y0 = Math.min(point.y, y1 - 4);
    if (handle.indexOf("s") >= 0) y1 = Math.max(point.y, y0 + 4);
    var w = x1 - x0, h = y1 - y0;
    var corner = handle.length === 2;
    if ((obj.type === "image" || obj.type === "signature") && corner !== !!keepRatio) {
      var ratio = before.w / before.h;
      if (w / h > ratio) w = h * ratio; else h = w / ratio;
      if (handle.indexOf("w") >= 0) x0 = x1 - w;
      if (handle.indexOf("n") >= 0) y0 = y1 - h;
    }
    if (obj.type === "ink") {
      var sx = w / (before.w || 1), sy = h / (before.h || 1);
      obj.points = obj.points.map(function (stroke) {
        return stroke.map(function (p) { return [round(x0 + (p[0] - before.x) * sx), round(y0 + (p[1] - before.y) * sy)]; });
      });
    }
    if (obj.type === "text" || obj.type === "textedit") obj.autoWidth = false;
    obj.x = round(x0); obj.y = round(y0); obj.w = round(w); obj.h = round(h);
  }

  // ------------------------------------------------------------ images and signatures
  function readImage(file) {
    return new Promise(function (resolve, reject) {
      if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { reject(new Error("Choose a PNG, JPG, WebP, or GIF picture.")); return; }
      var reader = new FileReader();
      reader.onerror = function () { reject(new Error("Could not read this picture.")); };
      reader.onload = function () {
        var img = new Image();
        img.onerror = function () { reject(new Error("Could not read this picture.")); };
        img.onload = function () {
          var max = 2400;
          var scale = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
          if (scale === 1 && file.size < 4 * 1024 * 1024 && file.type !== "image/webp" && file.type !== "image/gif") {
            resolve({ data: reader.result, ratio: img.naturalWidth / img.naturalHeight });
            return;
          }
          var c = document.createElement("canvas");
          c.width = Math.round(img.naturalWidth * scale);
          c.height = Math.round(img.naturalHeight * scale);
          c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
          var jpeg = file.type === "image/jpeg";
          resolve({ data: c.toDataURL(jpeg ? "image/jpeg" : "image/png", 0.9), ratio: c.width / c.height });
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  function addAsset(data) {
    var id = uid();
    state.assets[id] = data;
    return id;
  }

  function placePending(page, point) {
    var width = Math.min(page.w * 0.4, pending.kind === "signature" ? 180 : 240);
    var height = width / pending.ratio;
    var obj = { type: pending.kind, assetId: pending.assetId, x: round(point.x - width / 2), y: round(point.y - height / 2), w: round(width), h: round(height) };
    pending = null;
    app.classList.remove("is-placing");
    tool = "select";
    app.dataset.tool = "select";
    railState();
    showHint();
    addObject(page, obj);
    commit();
  }

  imageInput.addEventListener("change", function () {
    var file = imageInput.files[0];
    if (!file) { if (tool === "image") setTool("select"); return; }
    readImage(file).then(function (result) {
      pending = { kind: "image", assetId: addAsset(result.data), ratio: result.ratio };
      app.classList.add("is-placing");
      showHint();
    }).catch(function (error) { toast(escapeHtml(error.message), true); setTool("select"); });
  });

  // Drop a picture straight onto a page.
  docEl.addEventListener("dragover", function (event) {
    if (!event.dataTransfer || Array.prototype.indexOf.call(event.dataTransfer.types, "Files") < 0) return;
    event.preventDefault();
    var pageNode = event.target.closest(".pe-page");
    docEl.querySelectorAll(".is-drop-target").forEach(function (node) { if (node !== pageNode) node.classList.remove("is-drop-target"); });
    if (pageNode) pageNode.classList.add("is-drop-target");
  });
  docEl.addEventListener("dragleave", function (event) {
    var pageNode = event.target.closest(".pe-page");
    if (pageNode && !pageNode.contains(event.relatedTarget)) pageNode.classList.remove("is-drop-target");
  });
  docEl.addEventListener("drop", function (event) {
    var pageNode = event.target.closest(".pe-page");
    docEl.querySelectorAll(".is-drop-target").forEach(function (node) { node.classList.remove("is-drop-target"); });
    if (!pageNode || !event.dataTransfer.files.length) return;
    event.preventDefault();
    var page = findPage(pageNode.dataset.page);
    var point = toPoint(event, page);
    readImage(event.dataTransfer.files[0]).then(function (result) {
      pending = { kind: "image", assetId: addAsset(result.data), ratio: result.ratio };
      placePending(page, point);
    }).catch(function (error) { toast(escapeHtml(error.message), true); });
  });
  ["dragover", "drop"].forEach(function (type) {
    window.addEventListener(type, function (event) { if (!event.target.closest || !event.target.closest(".pe-page")) event.preventDefault(); });
  });

  // Signature pad
  var signatureDialog = document.getElementById("pe-signature");
  var pad = document.getElementById("pe-signature-pad");
  var padCtx = pad.getContext("2d");
  var inkColor = "#111111";
  var padStrokes = [];

  function savedSignature() { try { return localStorage.getItem("pe-signature"); } catch (error) { return null; } }

  function resetPad() {
    var ratio = window.devicePixelRatio || 1;
    pad.width = pad.clientWidth * ratio || 560 * ratio;
    pad.height = pad.clientHeight * ratio || 200 * ratio;
    padCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
    padCtx.lineCap = "round";
    padCtx.lineJoin = "round";
    padCtx.lineWidth = 2.6;
    padStrokes = [];
  }

  function openSignature() {
    document.getElementById("pe-signature-saved").hidden = !savedSignature();
    if (signatureDialog.showModal) signatureDialog.showModal(); else signatureDialog.setAttribute("open", "");
    requestAnimationFrame(resetPad);
  }

  pad.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    pad.setPointerCapture(event.pointerId);
    var box = pad.getBoundingClientRect();
    var stroke = [[event.clientX - box.left, event.clientY - box.top]];
    padStrokes.push(stroke);
    padCtx.strokeStyle = inkColor;
    padCtx.beginPath();
    padCtx.moveTo(stroke[0][0], stroke[0][1]);
    padCtx.lineTo(stroke[0][0] + 0.1, stroke[0][1] + 0.1);
    padCtx.stroke();
    pad.onpointermove = function (move) {
      var x = move.clientX - box.left, y = move.clientY - box.top;
      stroke.push([x, y]);
      padCtx.lineTo(x, y);
      padCtx.stroke();
    };
  });
  pad.addEventListener("pointerup", function () { pad.onpointermove = null; });
  pad.addEventListener("pointercancel", function () { pad.onpointermove = null; });
  document.getElementById("pe-signature-clear").addEventListener("click", function () {
    padCtx.clearRect(0, 0, pad.width, pad.height);
    padStrokes = [];
  });
  signatureDialog.querySelectorAll(".pe-swatch").forEach(function (swatch) {
    swatch.addEventListener("click", function () {
      inkColor = swatch.dataset.color;
      signatureDialog.querySelectorAll(".pe-swatch").forEach(function (s) {
        s.classList.toggle("is-active", s === swatch);
        s.setAttribute("aria-pressed", s === swatch ? "true" : "false");
      });
    });
  });

  function useSignature(data, ratio) {
    pending = { kind: "signature", assetId: addAsset(data), ratio: ratio };
    signatureDialog.close();
    tool = "signature";
    app.dataset.tool = "signature";
    app.classList.add("is-placing");
    railState();
    showHint();
  }

  document.getElementById("pe-signature-use").addEventListener("click", function () {
    if (!padStrokes.length) { toast("Draw your signature first.", true); return; }
    // Crop to the ink so the signature has no empty margins.
    var xs = [], ys = [];
    padStrokes.forEach(function (s) { s.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); }); });
    var ratio = window.devicePixelRatio || 1, pad8 = 8;
    var x0 = Math.max(0, Math.min.apply(null, xs) - pad8), y0 = Math.max(0, Math.min.apply(null, ys) - pad8);
    var x1 = Math.min(pad.clientWidth, Math.max.apply(null, xs) + pad8), y1 = Math.min(pad.clientHeight, Math.max.apply(null, ys) + pad8);
    var out = document.createElement("canvas");
    out.width = Math.max(1, Math.round((x1 - x0) * ratio));
    out.height = Math.max(1, Math.round((y1 - y0) * ratio));
    out.getContext("2d").drawImage(pad, x0 * ratio, y0 * ratio, out.width, out.height, 0, 0, out.width, out.height);
    var data = out.toDataURL("image/png");
    try { localStorage.setItem("pe-signature", data); } catch (error) {}
    useSignature(data, out.width / out.height);
  });
  document.getElementById("pe-signature-saved").addEventListener("click", function () {
    var data = savedSignature();
    if (!data) return;
    var img = new Image();
    img.onload = function () { useSignature(data, img.naturalWidth / img.naturalHeight); };
    img.src = data;
  });
  signatureDialog.addEventListener("close", function () { if (!pending && tool === "signature") setTool("select"); });

  // ------------------------------------------------------------ properties panel
  function field(label, control) {
    var wrap = el("label", "pe-field");
    wrap.appendChild(document.createTextNode(label));
    wrap.appendChild(control);
    return wrap;
  }
  function row() {
    var node = el("div", "pe-field-row");
    Array.prototype.slice.call(arguments).forEach(function (child) { node.appendChild(child); });
    return node;
  }
  function input(type, value, onChange, attrs) {
    var node = el("input");
    node.type = type;
    node.value = value;
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    node.addEventListener(type === "color" || type === "range" ? "input" : "change", function () { onChange(type === "number" || type === "range" ? Number(node.value) : node.value, false); });
    if (type === "color" || type === "range") node.addEventListener("change", function () { onChange(type === "range" ? Number(node.value) : node.value, true); });
    return node;
  }
  function selectBox(options, value, onChange) {
    var node = el("select");
    options.forEach(function (option) {
      var item = el("option", "", option[1]);
      item.value = option[0];
      if (String(option[0]) === String(value)) item.selected = true;
      node.appendChild(item);
    });
    node.addEventListener("change", function () { onChange(node.value); });
    return node;
  }
  function segment(options, value, onChange) {
    var node = el("div", "pe-segment");
    options.forEach(function (option) {
      var button = el("button");
      button.type = "button";
      button.innerHTML = option[1];
      button.title = option[2] || "";
      button.setAttribute("aria-pressed", option[0] === value ? "true" : "false");
      button.addEventListener("click", function () { onChange(option[0]); });
      node.appendChild(button);
    });
    return node;
  }
  function swatches(colors, value, onChange) {
    var node = el("div", "pe-swatches");
    colors.forEach(function (color) {
      var button = el("button", "pe-swatch");
      button.type = "button";
      button.style.setProperty("--swatch", color);
      button.setAttribute("aria-label", color);
      button.setAttribute("aria-pressed", color.toLowerCase() === String(value).toLowerCase() ? "true" : "false");
      button.addEventListener("click", function () { onChange(color); });
      node.appendChild(button);
    });
    return node;
  }
  function button(label, onClick, className) {
    var node = el("button", "pe-text-button" + (className ? " " + className : ""), label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  // Apply a property change to the selected object (if any) and to the tool's defaults.
  function change(group, key, value, done) {
    if (defaults[group]) defaults[group][key] = value;
    var obj = selectedObject();
    if (obj) {
      if (key === "kind") return;
      obj[key] = value;
      if (editing === obj.id) {
        var node = slots[selected.page].page.querySelector('[data-id="' + obj.id + '"]');
        if (node) textStyle(node, obj);
        return;
      }
      redrawObject(findPage(selected.page), obj);
      if (done !== false) commit();
    }
  }

  function textControls(target, group) {
    var frag = document.createDocumentFragment();
    frag.appendChild(row(
      field("Font", selectBox([["sans", "Sans serif"], ["serif", "Serif"], ["mono", "Monospace"]], target.font, function (v) { change(group, "font", v); renderProps(); })),
      field("Size", input("number", target.size, function (v) { change(group, "size", clamp(v || 12, 4, 200)); renderProps(); }, { min: 4, max: 200, step: 0.5 }))
    ));
    var styleRow = el("div", "pe-field-row");
    styleRow.appendChild(field("Style", segment([["bold", "<b>B</b>", "Bold"], ["italic", "<i>I</i>", "Italic"]], target.bold ? "bold" : target.italic ? "italic" : "", function (v) {
      change(group, v, !target[v]);
      renderProps();
    })));
    styleRow.appendChild(field("Align", segment([["left", "⇤", "Left"], ["center", "≡", "Center"], ["right", "⇥", "Right"]], target.align || "left", function (v) {
      change(group, "align", v);
      renderProps();
    })));
    frag.appendChild(styleRow);
    // Mark both bold and italic correctly in the style segment.
    var segButtons = styleRow.querySelectorAll(".pe-segment")[0].querySelectorAll("button");
    segButtons[0].setAttribute("aria-pressed", target.bold ? "true" : "false");
    segButtons[1].setAttribute("aria-pressed", target.italic ? "true" : "false");
    frag.appendChild(field("Color", input("color", target.color, function (v, done) { change(group, "color", v, done); })));
    frag.appendChild(swatches(["#111111", "#e5484d", "#155eef", "#12a150", "#f5a524", "#ffffff"], target.color, function (v) { change(group, "color", v); renderProps(); }));
    return frag;
  }

  function strokeControls(target, group, withFill) {
    var frag = document.createDocumentFragment();
    frag.appendChild(field("Color", input("color", target.color, function (v, done) { change(group, "color", v, done); })));
    frag.appendChild(swatches(["#111111", "#e5484d", "#155eef", "#12a150", "#f5a524", "#8e4ec6"], target.color, function (v) { change(group, "color", v); renderProps(); }));
    frag.appendChild(el("div", "pe-spacer"));
    var widthField = field("Line width: " + target.width + " pt", input("range", target.width, function (v, done) {
      widthField.firstChild.textContent = "Line width: " + v + " pt";
      change(group, "width", v, done);
    }, { min: 0.5, max: 20, step: 0.5 }));
    widthField.style.marginTop = "12px";
    frag.appendChild(widthField);
    if (withFill) {
      var fillWrap = el("div", "pe-field-row");
      var noFill = el("label", "pe-check");
      var box = el("input");
      box.type = "checkbox";
      box.checked = !!target.fill;
      box.addEventListener("change", function () { change(group, "fill", box.checked ? "#dbe7ff" : ""); renderProps(); });
      noFill.appendChild(box);
      noFill.appendChild(document.createTextNode("Fill"));
      fillWrap.appendChild(noFill);
      if (target.fill) fillWrap.appendChild(input("color", target.fill, function (v, done) { change(group, "fill", v, done); }));
      frag.appendChild(fillWrap);
    }
    var opacityField = field("Opacity: " + Math.round(target.opacity * 100) + "%", input("range", target.opacity, function (v, done) {
      opacityField.firstChild.textContent = "Opacity: " + Math.round(v * 100) + "%";
      change(group, "opacity", v, done);
    }, { min: 0.1, max: 1, step: 0.05 }));
    frag.appendChild(opacityField);
    return frag;
  }

  function objectActions(obj) {
    var wrap = el("div", "pe-actions");
    if (obj.type === "text" || obj.type === "textedit") wrap.appendChild(button("Edit text", function () { editObject(findPage(selected.page), obj); }));
    if (obj.type !== "textedit") wrap.appendChild(button("Duplicate", duplicateSelected));
    wrap.appendChild(button("Bring to front", function () { reorderSelected(true); }));
    wrap.appendChild(button("Send to back", function () { reorderSelected(false); }));
    wrap.appendChild(button(obj.type === "textedit" ? "Undo text change" : "Delete", deleteSelected, "danger"));
    return wrap;
  }

  var TITLES = {
    text: "Text", textedit: "Edited text", image: "Image", signature: "Signature", rect: "Rectangle", ellipse: "Ellipse",
    line: "Line", ink: "Drawing", highlight: "Highlight", whiteout: "Whiteout", redact: "Redaction", note: "Comment",
  };

  function renderProps() {
    props.textContent = "";
    var obj = selectedObject();
    var head = el("div", "pe-panel-head");
    props.appendChild(head);

    if (panel === "document") {
      head.appendChild(el("h2", "", "Watermark & page numbers"));
      documentControls();
      props.classList.remove("is-empty");
      return;
    }

    if (obj) {
      head.appendChild(el("h2", "", TITLES[obj.type] || "Item"));
      head.appendChild(button("Done", function () { select(null); }));
      if (obj.type === "text" || obj.type === "textedit") props.appendChild(textControls(obj, "text"));
      if (obj.type === "textedit") props.appendChild(el("p", "pe-panel-hint", "The original line is replaced when you download."));
      if (obj.type === "ink") props.appendChild(strokeControls(obj, "draw", false));
      if (obj.type === "rect" || obj.type === "ellipse") props.appendChild(strokeControls(obj, "shape", true));
      if (obj.type === "line") {
        props.appendChild(el("label", "pe-check")).append((function () {
          var box = el("input"); box.type = "checkbox"; box.checked = !!obj.arrow;
          box.addEventListener("change", function () { change("shape", "arrow", box.checked); });
          return box;
        })(), document.createTextNode(" Arrowhead"));
        props.appendChild(strokeControls(obj, "shape", false));
      }
      if (obj.type === "highlight") {
        props.appendChild(el("h3", "", "Color"));
        props.appendChild(swatches(["#ffd60a", "#7ee081", "#7cc4ff", "#ff9bd2", "#ffb057"], obj.color, function (v) { change("highlight", "color", v); renderProps(); }));
      }
      if (obj.type === "note") {
        var area = el("textarea");
        area.value = obj.text;
        area.placeholder = "Type your comment";
        area.addEventListener("input", function () { obj.text = area.value; });
        area.addEventListener("change", function () { redrawObject(findPage(selected.page), obj); commit(); });
        props.appendChild(field("Comment", area));
        props.appendChild(swatches(["#ffd60a", "#7ee081", "#7cc4ff", "#ff9bd2"], obj.color, function (v) { change("note", "color", v); renderProps(); }));
      }
      if (obj.type === "redact") props.appendChild(el("p", "pe-note", "Text, pictures and drawings under this box are permanently removed from the downloaded PDF."));
      if (obj.type === "image" || obj.type === "signature") props.appendChild(el("p", "pe-panel-hint", "Drag a corner to resize. Hold Shift to resize freely."));
      props.appendChild(el("h3", "", "Arrange"));
      props.appendChild(objectActions(obj));
      props.classList.remove("is-empty");
      return;
    }

    var item = TOOLS.filter(function (t) { return t.id === tool; })[0];
    head.appendChild(el("h2", "", item ? item.label : "Edit PDF"));
    if (tool === "text") props.appendChild(textControls(defaults.text, "text"));
    if (tool === "draw") props.appendChild(strokeControls(defaults.draw, "draw", false));
    if (tool === "shape") {
      props.appendChild(field("Shape", segment([["rect", "▭", "Rectangle"], ["ellipse", "◯", "Ellipse"], ["line", "╱", "Line"], ["arrow", "↗", "Arrow"]], defaults.shape.kind, function (v) {
        defaults.shape.kind = v;
        renderProps();
      })));
      props.appendChild(strokeControls(defaults.shape, "shape", defaults.shape.kind === "rect" || defaults.shape.kind === "ellipse"));
    }
    if (tool === "highlight") props.appendChild(swatches(["#ffd60a", "#7ee081", "#7cc4ff", "#ff9bd2", "#ffb057"], defaults.highlight.color, function (v) { defaults.highlight.color = v; renderProps(); }));
    if (tool === "redact") props.appendChild(el("p", "pe-note", "Redaction permanently removes everything under the black boxes when you download. Check the result before sharing."));
    if (item && item.help) props.appendChild(el("p", "pe-panel-hint", item.help));
    if (tool === "select") {
      props.appendChild(el("p", "pe-panel-hint", "Pick a tool on the left to start editing. Select an item to change its look."));
      props.appendChild(el("h3", "", "Shortcuts"));
      props.appendChild(el("p", "pe-panel-hint", "⌘Z undo · ⇧⌘Z redo · Delete removes · ⌘D duplicates · arrows nudge · Esc finishes"));
    }
    props.classList.toggle("is-empty", tool === "select" || tool === "edittext" || tool === "image" || tool === "signature" || tool === "note" || tool === "whiteout");
  }

  function documentControls() {
    var mark = state.watermark;
    var numbers = state.pageNumbers;
    function update(target, key, value, done) {
      target[key] = value;
      state.pages.forEach(function (page, index) { renderLayer(page, index); });
      if (done !== false) commit();
    }
    function toggle(label, target) {
      var wrap = el("label", "pe-check");
      var box = el("input");
      box.type = "checkbox";
      box.checked = target.enabled;
      box.addEventListener("change", function () { update(target, "enabled", box.checked); renderProps(); });
      wrap.appendChild(box);
      wrap.appendChild(document.createTextNode(label));
      return wrap;
    }
    props.appendChild(el("h3", "", "Watermark"));
    props.appendChild(toggle("Add a watermark", mark));
    if (mark.enabled) {
      props.appendChild(field("Text", input("text", mark.text, function (v) { update(mark, "text", v.slice(0, 200)); }, { maxlength: 200 })));
      props.appendChild(row(
        field("Size", input("number", mark.size, function (v) { update(mark, "size", clamp(v || 60, 8, 300)); }, { min: 8, max: 300 })),
        field("Angle", selectBox([[45, "Diagonal ↗"], [0, "Flat"], [-45, "Diagonal ↘"]], mark.angle, function (v) { update(mark, "angle", Number(v)); }))
      ));
      props.appendChild(row(
        field("Color", input("color", mark.color, function (v, done) { update(mark, "color", v, done); })),
        field("Position", selectBox([["over", "On top"], ["under", "Behind text"]], mark.layer, function (v) { update(mark, "layer", v); }))
      ));
      var op = field("Opacity: " + Math.round(mark.opacity * 100) + "%", input("range", mark.opacity, function (v, done) {
        op.firstChild.textContent = "Opacity: " + Math.round(v * 100) + "%";
        update(mark, "opacity", v, done);
      }, { min: 0.05, max: 1, step: 0.05 }));
      props.appendChild(op);
    }
    props.appendChild(el("h3", "", "Page numbers"));
    props.appendChild(toggle("Number the pages", numbers));
    if (numbers.enabled) {
      props.appendChild(field("Format", selectBox([["{n}", "1"], ["Page {n}", "Page 1"], ["Page {n} of {total}", "Page 1 of 9"], ["{n} / {total}", "1 / 9"], ["- {n} -", "- 1 -"]], numbers.format, function (v) { update(numbers, "format", v); })));
      props.appendChild(field("Position", selectBox([
        ["bottom-center", "Bottom center"], ["bottom-right", "Bottom right"], ["bottom-left", "Bottom left"],
        ["top-center", "Top center"], ["top-right", "Top right"], ["top-left", "Top left"],
      ], numbers.position, function (v) { update(numbers, "position", v); })));
      props.appendChild(row(
        field("Size", input("number", numbers.size, function (v) { update(numbers, "size", clamp(v || 10, 6, 48)); }, { min: 6, max: 48 })),
        field("Color", input("color", numbers.color, function (v, done) { update(numbers, "color", v, done); }))
      ));
    }
  }

  // ------------------------------------------------------------ pages panel
  var thumbDrag = null;

  function currentPageIndex() {
    var top = canvas.getBoundingClientRect().top + canvas.clientHeight * 0.35;
    var best = 0;
    state.pages.forEach(function (page, index) {
      var box = slots[page.id] && slots[page.id].slot.getBoundingClientRect();
      if (box && box.top <= top) best = index;
    });
    return best;
  }

  function updatePageInfo() {
    if (!state.pages.length) return;
    var index = currentPageIndex();
    pageInfo.textContent = "Page " + (index + 1) + " of " + state.pages.length;
    if (panel === "pages") {
      thumbs.querySelectorAll(".pe-thumb").forEach(function (node, i) { node.classList.toggle("is-current", i === index); });
    }
  }

  function renderThumbs() {
    thumbs.textContent = "";
    state.pages.forEach(function (page, index) {
      var item = el("li", "pe-thumb");
      item.draggable = true;
      item.dataset.index = index;
      var frame = el("div", "pe-thumb-frame");
      var sheet = el("div", "pe-thumb-page");
      var scale = Math.min(120 / (turned(page) ? page.h : page.w), 140 / (turned(page) ? page.w : page.h));
      sheet.style.width = page.w * scale + "px";
      sheet.style.height = page.h * scale + "px";
      sheet.style.transform = "rotate(" + rotation(page) + "deg)";
      if (typeof page.src === "number") {
        var img = el("img");
        img.alt = "";
        img.loading = "lazy";
        img.draggable = false;
        img.src = base + "/page/" + page.src + ".png?w=400";
        sheet.appendChild(img);
      }
      frame.appendChild(sheet);
      frame.addEventListener("click", function () { slots[page.id].slot.scrollIntoView({ behavior: "smooth", block: "start" }); });
      item.appendChild(frame);
      var bar = el("div", "pe-thumb-row");
      bar.appendChild(el("span", "pe-thumb-number", String(index + 1)));
      var actions = el("div", "pe-thumb-actions");
      [
        ["rotateLeft", "Rotate left", function () { page.rotate = rotation(page) - 90; afterPageChange(); }],
        ["rotateRight", "Rotate right", function () { page.rotate = rotation(page) + 90; afterPageChange(); }],
        ["duplicate", "Duplicate page", function () {
          var copy = JSON.parse(JSON.stringify(page));
          copy.id = uid();
          copy.objects.forEach(function (obj) { obj.id = uid(); });
          state.pages.splice(index + 1, 0, copy);
          afterPageChange();
        }],
        ["trash", "Delete page", function () {
          if (state.pages.length < 2) return;
          state.pages.splice(index, 1);
          if (selected && selected.page === page.id) selected = null;
          afterPageChange();
        }],
      ].forEach(function (spec) {
        var b = el("button");
        b.type = "button";
        b.innerHTML = ICONS[spec[0]];
        b.title = spec[1];
        b.setAttribute("aria-label", spec[1] + " " + (index + 1));
        if (spec[0] === "trash") b.disabled = state.pages.length < 2;
        b.addEventListener("click", spec[2]);
        actions.appendChild(b);
      });
      bar.appendChild(actions);
      item.appendChild(bar);
      thumbs.appendChild(item);
    });
    updatePageInfo();
  }

  function afterPageChange() {
    renderAll();
    commit();
  }

  thumbs.addEventListener("dragstart", function (event) {
    var item = event.target.closest(".pe-thumb");
    if (!item) return;
    thumbDrag = Number(item.dataset.index);
    item.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(thumbDrag));
  });
  thumbs.addEventListener("dragover", function (event) {
    if (thumbDrag === null) return;
    event.preventDefault();
    thumbs.querySelectorAll(".is-drop-before, .is-drop-after").forEach(function (node) { node.classList.remove("is-drop-before", "is-drop-after"); });
    var item = event.target.closest(".pe-thumb");
    if (!item) return;
    var box = item.getBoundingClientRect();
    item.classList.add(event.clientY < box.top + box.height / 2 ? "is-drop-before" : "is-drop-after");
  });
  thumbs.addEventListener("drop", function (event) {
    if (thumbDrag === null) return;
    event.preventDefault();
    var item = event.target.closest(".pe-thumb");
    var from = thumbDrag;
    thumbDrag = null;
    if (!item) { renderThumbs(); return; }
    var to = Number(item.dataset.index) + (item.classList.contains("is-drop-after") ? 1 : 0);
    if (to > from) to -= 1;
    if (to !== from) { state.pages.splice(to, 0, state.pages.splice(from, 1)[0]); afterPageChange(); }
    else renderThumbs();
  });
  thumbs.addEventListener("dragend", function () { thumbDrag = null; renderThumbs(); });

  document.getElementById("pe-add-page").addEventListener("click", function () {
    var index = currentPageIndex();
    var near = state.pages[index];
    state.pages.splice(index + 1, 0, { id: uid(), src: null, w: near ? near.w : 595.28, h: near ? near.h : 841.89, rotate: 0, objects: [] });
    afterPageChange();
    var added = state.pages[index + 1];
    setTimeout(function () { slots[added.id].slot.scrollIntoView({ behavior: "smooth", block: "start" }); }, 50);
  });

  // ------------------------------------------------------------ zoom
  function computeFit() {
    var widest = 0;
    state.pages.forEach(function (page) { widest = Math.max(widest, turned(page) ? page.h : page.w); });
    var available = canvas.clientWidth - (window.innerWidth < 700 ? 20 : 48);
    fitZoom = clamp(available / (widest || 612), 0.2, 3);
  }

  function setZoom(value, anchor) {
    var old = zoom;
    zoom = clamp(value, 0.2, 5);
    var focus = anchor || { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 };
    var px = (canvas.scrollLeft + focus.x) / old, py = (canvas.scrollTop + focus.y) / old;
    document.getElementById("pe-zoom-fit").textContent = Math.round(zoom * 100) + "%";
    state.pages.forEach(function (page, index) { if (slots[page.id]) layoutPage(page, index); });
    canvas.scrollLeft = px * zoom - focus.x;
    canvas.scrollTop = py * zoom - focus.y;
  }

  document.getElementById("pe-zoom-in").addEventListener("click", function () { setZoom(zoom * 1.25); });
  document.getElementById("pe-zoom-out").addEventListener("click", function () { setZoom(zoom / 1.25); });
  document.getElementById("pe-zoom-fit").addEventListener("click", function () { computeFit(); setZoom(fitZoom); });
  canvas.addEventListener("wheel", function (event) {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    var box = canvas.getBoundingClientRect();
    setZoom(zoom * Math.exp(-event.deltaY * 0.0025), { x: event.clientX - box.left, y: event.clientY - box.top });
  }, { passive: false });
  var scrollTimer = null;
  canvas.addEventListener("scroll", function () {
    pageInfo.style.opacity = 1;
    updatePageInfo();
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () { pageInfo.style.opacity = 0; }, 1200);
  }, { passive: true });

  // ------------------------------------------------------------ keyboard
  document.addEventListener("keydown", function (event) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) || event.target.isContentEditable;
    var mod = event.metaKey || event.ctrlKey;
    if (mod && event.key.toLowerCase() === "z" && !typing) { event.preventDefault(); if (event.shiftKey) redo(); else undo(); return; }
    if (mod && event.key.toLowerCase() === "y" && !typing) { event.preventDefault(); redo(); return; }
    if (mod && event.key.toLowerCase() === "s") { event.preventDefault(); download("download"); return; }
    if (typing) return;
    if (mod && event.key.toLowerCase() === "d") { event.preventDefault(); duplicateSelected(); return; }
    if (event.key === "Escape") {
      if (pending) { pending = null; app.classList.remove("is-placing"); }
      if (selected) select(null); else setTool("select");
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && selected) { event.preventDefault(); deleteSelected(); return; }
    if (event.key === "Enter" && selected) {
      var obj = selectedObject();
      if (obj && (obj.type === "text" || obj.type === "textedit")) { event.preventDefault(); editObject(findPage(selected.page), obj); }
      return;
    }
    var nudge = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (nudge && selected) {
      event.preventDefault();
      var step = event.shiftKey ? 10 : 1;
      var target = selectedObject();
      translate(target, nudge[0] * step, nudge[1] * step);
      redrawObject(findPage(selected.page), target);
      commit();
      return;
    }
    if (!mod && !event.altKey) {
      var match = TOOLS.filter(function (t) { return t.key === event.key.toLowerCase(); })[0];
      if (match) setTool(match.id);
    }
  });

  document.getElementById("pe-undo").addEventListener("click", undo);
  document.getElementById("pe-redo").addEventListener("click", redo);

  // ------------------------------------------------------------ saving the PDF
  function exportState() {
    var pages = state.pages.map(function (page) {
      return Object.assign({}, page, {
        objects: page.objects.map(function (obj) {
          return obj.assetId ? Object.assign({}, obj, { data: state.assets[obj.assetId] }) : obj;
        }),
      });
    });
    return { pages: pages, watermark: state.watermark, pageNumbers: state.pageNumbers };
  }

  function download(target) {
    finishEditing();
    var downloadButton = document.getElementById("pe-download");
    var filesButton = document.getElementById("pe-save-files");
    downloadButton.disabled = filesButton.disabled = true;
    toast(target === "files" ? "Saving to Files…" : "Preparing your PDF…");
    var name = nameInput.value.trim() || info.name;
    fetch(base + "/save", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ name: name, target: target, state: exportState() }),
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().catch(function () { return {}; }).then(function (data) { throw new Error(data.error || "Could not build the PDF (error " + response.status + ")."); });
        }
        if (target === "files") {
          return response.json().then(function (data) {
            toast("Saved “" + escapeHtml(data.saved) + "” in Files. <a href=\"" + app.dataset.filesUrl + "\">Open Files</a>");
          });
        }
        return response.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var link = document.createElement("a");
          link.href = url;
          link.download = name.replace(/\.pdf$/i, "") + "-edited.pdf";
          document.body.appendChild(link);
          link.click();
          link.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
          toast("Downloaded " + escapeHtml(link.download));
        });
      })
      .catch(function (error) { toast(escapeHtml(error.message === "Failed to fetch" ? "Could not reach the server." : error.message), true); })
      .then(function () { downloadButton.disabled = filesButton.disabled = false; });
  }

  document.getElementById("pe-download").addEventListener("click", function () { download("download"); });
  document.getElementById("pe-save-files").addEventListener("click", function () { download("files"); });
  nameInput.addEventListener("keydown", function (event) { if (event.key === "Enter") nameInput.blur(); });

  // ------------------------------------------------------------ start
  app.dataset.tool = tool;
  railState();
  computeFit();
  zoom = fitZoom;
  document.getElementById("pe-zoom-fit").textContent = Math.round(zoom * 100) + "%";
  renderAll();
  showHint();
  setStatus(info.state ? "Restored your earlier edits" : "");
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      var wasFit = Math.abs(zoom - fitZoom) < 0.01;
      computeFit();
      if (wasFit) setZoom(fitZoom);
    }, 150);
  });
})();
