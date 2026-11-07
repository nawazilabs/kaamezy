/* App language. The fixed wording of the app is translated ahead of time (scripts/build_i18n.py, Sarvam Translate) into
   web/i18n/<code>.json as {"English text": "translated text"}.

   The page is never sent anywhere. A text node, placeholder, label or title is swapped only when its text EQUALS a key
   exactly, so anything built at run time (names, amounts, quotes, PAN, the assistant's replies) can never be changed or
   leaked by this file. English is always the fallback: a string with no translation just stays English.

   window.ChaukasI18n: { code, list(), set(code), onChange(fn), ready } */
(function () {
  "use strict";
  const KEY = "chaukas_lang", EN = "en-IN";
  const SKIP = /^(SCRIPT|STYLE|TEXTAREA|CODE|PRE|INPUT|SELECT)$/;
  const ATTRS = ["placeholder", "aria-label", "title", "alt"];
  const RTL = new Set(["ur-IN"]);
  const store = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };

  let dict = null, code = EN, langs = [{ code: EN, native: "English", english: "English" }];
  const files = new Map(), orig = new WeakMap(), listeners = [];
  const norm = s => s.replace(/\s+/g, " ").trim();
  const skipped = el => { for (let e = el; e; e = e.parentElement) { if (SKIP.test(e.tagName) || (e.getAttribute && e.getAttribute("translate") === "no")) return true; } return false; };

  /* the English a node held before we translated it (if the app wrote new text since, that new text is the English) */
  const englishOf = n => { const o = orig.get(n); return o && n.nodeValue === o.tr ? o.en : n.nodeValue; };

  function text(n) {
    const v = n.nodeValue; if (v == null) return;
    const en = englishOf(n), key = norm(en), tr = dict && dict[key];
    if (tr) {
      const lead = en.match(/^\s*/)[0], trail = en.match(/\s*$/)[0], next = lead + tr + trail;
      orig.set(n, { en, tr: next }); if (v !== next) n.nodeValue = next;
    } else if (orig.has(n)) { orig.delete(n); if (v !== en) n.nodeValue = en; }
  }
  function attrs(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute(a); if (v == null) continue;
      const rec = orig.get(el) || {}, o = rec[a];
      const en = o && v === o.tr ? o.en : v, tr = dict && dict[norm(en)];
      if (tr) { rec[a] = { en, tr }; orig.set(el, rec); if (v !== tr) el.setAttribute(a, tr); }
      else if (o) { delete rec[a]; if (v !== en) el.setAttribute(a, en); }
    }
  }
  function apply(root) {
    if (!root) return;
    if (root.nodeType === 3) { if (!skipped(root.parentElement)) text(root); return; }
    if (root.nodeType !== 1 || skipped(root)) return;
    attrs(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: n => n.nodeType === 1 ? (skipped(n) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP) : NodeFilter.FILTER_ACCEPT });
    const texts = []; for (let n = w.nextNode(); n; n = w.nextNode()) texts.push(n);
    texts.forEach(text);
    root.querySelectorAll("[placeholder],[aria-label],[title],[alt]").forEach(e => { if (!skipped(e)) attrs(e); });
  }

  const obs = new MutationObserver(ms => {
    for (const m of ms) {
      if (m.type === "childList") m.addedNodes.forEach(apply);
      else if (m.type === "characterData") { if (!skipped(m.target.parentElement)) text(m.target); }
      else if (m.type === "attributes" && !skipped(m.target)) attrs(m.target);
    }
  });
  const watch = () => obs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  async function load(c) {
    if (files.has(c)) return files.get(c);
    try {
      const r = await fetch("i18n/" + c + ".json", { cache: "no-cache" });
      if (!r.ok) throw new Error(String(r.status));
      files.set(c, await r.json());
    } catch (e) { files.set(c, null); }
    return files.get(c);
  }

  async function set(c) {
    c = c || EN;
    const d = c === EN ? null : await load(c);
    if (c !== EN && !d) return false;     /* no file for that language: stay where we are */
    dict = d; code = c; store.set(KEY, c);
    const root = document.documentElement; root.lang = c === EN ? "en" : c; root.dir = RTL.has(c) ? "rtl" : "ltr";
    apply(document.body);
    listeners.forEach(f => { try { f(c); } catch (e) {} });
    return true;
  }

  const ready = (async function () {
    try { const r = await fetch("i18n/languages.json", { cache: "no-cache" }); if (r.ok) langs = await r.json(); } catch (e) {}
    watch();
    const saved = store.get(KEY);
    if (saved && saved !== EN && langs.some(l => l.code === saved)) await set(saved);
  })();

  window.ChaukasI18n = { get code() { return code; }, list: () => langs.slice(), set, onChange: f => listeners.push(f), ready };
})();
