/* Reads a document file ON THIS DEVICE and returns the TEXT of each page. Nothing is uploaded: the PDF reader and the OCR
   engine are downloaded as code, the file itself never leaves the browser. Only the text is later sent to the server,
   which reads it again in code (the browser is not trusted) and never stores it.

   - PDF with a text layer: the text items are grouped into lines by their position (pdf.js).
   - PDF page with (almost) no text, or a photo / scan: the page is drawn to a canvas and read by Tesseract.js.
   Each page is labelled "text" or "ocr", because OCR is trusted less by the server.

   Works in the browser (window.ChaukasDocRead) and, for the pure line-grouping function, in Node (for tests). */
(function (root) {
  "use strict";

  /* ---------- pure: pdf.js text items -> lines (tested in Node) ----------
     An item is { str, transform: [a,b,c,d,x,y], width, height }. Items on the same baseline form a line; a big gap between
     two items becomes three spaces (a table column), a small gap one space, no gap nothing (pdf.js often splits a word). */
  function groupItems(items) {
    const its = (items || []).filter(i => i && typeof i.str === "string" && i.str.trim() !== "" && i.transform)
      .map(i => ({ s: i.str, x: i.transform[4], y: i.transform[5], w: i.width || 0, h: Math.abs(i.height || i.transform[3] || 10) || 10 }));
    its.sort((a, b) => b.y - a.y || a.x - b.x);
    const lines = [];
    for (const it of its) {
      const tol = Math.max(2, it.h * 0.45);
      const ln = lines.find(l => Math.abs(l.y - it.y) <= tol);
      if (ln) ln.items.push(it); else lines.push({ y: it.y, items: [it] });
    }
    lines.sort((a, b) => b.y - a.y);
    return lines.map(l => {
      l.items.sort((a, b) => a.x - b.x);
      let out = "", end = null, em = Math.max(4, l.items.reduce((m, i) => Math.max(m, i.h), 0) * 0.5);
      for (const it of l.items) {
        if (end !== null) {
          const gap = it.x - end;
          out += gap > em * 2.4 ? "   " : gap > em * 0.25 || /\w$/.test(out) && /^\w/.test(it.s) && gap > 0.5 ? " " : "";
        }
        out += it.s;
        end = it.x + it.w;
      }
      return out.replace(/[ \t]+$/g, "");
    }).filter(Boolean).join("\n");
  }

  if (typeof window === "undefined") { module.exports = { groupItems }; return; }

  /* ---------- browser only ---------- */
  const PDFJS_SRC = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js";
  const PDFJS_WORKER = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
  const TESS_SRC = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
  const MAX_FILE = 20 * 1024 * 1024;

  function loadScript(src, ready, failMsg) {
    if (ready()) return Promise.resolve();
    return new Promise((res, rej) => {
      const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = () => rej(new Error(failMsg));
      document.head.append(s);
    });
  }
  const loadPdf = () => loadScript(PDFJS_SRC, () => window.pdfjsLib, "Could not load the PDF reader. Check your connection.")
    .then(() => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER; });
  const loadTess = () => loadScript(TESS_SRC, () => window.Tesseract, "Could not load the text reader. Check your connection.");

  /* grayscale + contrast, scaled so the page is about 2000px wide: OCR needs letters of about 30px */
  function prep(source, w, h) {
    const sc = Math.min(2.5, 2000 / Math.max(w, 1));
    const c = document.createElement("canvas"); c.width = Math.round(w * sc); c.height = Math.round(h * sc);
    const g = c.getContext("2d"); g.filter = "grayscale(1) contrast(1.35)"; g.drawImage(source, 0, 0, c.width, c.height);
    return c;
  }
  async function imageCanvas(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("That file is not a readable photo.")); i.src = url; });
      return prep(img, img.width, img.height);
    } finally { URL.revokeObjectURL(url); }
  }

  let WORKER = null;
  async function ocr(canvas, onProgress) {
    await loadTess();
    if (!WORKER) WORKER = await Tesseract.createWorker("eng", 1, { logger: m => { if (m.progress != null && onProgress) onProgress("Reading the page", m.progress); } });
    /* psm 6 (one block) suits a bill's table; if it finds almost no letters, try psm 4 (columns of varying size) */
    let text = "";
    for (const psm of ["6", "4"]) {
      await WORKER.setParameters({ tessedit_pageseg_mode: psm });
      text = (await WORKER.recognize(canvas)).data.text || "";
      if ((text.match(/[A-Za-z]{3,}/g) || []).length >= 8) break;
    }
    canvas.width = canvas.height = 0;
    return text;
  }
  async function endOcr() { if (WORKER) { try { await WORKER.terminate(); } catch (e) {} WORKER = null; } }

  /* One file -> [{ text, source }] pages. cfg = { max_pages, min_text_chars_per_page } from the server. */
  async function readOne(file, cfg, onProgress, room) {
    if (file.size > MAX_FILE) throw new Error("\"" + file.name + "\" is too large. Please use a file under 20 MB.");
    const pages = [];
    if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) {
      onProgress("Opening the PDF", 0); await loadPdf();
      const pdf = await pdfjsLib.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
      const n = Math.min(pdf.numPages, room);
      if (pdf.numPages > room) throw new Error("Too many pages. You can upload up to " + cfg.max_pages + " pages in all.");
      for (let p = 1; p <= n; p++) {
        onProgress("Reading page " + p + " of " + n, (p - 1) / n);
        const page = await pdf.getPage(p);
        const text = groupItems((await page.getTextContent()).items);
        if (text.replace(/\s/g, "").length >= cfg.min_text_chars_per_page) { pages.push({ text, source: "text" }); continue; }
        const vp = page.getViewport({ scale: 2 });          /* a scanned page: draw it, then OCR it */
        const cv = document.createElement("canvas"); cv.width = vp.width; cv.height = vp.height;
        await page.render({ canvasContext: cv.getContext("2d"), viewport: vp }).promise;
        const text2 = await ocr(prep(cv, cv.width, cv.height), (s, f) => onProgress("Scanned page " + p + " of " + n, f));
        cv.width = cv.height = 0;
        pages.push({ text: text2, source: "ocr" });
      }
      return pages;
    }
    if (/^image\//.test(file.type)) {
      if (room < 1) throw new Error("Too many pages. You can upload up to " + cfg.max_pages + " pages in all.");
      onProgress("Preparing the photo", 0);
      pages.push({ text: await ocr(await imageCanvas(file), onProgress), source: "ocr" });
      return pages;
    }
    throw new Error("\"" + file.name + "\" is not a PDF or a photo (JPG, PNG).");
  }

  /* files: FileList / array. Returns { pages: [text], page_sources: ["text"|"ocr"] } for one document (all files = its pages). */
  async function readFiles(files, cfg, onProgress) {
    onProgress = onProgress || function () {};
    const pages = [];
    try {
      for (const f of Array.from(files)) pages.push(...await readOne(f, cfg, onProgress, cfg.max_pages - pages.length));
    } finally { await endOcr(); }
    return { pages: pages.map(p => p.text), page_sources: pages.map(p => p.source) };
  }

  root.ChaukasDocRead = { readFiles, groupItems };
})(typeof window !== "undefined" ? window : globalThis);
