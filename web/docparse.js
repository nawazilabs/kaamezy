/* Turns OCR text from a PAN card or Aadhaar card into a few form fields.
   Pure, deterministic code: no network, no LLM. The 12-digit Aadhaar number is detected only so it can be
   ignored; it is never returned. Works in the browser (window.ChaukasDoc) and in Node (for tests). */
(function (root) {
  "use strict";
  const L2D = { O: "0", I: "1", L: "1", S: "5", B: "8", Z: "2", G: "6" };
  const D2L = { "0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G" };

  /* PAN is 5 letters, 4 digits, 1 letter. OCR confuses look-alikes, so fix each position by its expected class. */
  function fixPan(tok) {
    if (tok.length !== 10) return null;
    const out = [...tok].map((ch, i) => {
      const letterPos = i < 5 || i === 9;
      if (letterPos) return /[A-Z]/.test(ch) ? ch : (D2L[ch] || ch);
      return /[0-9]/.test(ch) ? ch : (L2D[ch] || ch);
    }).join("");
    return /^[A-Z]{5}[0-9]{4}[A-Z]$/.test(out) ? out : null;
  }

  function titleCase(s) { return s.toLowerCase().replace(/\b[a-z]/g, c => c.toUpperCase()); }
  const NOT_A_NAME = /government|india|aadhaar|aadhar|unique|identification|authority|income|tax|department|permanent|account|number|card|signature|father|mother|male|female|dob|birth|address|vid/i;

  function ageOn(y, m, d, now) {
    let a = now.getFullYear() - y;
    if (now.getMonth() + 1 < m || (now.getMonth() + 1 === m && now.getDate() < d)) a--;
    return a;
  }

  function parse(text, now) {
    now = now || new Date();
    const raw = String(text || "");
    const up = raw.toUpperCase();
    const res = { pan: null, name: null, dob: null, age: null, gender: null, aadhaarSeen: false };

    /* Aadhaar number: spotted so we can warn, then thrown away. */
    if (/\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b/.test(raw)) res.aadhaarSeen = true;

    for (const tok of (up.match(/\b[A-Z0-9]{10}\b/g) || [])) { const p = fixPan(tok); if (p) { res.pan = p; break; } }

    const dre = /\b(0?[1-9]|[12]\d|3[01])[\/\-.](0?[1-9]|1[0-2])[\/\-.]((?:19|20)\d{2})\b/g;
    let dm;
    while ((dm = dre.exec(raw))) {
      const d = +dm[1], m = +dm[2], y = +dm[3];
      const real = new Date(y, m - 1, d);
      if (real.getFullYear() !== y || real.getMonth() !== m - 1 || real.getDate() !== d) continue; /* e.g. 31/02 */
      const a = ageOn(y, m, d, now);
      if (a >= 0 && a <= 110) { res.dob = y + "-" + String(m).padStart(2, "0") + "-" + String(d).padStart(2, "0"); res.age = a; break; }
    }

    if (/\bFEMALE\b/.test(up)) res.gender = "F"; else if (/\bMALE\b/.test(up)) res.gender = "M";

    const lines = raw.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    const nameOk = l => /^[A-Za-z][A-Za-z .'-]{2,40}$/.test(l) && !NOT_A_NAME.test(l) && l.split(/\s+/).length <= 5;
    /* PAN card: the line after a "Name" label (not "Father's Name"). */
    let i = lines.findIndex(l => /name\s*$/i.test(l) && !/father|mother/i.test(l));
    if (i >= 0) { const c = lines.slice(i + 1, i + 3).find(nameOk); if (c) res.name = titleCase(c); }
    /* Aadhaar card: the name sits just above the date of birth line. */
    if (!res.name) {
      const j = lines.findIndex(l => /dob|date of birth|year of birth/i.test(l) || /\b\d{2}[\/\-.]\d{2}[\/\-.]\d{4}\b/.test(l));
      if (j > 0) for (let k = j - 1; k >= Math.max(0, j - 3); k--) if (nameOk(lines[k])) { res.name = titleCase(lines[k]); break; }
    }
    return res;
  }

  const api = { parse, fixPan };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.ChaukasDoc = api;
})(typeof window !== "undefined" ? window : globalThis);
