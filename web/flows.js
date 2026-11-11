/* Health insurance + personal loan screens, wired to the backend (/api/consumer).
   The server is the source of truth for every number and every decision: premiums come from its rule engine, the
   lender's offer from its simulated lender, and a deterministic guardrail (chaukas/consumer/guardrail.py) decides
   ALLOW / OTP / human / BLOCK before any policy is issued or loan disbursed. The browser only shows what the server
   returned and sends what the user typed. PAN is sent for a format check and never stored; the Aadhaar number is never
   collected (KYC is a DigiLocker mock).    Uses helpers from index.html: $, el, toast, showScreen, icons, openSheet, closeSheets, API, sb. */
(function () {
  "use strict";

  /* ---------------- UI options only (everything that decides money lives on the server) ---------------- */
  const COVERS = [500000, 1000000, 2500000, 5000000];
  const COND = { diabetes: { n: "Diabetes" }, bp: { n: "High BP" }, heart: { n: "Heart condition" }, asthma: { n: "Asthma" }, thyroid: { n: "Thyroid" }, cancer: { n: "Cancer (past)" } };
  const RELS = ["Self", "Spouse", "Son", "Daughter", "Father", "Mother"];
  const RE = { pan: /^[A-Z]{5}[0-9]{4}[A-Z]$/, mobile: /^[6-9][0-9]{9}$/, pin: /^[0-9]{6}$/ };
  const DEMO_OTP = "123456";   /* shown as a hint only; the server checks the code */
  const maskPan = s => s && s.length === 10 ? "XXXXX" + s.slice(5) : "—";
  const maskMobile = s => s ? "XXXXXX" + String(s).slice(-4) : "—";
  const tok = (kind, s) => { let h = 5381; for (const ch of String(s || "")) h = ((h * 33) ^ ch.charCodeAt(0)) >>> 0; return "tok_" + kind + "_" + h.toString(16).padStart(8, "0"); };
  const ageBand = a => a <= 25 ? "up to 25" : a <= 35 ? "26–35" : a <= 45 ? "36–45" : a <= 55 ? "46–55" : a <= 65 ? "56–65" : "66+";

  const inr = n => "₹" + Number(n).toLocaleString("en-IN");
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const when = ts => new Date(ts).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  const day = iso => new Date(iso).toLocaleDateString("en-IN");
  const refresh = () => { try { icons(); } catch (e) {} };

  /* ---------------- backend ---------------- */
  async function capi(method, path, body, extra) {
    const headers = Object.assign({ "Content-Type": "application/json" }, extra || {});
    try { const s = await sb.auth.getSession(); const t = s && s.data && s.data.session && s.data.session.access_token; if (t) headers.Authorization = "Bearer " + t; } catch (e) {}
    const slow = setTimeout(() => toast("Server is waking up (free tier). This can take up to a minute…"), 6000);
    try {
      const r = await fetch(API + "/api/consumer" + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
      let data = null; try { data = await r.json(); } catch (e) {}
      return { ok: r.ok, status: r.status, data };
    } catch (e) { toast("Cannot reach the server: " + API); return { ok: false, status: 0, data: null }; }
    finally { clearTimeout(slow); }
  }
  const errMsg = r => (r.data && r.data.error && r.data.error.message) || (r.status === 0 ? "The server could not be reached." : "Something went wrong (HTTP " + r.status + ").");
  const listPolicies = async () => { const r = await capi("GET", "/policies"); return r.ok ? r.data : []; };

  /* ---------------- on-device document reader ----------------
     The photo is drawn to a canvas and read by an open-source OCR engine (Tesseract.js) running in this tab.
     Nothing is uploaded: the engine and its English model are downloaded as code, the photo never leaves the device.
     The text is parsed by deterministic code (docparse.js); the user confirms every value before it is used. */
  const TESS_SRC = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
  function loadTess() {
    if (window.Tesseract) return Promise.resolve();
    return new Promise((res, rej) => {
      const s = document.createElement("script"); s.src = TESS_SRC; s.onload = res;
      s.onerror = () => rej(new Error("Could not load the reader. Check your connection, or type the details."));
      document.head.append(s);
    });
  }
  async function prepImage(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("That file is not a readable photo.")); i.src = url; });
      const sc = Math.min(2.5, 1600 / Math.max(img.width, img.height));   /* small card photos are enlarged: OCR needs ~30px letters */
      const c = document.createElement("canvas"); c.width = Math.round(img.width * sc); c.height = Math.round(img.height * sc);
      const g = c.getContext("2d"); g.filter = "grayscale(1) contrast(1.3)"; g.drawImage(img, 0, 0, c.width, c.height);
      return c;
    } finally { URL.revokeObjectURL(url); }
  }
  async function readDoc(file, onProgress) {
    if (!/^image\//.test(file.type)) throw new Error("Please choose a photo (JPG or PNG) of the card.");
    if (file.size > 12 * 1024 * 1024) throw new Error("That photo is too large. Please use one under 12 MB.");
    const canvas = await prepImage(file);
    onProgress("Loading the reader", 0); await loadTess();
    const worker = await Tesseract.createWorker("eng", 1, { logger: m => { if (m.progress != null) onProgress(m.status, m.progress); } });
    /* Up to three reads of the same photo (normal layout, scattered text, black-and-white); each fills only what the
       earlier ones missed, and we stop as soon as name, age and an identity field are all found. */
    const bw = binarize(canvas), seen = [];
    try {
      let doc = null;
      /* Passes 4 and 5 are made only if the date is still missing: the lower part of the card (father's name, date of
         birth, in small print) is cut out and enlarged, normal and black-and-white. */
      const lower = () => lowerPart(canvas);
      const passes = [[() => canvas, "3"], [() => canvas, "11"], [() => bw, "6"], [lower, "6"], [() => binarize(lower()), "6"], [lower, "11"]];
      for (const [make, psm] of passes) {
        const cv = make();
        await worker.setParameters({ tessedit_pageseg_mode: psm });
        const text = (await worker.recognize(cv)).data.text; seen.push(maskForDisplay(text));
        const d = window.ChaukasDoc.parse(text);
        if (!doc) doc = d;
        else {
          for (const k of ["name", "gender", "pan"]) if (!doc[k]) doc[k] = d[k];
          if (doc.age == null && d.age != null) { doc.age = d.age; doc.dob = d.dob; doc.yearOnly = d.yearOnly; }
          doc.aadhaarSeen = doc.aadhaarSeen || d.aadhaarSeen;
        }
        if (doc.name && doc.age != null && (doc.pan || doc.gender)) break;
      }
      doc.seen = seen.join("\n— next read —\n");   /* shown on this screen only, with long numbers masked; never sent or stored */
      return doc;
    }
    finally { await worker.terminate(); canvas.width = canvas.height = 0; bw.width = bw.height = 0; }
  }
  /* the lower ~65% of the card, enlarged (small print needs big letters); a fresh canvas each call */
  function lowerPart(src) {
    const y0 = Math.round(src.height * 0.35), h = src.height - y0, sc = Math.min(2, 2400 / src.width);
    const c = document.createElement("canvas"); c.width = Math.round(src.width * sc); c.height = Math.round(h * sc);
    c.getContext("2d").drawImage(src, 0, y0, src.width, h, 0, 0, c.width, c.height);
    return c;
  }
  /* black-and-white copy (Otsu threshold): helps on cards with a coloured or patterned background */
  function binarize(src) {
    const c = document.createElement("canvas"); c.width = src.width; c.height = src.height;
    const g = c.getContext("2d"); g.drawImage(src, 0, 0);
    const im = g.getImageData(0, 0, c.width, c.height), px = im.data, hist = new Array(256).fill(0);
    for (let i = 0; i < px.length; i += 4) hist[px[i]]++;
    const total = px.length / 4; let sum = 0; for (let t = 0; t < 256; t++) sum += t * hist[t];
    let wB = 0, sB = 0, best = 0, thr = 128;
    for (let t = 0; t < 256; t++) { wB += hist[t]; if (!wB) continue; const wF = total - wB; if (!wF) break; sB += t * hist[t];
      const v = wB * wF * ((sB / wB) - ((sum - sB) / wF)) ** 2; if (v > best) { best = v; thr = t; } }
    for (let i = 0; i < px.length; i += 4) { const v = px[i] > thr ? 255 : 0; px[i] = px[i + 1] = px[i + 2] = v; }
    g.putImageData(im, 0, 0); return c;
  }
  const maskForDisplay = t => String(t)
    .replace(/\b\d{4}[ -]?\d{4}[ -]?\d{4}\b/g, "XXXX XXXX XXXX")
    .replace(/\b(?=[A-Za-z0-9]{10}\b)(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9]{10}\b/g, "XXXXXXXXXX");

  /* ---------------- markup (injected so index.html stays small) ---------------- */
  const back = (to) => `<button class="picon" data-back="${to}" aria-label="Back"><i data-lucide="arrow-left"></i></button>`;
  const bar = (to, title, sub, right) => `<header class="czbar">${back(to)}<div class="czlogo"><b>${title}</b><small>${sub}</small></div>${right || '<span style="width:2.25rem"></span>'}</header>`;
  const row = (id, ico, red, t, s) => `<button class="czrow" id="${id}"><span class="rico${red ? " red" : ""}"><i data-lucide="${ico}"></i></span><span class="rtxt"><b>${t}</b><span id="${id}-s">${s}</span></span><i data-lucide="chevron-right"></i></button>`;

  const HTML = `
<section class="flow" id="hi" hidden>
  ${bar("home", "Health Insurance", "powered by Kaamezy", '<button class="plink czhelp" id="hi-help">Help</button>')}
  <div class="czbody">
    <div class="czhero"><h1>Your health cover,<br><mark>bought right.</mark></h1>
      <ul>
        <li><i data-lucide="sparkles"></i>Tell us once, the agent does the paperwork</li>
        <li><i data-lucide="calculator"></i>Premiums from a rule engine, never guessed</li>
        <li><i data-lucide="shield-check"></i>Every purchase is guardrail-checked</li>
      </ul></div>
    <div class="czcard" id="hi-start"><h2>New here? Start with step 1</h2>
      <p>Answer a few questions, or walk through a full purchase with a made-up family. Nothing real is bought anywhere.</p>
      <button class="czbtn" id="hi-new"><i data-lucide="plus"></i> Get a quote</button>
      <button class="czbtn soft" id="hi-sample"><i data-lucide="circle-play"></i> Try with a sample family</button></div>
    <div class="czcard menu">
      ${row("hi-r-new", "plus", false, "Get a new quote", "Compare plans for your family")}
      ${row("hi-r-pol", "file-text", false, "My policies", "Add a policy to see it here")}
      ${row("hi-r-claims", "heart-pulse", false, "My claims", "File a claim, get cash before the payout")}
      ${row("hi-r-claim", "briefcase-medical", true, "How do I claim?", "Ask the assistant, in your language")}
      ${row("hi-r-act", "lock", false, "Privacy &amp; AI activity", "Consents and everything the AI did")}
    </div>
    <p class="czfoot">Nawazi Labs · demo. Quotes, insurers and policy issue are mocks; the rule engine and the guardrail are real code. Designed to align with IRDAI norms, RBI FREE-AI and the DPDP Act 2023.</p>
  </div>
  <button class="askfab" id="hi-ask" aria-label="Ask Kaamezy"><span class="avatar-b"><i data-lucide="shield-check"></i></span><b>Ask</b></button>
</section>

<section class="flow" id="hiw" hidden>
  ${bar("hi", "Get a quote", "Kaamezy does the paperwork")}
  <div class="steps" id="hiw-steps"></div><div class="steplabel" id="hiw-label"></div>
  <div class="czbody" id="hiw-body"></div>
</section>

<section class="flow" id="hip" hidden>
  ${bar("hi", "My policies", "issued through Kaamezy")}
  <div class="czbody" id="hip-body"></div>
</section>

<section class="flow" id="clm" hidden>
  ${bar("hi", "My claims", "simulated insurer and lender")}
  <div class="czbody" id="clm-body"></div>
</section>

<section class="flow" id="hia" hidden>
  ${bar("hi", "Privacy &amp; AI activity", "append-only, nothing can be edited")}
  <div class="czbody" id="hia-body"></div>
</section>

<section class="flow" id="ich" hidden aria-label="Kaamezy insurance assistant">
  <header class="chhead"><span class="avatar-b"><i data-lucide="shield-check"></i></span>
    <div class="chtitle"><strong>Kaamezy assistant</strong><small id="ic-sub">Insurance and loan help</small></div>
    <button class="picon" id="ic-new" aria-label="Clear chat" title="Clear chat"><i data-lucide="trash-2"></i></button>
    <button class="picon" id="ic-close" aria-label="Close"><i data-lucide="x"></i></button></header>
  <div class="ichat" id="ic-chat" aria-live="polite"></div>
  <div class="hints" id="ic-hints" style="background:#fff;padding:0 14px 8px"></div>
  <form class="send2" id="ic-form">
    <button type="button" class="roundb soft" id="ic-attach" aria-label="Attach a file"><i data-lucide="paperclip"></i></button>
    <input id="ic-text" placeholder="Ask about cover, claims, loans, EMI…" autocomplete="off" aria-label="Message">
    <button type="button" class="roundb soft" id="ic-mic" aria-label="Speak"><i data-lucide="mic"></i></button>
    <button class="roundb navy" aria-label="Send"><i data-lucide="send"></i></button></form>
  <button class="humanbtn" id="ic-human"><i data-lucide="headphones"></i> Talk to a human</button>
</section>

<section class="flow" id="ln" hidden>
  ${bar("home", "Loan", "powered by Kaamezy", '<button class="plink czhelp" id="ln-help">Help</button>')}
  <div class="czbody">
    <div class="czhero"><h1>Money when you need it,<br><mark>sorted right.</mark></h1>
      <ul>
        <li><i data-lucide="sparkles"></i>One form, the agent does the rest</li>
        <li><i data-lucide="scale"></i>A regulated lender decides, not us</li>
        <li><i data-lucide="receipt-text"></i>Every cost shown before you agree</li>
      </ul></div>
    <div class="czcard" id="ln-start"><h2>New here? Start with step 1</h2>
      <p>Tell us what you need and we check your offer. Or walk through a full loan with a made-up borrower. Nothing real is lent anywhere.</p>
      <button class="czbtn" id="ln-new"><i data-lucide="plus"></i> Check my offer</button>
      <button class="czbtn soft" id="ln-sample"><i data-lucide="circle-play"></i> Try with a sample borrower</button></div>
    <div class="czcard" id="ln-bridge" hidden><h2>Need cash while a claim is pending?</h2>
      <p><span id="ln-bridge-s"></span> Once your claim is approved you can ask for a short advance against it. It is repaid from the claim payout, and your policy never changes a personal loan.</p>
      <button class="czbtn soft" id="ln-bridge-btn"><i data-lucide="heart-pulse"></i> My claims and advance</button></div>
    <div class="czcard menu">
      ${row("ln-r-new", "plus", false, "Check a new offer", "See what you can borrow")}
      ${row("ln-r-loans", "file-text", false, "My loans", "Your loans will show up here")}
      ${row("ln-r-charges", "receipt-text", true, "What does it cost?", "APR, fees and EMI explained")}
      ${row("ln-r-act", "lock", false, "Privacy &amp; AI activity", "Consents and everything the AI did")}
    </div>
    <p class="czfoot">Nawazi Labs · demo. The lender, offer and disbursal are mocks; the guardrail is real code. The amount comes from the lender's rules, never from the AI. Designed to align with RBI's Digital Lending Directions, RBI FREE-AI and the DPDP Act 2023.</p>
  </div>
  <button class="askfab" id="ln-ask" aria-label="Ask Kaamezy"><span class="avatar-b"><i data-lucide="shield-check"></i></span><b>Ask</b></button>
</section>

<section class="flow" id="lnw" hidden>
  ${bar("ln", "Check my offer", "Kaamezy does the paperwork")}
  <div class="steps" id="lnw-steps"></div><div class="steplabel" id="lnw-label"></div>
  <div class="czbody" id="lnw-body"></div>
</section>

<section class="flow" id="lnl" hidden>
  ${bar("ln", "My loans", "issued through Kaamezy")}
  <div class="czbody" id="lnl-body"></div>
</section>

<div class="sheet" id="sheet-lnhelp" hidden role="dialog" aria-label="How it works">
  <h3>How the loan works</h3>
  <ol><li><b>Tell us what you need.</b> Amount, months, your income and EMIs.</li>
    <li><b>The lender decides.</b> Only age, income and EMIs count: never your name, gender, city or language.</li>
    <li><b>See every cost first.</b> The Key Facts Statement comes before you agree, with the lender's name.</li>
    <li><b>Guardrail checks, then money moves.</b> Big or risky cases need an OTP or a human to check income proof.</li></ol>
  <button class="czbtn" data-close>Got it</button>
</div>

<div class="sheet" id="sheet-hihelp" hidden role="dialog" aria-label="How it works">
  <h3>How buying works</h3>
  <ol><li><b>Tell us who to cover.</b> Ages and city, nothing else yet.</li>
    <li><b>Add health details.</b> Existing conditions change the premium, never hidden.</li>
    <li><b>Agent compares insurers.</b> A rule engine prices each plan, then the agent explains the pick.</li>
    <li><b>Guardrail checks, then policy issues.</b> Big or risky cases go to an OTP or a human underwriter.</li></ol>
  <button class="czbtn" data-close>Got it</button>
</div>`;

  /* ---------------- wizard ---------------- */
  const blank = () => ({ members: [{ rel: "Self", age: 32, gender: "M", ht: 170, wt: 70 }], pincode: "", city: "metro", proposer: { name: "", mobile: "" }, nominee: { name: "", rel: "Spouse" }, conds: [], smoker: false, surgery: false, cover: 1000000 });
  const SAMPLE = () => ({ members: [{ rel: "Self", age: 34, gender: "M", ht: 172, wt: 72 }, { rel: "Spouse", age: 32, gender: "F", ht: 160, wt: 58 }, { rel: "Son", age: 6, gender: "M", ht: 115, wt: 20 }], pincode: "400001", city: "metro", proposer: { name: "Demo Sharma", mobile: "9876543210" }, nominee: { name: "Demo Sharma Jr", rel: "Spouse" }, conds: [], smoker: false, surgery: false, cover: 1000000 });
  let W = blank(), STEP = 1, Q = null, QID = null, PICK = null, CONSENT = false, HANDOFF = null;
  let PAN = "";                       /* kept in memory only, never saved; shown masked */
  let KYC = { ok: false, ref: null }; /* display only: the server holds the real KYC state */

  function setSteps(n, label, pre) {
    pre = pre || "hiw";
    $(pre + "-steps").replaceChildren(...[1, 2, 3, 4].map(i => el("i", { class: i <= n ? "on" : "" })));
    $(pre + "-label").textContent = "Step " + n + " of 4 · " + label;
  }
  const chip = (text, pressed, onclick) => el("button", { type: "button", class: "cchip", "aria-pressed": pressed ? "true" : "false", text, onclick });

  function field(label, attrs) { return el("label", { class: "auth-field", style: "margin-top:.7rem" }, label, el("input", attrs)); }
  function memRow(m) {
    const num = (k, ph) => el("input", { type: "number", inputmode: "numeric", placeholder: ph, "aria-label": m.rel + " " + ph, value: m[k] == null ? "" : String(m[k]), oninput: e => { m[k] = e.target.value === "" ? null : Number(e.target.value); } });
    const sel = el("select", { "aria-label": m.rel + " gender", onchange: e => { m.gender = e.target.value; } }, el("option", { value: "M", text: "Male" }), el("option", { value: "F", text: "Female" }));
    sel.value = m.gender;
    return el("div", { class: "mem3" }, el("b", { text: m.rel }), el("div", { class: "g4" }, num("age", "Age"), sel, num("ht", "Ht cm"), num("wt", "Wt kg")));
  }
  function validStep1() {
    const bad = m => { toast(m); return false; };
    if ((W.proposer.name || "").trim().length < 2) return bad("Please enter your name.");
    if (!RE.mobile.test(W.proposer.mobile || "")) return bad("Enter a valid 10-digit mobile number.");
    if (!RE.pin.test(W.pincode || "")) return bad("Enter a valid 6-digit pincode.");
    if (W.members.some(m => m.age == null || m.age < 0 || m.age > 99)) return bad("Check the ages: each must be between 0 and 99.");
    return true;
  }
  function docBox(apply) {
    const box = el("div", { class: "docbox" }), status = el("div", { class: "docstatus", role: "status" });
    const say = (txt, seen) => {
      status.replaceChildren(txt);
      if (seen) status.append(el("details", { style: "margin-top:.5rem;font-size:.78rem" }, el("summary", { text: "What the reader saw (numbers masked)" }), el("pre", { style: "white-space:pre-wrap;max-height:11rem;overflow:auto;margin:.3rem 0", text: seen })));
    };
    const inp = el("input", { type: "file", accept: "image/*", hidden: "", onchange: async e => {
      const f = e.target.files[0]; e.target.value = ""; if (!f) return;
      try {
        say("Preparing the photo on your device…");
        const doc = await readDoc(f, (s, p) => say(String(s).replace(/^./, c => c.toUpperCase()) + " " + Math.round(p * 100) + "%"));
        const got = [doc.name && "name", doc.age != null && "date of birth", doc.gender && "gender", doc.pan && "PAN"].filter(Boolean);
        if (!got.length) { say(doc.aadhaarSeen ? "That looks like an Aadhaar card. We ignore the number, but could not read your name or date of birth from this photo. Try a bright, flat, closer photo of the front, or type the details." :"Could not read that clearly. Try a bright, flat photo, or type the details.", doc.seen); return; }
        const rows = [["Name", doc.name], ["Age", doc.age != null ? doc.age + (doc.yearOnly ? " (from year of birth)" : " (from date of birth)") : null], ["Gender", doc.gender ? (doc.gender === "F" ? "Female" : "Male") : null], ["PAN", doc.pan ? maskPan(doc.pan) : null]].filter(r => r[1]);
        const use = el("button", { type: "button", class: "czbtn", text: "Use these", onclick: () => {
          apply(doc);
          toast("Filled from your document. Please check each field.");
        } });
        const no = el("button", { type: "button", class: "czbtn soft", text: "Discard", onclick: () => say("Discarded. Nothing was kept.") });
        status.replaceChildren(el("div", { class: "ocrres" }, el("b", { text: "We read this on your device" }),
          ...rows.map(r => el("div", { class: "prow2" }, el("span", { text: r[0] }), el("code", { text: r[1] }))),
          doc.aadhaarSeen ? el("p", { class: "s", style: "margin:.4rem 0;font-size:.8rem", text: "An Aadhaar number was visible. It was ignored and not kept." }) : null,
          got.length < 3 ? el("p", { class: "s", style: "margin:.4rem 0;font-size:.8rem", text: "Some details could not be read. Fill the rest by hand, or try a brighter, closer photo." }) : null,
          el("div", { class: "navrow", style: "margin-top:.6rem" }, no, use),
          doc.seen ? el("details", { style: "margin-top:.5rem;font-size:.78rem" }, el("summary", { text: "What the reader saw (numbers masked)" }), el("pre", { style: "white-space:pre-wrap;max-height:11rem;overflow:auto;margin:.3rem 0", text: doc.seen })) : null));
      } catch (err) { say(err.message || "Could not read that photo."); }
    } });
    box.append(el("b", { text: "Autofill from your PAN or Aadhaar card" }),
      el("p", { class: "s", style: "margin:.3rem 0 .6rem;font-size:.82rem", text: "Take or choose a photo. It is read inside this browser tab and is never uploaded. A 12-digit Aadhaar number, if visible, is ignored. You confirm every value before it is used." }),
      el("label", { class: "czbtn soft", style: "cursor:pointer" }, inp, "Choose a photo"), status);
    return box;
  }
  function step1() {
    STEP = 1; setSteps(1, "Who to cover"); const b = $("hiw-body"); b.replaceChildren();
    b.append(el("h2", { class: "t", text: "Who needs cover?" }), el("p", { class: "s", text: "Pick everyone for the same plan. You are always included." }), docBox(d => { if (d.name) W.proposer.name = d.name; if (d.age != null) W.members[0].age = d.age; if (d.gender) W.members[0].gender = d.gender; if (d.pan) PAN = d.pan; step1(); }));
    const chips = el("div", { class: "chips" });
    RELS.forEach(r => chips.append(chip(r, W.members.some(m => m.rel === r), () => {
      if (r === "Self") return;
      const i = W.members.findIndex(m => m.rel === r);
      if (i >= 0) W.members.splice(i, 1); else W.members.push({ rel: r, age: r === "Son" || r === "Daughter" ? 8 : r === "Spouse" ? 30 : 58, gender: r === "Daughter" || r === "Spouse" || r === "Mother" ? "F" : "M", ht: null, wt: null });
      step1();
    })));
    b.append(chips, el("span", { class: "lbl", text: "Details for each person" }), el("p", { class: "s", style: "margin:0 0 .3rem;font-size:.82rem", text: "Age, gender, height and weight help price the plan. Height and weight are optional." }));
    W.members.forEach(m => b.append(memRow(m)));
    b.append(el("span", { class: "lbl", text: "About you (policy holder)" }),
      field("Full name", { value: W.proposer.name, autocomplete: "name", oninput: e => { W.proposer.name = e.target.value; } }),
      field("Mobile number", { value: W.proposer.mobile, type: "tel", inputmode: "numeric", maxlength: "10", autocomplete: "tel-national", oninput: e => { W.proposer.mobile = e.target.value.replace(/\D/g, ""); } }),
      field("Pincode", { value: W.pincode, type: "tel", inputmode: "numeric", maxlength: "6", autocomplete: "postal-code", oninput: e => { W.pincode = e.target.value.replace(/\D/g, ""); } }));
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn", text: "Next", onclick: () => { if (validStep1()) step2(); } })));
  }

  function step2() {
    STEP = 2; setSteps(2, "Health and cover"); const b = $("hiw-body"); b.replaceChildren();
    b.append(el("h2", { class: "t", text: "Health and cover" }), el("p", { class: "s", text: "Be honest here: hiding a condition is the most common reason claims get rejected later." }));
    b.append(el("span", { class: "lbl", text: "Any existing conditions in the family?" }));
    const cs = el("div", { class: "chips" });
    cs.append(chip("None", W.conds.length === 0, () => { W.conds = []; step2(); }));
    Object.keys(COND).forEach(k => cs.append(chip(COND[k].n, W.conds.includes(k), () => { const i = W.conds.indexOf(k); if (i >= 0) W.conds.splice(i, 1); else W.conds.push(k); step2(); })));
    b.append(cs, el("span", { class: "lbl", text: "Does anyone smoke or chew tobacco?" }), el("div", { class: "chips" },
      chip("No", !W.smoker, () => { W.smoker = false; step2(); }), chip("Yes", W.smoker, () => { W.smoker = true; step2(); })));
    b.append(el("span", { class: "lbl", text: "Any surgery or hospital stay in the last 4 years?" }), el("div", { class: "chips" },
      chip("No", !W.surgery, () => { W.surgery = false; step2(); }), chip("Yes", W.surgery, () => { W.surgery = true; step2(); })));
    b.append(el("span", { class: "lbl", text: "Cover amount" }));
    const cv = el("div", { class: "chips" });
    COVERS.forEach(c => cv.append(chip(c >= 10000000 ? "₹1 Cr" : "₹" + (c / 100000) + " L", W.cover === c, () => { W.cover = c; step2(); })));
    b.append(cv, el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: () => step1() }), el("button", { class: "czbtn", text: "Find plans", onclick: () => step3() })));
  }

  const proposalBody = () => ({
    members: W.members.map(m => ({ rel: m.rel, age: m.age, gender: m.gender, height_cm: m.ht || null, weight_kg: m.wt || null })),
    pincode: W.pincode, conditions: W.conds, smoker: W.smoker, surgery_last_4y: W.surgery, cover_inr: W.cover });

  function failBox(b, title, r, back, again) {
    b.append(el("div", { class: "gr BLOCK" }, el("h3", { text: title }), el("div", { class: "why2", text: errMsg(r) })),
      el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: back }), el("button", { class: "czbtn", text: "Try again", onclick: again })));
  }

  async function step3() {
    STEP = 3; setSteps(3, "Agent compares plans"); const b = $("hiw-body"); b.replaceChildren();
    b.append(el("h2", { class: "t", text: "Kaamezy is working" }), el("p", { class: "s", text: "Each line below is a real check in the rule engine." }));
    const list = el("div", { class: "agentlist" }); b.append(list);
    const req = capi("POST", "/insurance/quote", proposalBody());
    for (const n of ["Checking your KYC on file", "Applying eligibility rules", "Pricing 3 insurers with the rule engine", "Ranking plans for your family"]) {
      const r = el("div", { class: "arow" }, el("span", { class: "dot" }), el("span", { text: n })); list.append(r); await sleep(520); r.classList.add("done");
    }
    const res = await req;
    if (!res.ok) { failBox(b, "Couldn't get quotes", res, () => step2(), () => step3()); return; }
    QID = res.data.quote_id; W.city = res.data.city_tier; HANDOFF = null;
    Q = res.data.quotes.map(x => ({ id: x.insurer_id, name: x.name, tag: x.tag, pts: x.points, premium: x.premium_inr, monthly: x.monthly_inr, cover: x.cover_inr }));
    const rec = { id: res.data.recommended.insurer_id, why: res.data.recommended.why };
    PICK = PICK && Q.some(q => q.id === PICK) ? PICK : rec.id;
    b.append(el("h2", { class: "t", text: "Plans for you" }));
    Q.forEach(q => {
      const card = el("button", { type: "button", class: "qcard", "aria-pressed": q.id === PICK ? "true" : "false", onclick: () => { PICK = q.id; step3Cards(); } },
        el("div", { class: "top" }, el("div", {}, el("b", { class: "n", text: q.name }), el("br"), el("span", { class: "tag2", text: q.id === rec.id ? "Recommended" : q.tag })),
          el("div", { class: "pr" }, inr(q.premium), el("br"), el("small", { text: "a year · " + inr(q.monthly) + "/mo" }))),
        el("ul", {}, ...q.pts.map(p => el("li", { text: p })), el("li", { text: "Cover " + inr(q.cover) })));
      card.dataset.id = q.id; b.append(card);
    });
    b.append(el("div", { class: "why" }, el("b", { text: "Why " + Q.find(q => q.id === rec.id).name + "? " }), rec.why));
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: () => step2() }), el("button", { class: "czbtn", text: "Review", onclick: () => step4() })));
    refresh();
  }
  function step3Cards() { document.querySelectorAll("#hiw-body .qcard").forEach(c => c.setAttribute("aria-pressed", c.dataset.id === PICK ? "true" : "false")); }

  function privacyPanel(q) {
    const age = W.members[0].age;
    const rows = [
      ["Name", tok("name", W.proposer.name), "AI never sees the real name"],
      ["Mobile", maskMobile(W.proposer.mobile), "masked"],
      ["PAN", maskPan(PAN), "format checked, shown masked, never stored"],
      ["Aadhaar", KYC.ok ? "••••" + KYC.ref : "not collected", "identity comes from DigiLocker; we get only a verified flag and the last 4 digits"],
      ["Medical", W.conds.length ? W.conds.map(c => COND[c].n).join(", ") : "none declared", "used to price the plan, shared with " + q.name + " only after your consent"],
    ];
    return el("details", { class: "priv" }, el("summary", { text: "What does the AI see about me?" }),
      el("p", { class: "s", style: "margin:.6rem 0 .4rem;font-size:.85rem", text: "The assistant only works with: age band " + ageBand(age) + ", city tier " + W.city + ", cover " + inr(W.cover) + ", and the conditions above. It never receives your name, mobile, PAN or Aadhaar." }),
      ...rows.map(r => el("div", { class: "prow" }, el("b", { text: r[0] }), el("code", { text: r[1] }), el("small", { text: r[2] }))));
  }
  function kycCard() {
    const c = el("div", { class: "kyc" });
    const draw = () => {
      c.replaceChildren(el("b", { text: "Verify your identity" }),
        el("p", { class: "s", style: "margin:.3rem 0 .6rem;font-size:.85rem", text: KYC.ok ? "Verified through DigiLocker (demo). Reference ••••" + KYC.ref + ". Your Aadhaar number never reaches Kaamezy or the AI." : "We use DigiLocker so your Aadhaar number never comes to us. You approve it in the DigiLocker screen, and we only get a 'verified' flag." }),
        KYC.ok ? el("span", { class: "v ALLOW", text: "VERIFIED" }) : el("button", { type: "button", class: "czbtn soft", text: "Verify with DigiLocker (demo)", onclick: async () => {
          const r = await capi("POST", "/kyc/digilocker-demo"); if (!r.ok) { toast(errMsg(r)); return; }
          KYC = { ok: true, ref: r.data.ref_last4 }; draw(); } }));
    };
    draw();
    capi("GET", "/kyc").then(r => { if (r.ok && r.data.verified) { KYC = { ok: true, ref: r.data.ref_last4 }; draw(); } });
    return c;
  }
  function step4() {
    STEP = 4; setSteps(4, "Review and buy"); const b = $("hiw-body"); b.replaceChildren();
    const q = Q.find(x => x.id === PICK);
    b.append(el("h2", { class: "t", text: "Review and buy" }), el("p", { class: "s", text: "The premium below came from the rule engine. It cannot be typed or changed here." }));
    const d = (k, v) => [el("dt", { text: k }), el("dd", { text: v })];
    b.append(el("dl", { class: "sum" }, ...d("Insurer", q.name), ...d("Members", W.members.map(m => m.rel + " " + m.age).join(", ")),
      ...d("Cover", inr(q.cover)), ...d("City", (W.city === "metro" ? "Metro" : "Other")),
      ...d("Existing conditions", W.conds.length ? W.conds.map(c => COND[c].n).join(", ") : "None"), ...d("Premium (incl. 18% GST)", inr(q.premium) + " / year")));

    b.append(el("span", { class: "lbl", text: "PAN" }));
    const pan = el("input", { id: "hiw-pan", placeholder: "ABCDE1234F", maxlength: "10", autocomplete: "off", value: PAN, style: "text-transform:uppercase", oninput: e => { PAN = e.target.value.toUpperCase(); } });
    b.append(pan, el("p", { class: "s", style: "margin:.4rem 0 0;font-size:.8rem", text: (PAN ? "Filled from your details. " : "") + "Insurers need PAN (or Form 60). It is checked for format and never saved." }));

    b.append(el("span", { class: "lbl", text: "Nominee" }),
      field("Nominee's full name", { value: W.nominee.name, oninput: e => { W.nominee.name = e.target.value; } }));
    const rel = el("select", { "aria-label": "Nominee relation", onchange: e => { W.nominee.rel = e.target.value; } }, ...["Spouse", "Son", "Daughter", "Father", "Mother", "Other"].map(r => el("option", { value: r, text: r })));
    rel.value = W.nominee.rel; b.append(el("label", { class: "auth-field", style: "margin-top:.7rem" }, "Relation to you", rel));

    b.append(el("span", { class: "lbl", text: "Identity (KYC)" }), kycCard(), el("div", { style: "height:.7rem" }), privacyPanel(q));

    const cb = el("input", { type: "checkbox", id: "hiw-consent" }); cb.checked = CONSENT; cb.onchange = () => { CONSENT = cb.checked; };
    b.append(el("label", { class: "consent" }, cb, el("span", { text: "I agree to share my health details with " + q.name + " only to issue this policy. I can withdraw this consent any time from Privacy & AI activity." })));
    const res = el("div", { id: "hiw-res" });
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: () => step3() }), el("button", { class: "czbtn", id: "hiw-buy", text: "Buy with Kaamezy", onclick: () => attempt({}) })), res);
    refresh();
  }

  /* One decision card for both flows. The server decided; this only shows it and offers the next allowed step. */
  function renderDecision(g, info) {
    const r = info.mount; r.replaceChildren();
    const head = { ALLOW: "Approved by the guardrail", NEEDS_CONFIRMATION: g.kind === "human" ? info.humanTitle : "Needs your one-time code", BLOCK: "Blocked. No confirmation can override this." }[g.verdict];
    const box = el("div", { class: "gr " + g.verdict }, el("span", { class: "v " + g.verdict, text: g.verdict.replace("_", " ") + " · " + g.reason_code }), el("h3", { text: head }), el("div", { class: "why2", text: g.reason }),
      el("ul", {}, ...g.checks.map(c => el("li", { class: c.ok === true ? "ok" : c.ok === false ? "no" : "pend", text: c.name }))));
    if (g.verdict === "NEEDS_CONFIRMATION" && g.kind === "otp") {
      const inp = el("input", { inputmode: "numeric", placeholder: "OTP (demo: " + DEMO_OTP + ")", "aria-label": "OTP" });
      box.append(el("div", { class: "otp" }, inp, el("button", { type: "button", text: "Confirm", onclick: () => { if (!inp.value.trim()) { toast("Enter the code."); return; } info.retry({ otp: inp.value.trim() }); } })));
    }
    if (g.verdict === "NEEDS_CONFIRMATION" && g.kind === "human" && info.handoff) {
      box.append(el("p", { class: "why2", text: "Handoff " + info.handoff.id + " created. Brief for the " + info.who + ": " + info.handoff.brief }),
        el("button", { type: "button", class: "czbtn soft", style: "margin-top:.7rem", text: "Demo: a human approves", onclick: () => info.retry({ humanApprove: true }) }));
    }
    if (g.verdict === "BLOCK") box.append(el("button", { type: "button", class: "ghost", text: "Go back and change details", onclick: () => info.back() }));
    r.append(box); refresh(); r.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* "Demo: a human approves" calls the staff endpoint with the demo staff key. A real deployment sets CHAUKAS_STAFF_KEY
     and the agent desk (not this page) holds it; the browser can never approve its own review. */
  const approveAsStaff = h => capi("POST", "/handoffs/" + h.id + "/resolve", { decision: "approved", staff_id: "DEMO-STAFF" }, { "X-Staff-Key": "demo-staff" });

  async function attempt(extra) {
    extra = extra || {}; const btn = $("hiw-buy"); if (btn) btn.disabled = true;
    try {
      if (extra.humanApprove && HANDOFF) { const a = await approveAsStaff(HANDOFF); if (!a.ok) { toast(errMsg(a)); return; } }
      const r = await capi("POST", "/insurance/purchase", { quote_id: QID, insurer_id: PICK, consent_share_health: CONSENT, pan: PAN || null,
        mobile: W.proposer.mobile, holder_name: W.proposer.name, nominee_name: W.nominee.name, nominee_relation: W.nominee.rel,
        otp: extra.otp || null, handoff_id: HANDOFF ? HANDOFF.id : null });
      if (!r.ok) { toast(errMsg(r)); return; }
      const d = r.data.decision; if (r.data.handoff) HANDOFF = r.data.handoff;
      if (extra.otp && d.reason_code === "OVER_AUTO_LIMIT") toast("That code is not right.");
      if (d.verdict !== "ALLOW") { renderDecision(d, { mount: $("hiw-res"), humanTitle: "Needs a human underwriter", who: "underwriter", handoff: HANDOFF, retry: attempt, back: () => step1() }); return; }
      showPolicy(r.data.policy);
    } finally { if (btn) btn.disabled = false; }
  }

  function showPolicy(p) {
    refreshLanding(); const b = $("hiw-body"); b.replaceChildren(); setSteps(4, "Done");
    b.append(el("div", { class: "done-mark", text: "✓" }), el("h2", { class: "t", style: "text-align:center", text: "You are covered" }),
      el("p", { class: "s", style: "text-align:center", text: p.insurer_name + " · policy " + p.id }),
      el("dl", { class: "sum" }, el("dt", { text: "Cover" }), el("dd", { text: inr(p.cover_inr) }), el("dt", { text: "Premium paid" }), el("dd", { text: inr(p.premium_inr) }),
        el("dt", { text: "Valid" }), el("dd", { text: day(p.start_date) + " – " + day(p.end_date) })),
      el("p", { class: "s", text: "Demo only: no money moved and no real policy exists. The decision is saved in Privacy & AI activity." }),
      el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "My policies", onclick: () => openPolicies() }), el("button", { class: "czbtn", text: "Ask Kaamezy", onclick: () => openChat(undefined, "hi") })));
    refresh();
  }

  /* ================= LOAN ================= */
  const TENURES = [6, 12, 18, 24, 36];
  const PURPOSES = ["Medical", "Education", "Home", "Business", "Other"];
  const LOAN_AMOUNTS = [25000, 50000, 100000, 200000, 500000];
  const PLAIN = {
    AGE_OUT_OF_RANGE: "The lender lends to people aged 21 to 60.",
    INCOME_TOO_LOW: "Monthly income is below the lender's minimum of ₹15,000.",
    OBLIGATIONS_TOO_HIGH: "Your existing EMIs already use half of your income.",
    CAPACITY_TOO_LOW: "After your EMIs there is too little monthly room for the minimum loan of ₹10,000.",
    AMOUNT_TOO_LOW: "The amount you asked for is below the minimum loan of ₹10,000.",
  };

  const blankLoan = () => ({ amount: 100000, tenure: 12, purpose: "Medical", employment: "salaried", income: 50000, emi: 0, age: 32, name: W.proposer.name || "", mobile: W.proposer.mobile || "", pincode: W.pincode || "", policy: null });
  const SAMPLE_LOAN = () => ({ amount: 100000, tenure: 12, purpose: "Medical", employment: "salaried", income: 60000, emi: 5000, age: 34, name: "Demo Sharma", mobile: "9876543210", pincode: "400001", policy: null });
  let L = blankLoan(), LD = null, LQ = null, LKFS = null, KFSP = null, LHANDOFF = null, LC1 = false, LC2 = false;

  const lnum = (label, key, ph, step) => el("label", { class: "auth-field", style: "margin-top:.7rem" }, label, el("input", { type: "number", inputmode: "numeric", placeholder: ph || "", step: step || "1", value: L[key] == null ? "" : String(L[key]), oninput: e => { L[key] = e.target.value === "" ? null : Number(e.target.value); } }));
  const ltxt = (label, key, attrs) => el("label", { class: "auth-field", style: "margin-top:.7rem" }, label, el("input", Object.assign({ value: L[key] || "", oninput: e => { L[key] = attrs && attrs.digits ? e.target.value.replace(/\D/g, "") : e.target.value; } }, attrs && attrs.attrs)));

  function validLoan1() {
    const bad = m => { toast(m); return false; };
    if (!(L.amount >= 10000 && L.amount <= 500000)) return bad("Choose an amount between ₹10,000 and ₹5,00,000.");
    if (!TENURES.includes(L.tenure)) return bad("Choose how long you want to repay over.");
    if (!L.purpose) return bad("Choose what the loan is for.");
    if (!L.employment) return bad("Choose how you earn.");
    if (!(L.income > 0)) return bad("Enter your monthly income.");
    if (L.emi == null || L.emi < 0) return bad("Enter your existing monthly EMIs (0 if none).");
    if (!(L.age >= 18 && L.age <= 99)) return bad("Enter a valid age.");
    if ((L.name || "").trim().length < 2) return bad("Please enter your name.");
    if (!RE.mobile.test(L.mobile || "")) return bad("Enter a valid 10-digit mobile number.");
    if (!RE.pin.test(L.pincode || "")) return bad("Enter a valid 6-digit pincode.");
    return true;
  }

  let POLS = [];   /* policies, only to decide whether to offer the "medical bridge" purpose; refreshed in the background */
  function lstep1() {
    setSteps(1, "About you and the loan", "lnw"); const b = $("lnw-body"); b.replaceChildren();
    listPolicies().then(p => { const changed = p.length !== POLS.length; POLS = p; if (changed && $("lnw-label").textContent.startsWith("Step 1")) lstep1(); });
    const pols = POLS;
    b.append(el("h2", { class: "t", text: "Tell us about the loan" }), el("p", { class: "s", text: "The lender decides the amount from your age, income and EMIs only. Your name, gender, city and language never matter." }),
      docBox(d => { if (d.name) L.name = d.name; if (d.age != null) L.age = d.age; if (d.pan) PAN = d.pan; lstep1(); }));
    const amtHint = el("div", { style: "margin:.35rem 0 0;font-weight:800;color:var(--p-blue)", text: L.amount ? inr(L.amount) : "" });
    b.append(el("span", { class: "lbl", text: "How much do you need?" }), el("label", { class: "auth-field" }, el("input", { type: "number", inputmode: "numeric", step: "5000", "aria-label": "Loan amount", value: String(L.amount || ""), oninput: e => { L.amount = e.target.value === "" ? null : Number(e.target.value); amtHint.textContent = L.amount ? inr(L.amount) : ""; } })), amtHint);
    const am = el("div", { class: "chips", style: "margin-top:.5rem" }); LOAN_AMOUNTS.forEach(a => am.append(chip(inr(a), L.amount === a, () => { L.amount = a; lstep1(); }))); b.append(am);
    b.append(el("span", { class: "lbl", text: "Repay over" })); const tn = el("div", { class: "chips" }); TENURES.forEach(n => tn.append(chip(n + " months", L.tenure === n, () => { L.tenure = n; lstep1(); }))); b.append(tn);
    b.append(el("span", { class: "lbl", text: "What is it for?" })); const pu = el("div", { class: "chips" });
    const purposes = PURPOSES.slice();
    purposes.forEach(p => pu.append(chip(p, L.purpose === p, () => { L.purpose = p; lstep1(); }))); b.append(pu);
    b.append(el("span", { class: "lbl", text: "How do you earn?" }), el("div", { class: "chips" }, chip("Salaried", L.employment === "salaried", () => { L.employment = "salaried"; lstep1(); }), chip("Self-employed", L.employment === "self", () => { L.employment = "self"; lstep1(); })));
    b.append(lnum("Monthly income (₹)", "income", "e.g. 50000", "1000"), lnum("Existing EMIs per month (₹, 0 if none)", "emi", "0", "500"));
    b.append(el("span", { class: "lbl", text: "About you" }), ltxt("Full name", "name", { attrs: { autocomplete: "name" } }), lnum("Age", "age", "e.g. 32"),
      ltxt("Mobile number", "mobile", { digits: true, attrs: { type: "tel", inputmode: "numeric", maxlength: "10" } }), ltxt("Pincode", "pincode", { digits: true, attrs: { type: "tel", inputmode: "numeric", maxlength: "6" } }));
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn", text: "Check my offer", onclick: () => { if (validLoan1()) lstep2(); } })));
    refresh();
  }

  async function lstep2() {
    setSteps(2, "The lender decides", "lnw"); const b = $("lnw-body"); b.replaceChildren();
    b.append(el("h2", { class: "t", text: "Kaamezy is checking" }), el("p", { class: "s", text: "Each line is a real check. The lender's rules, not the AI, decide." }));
    const list = el("div", { class: "agentlist" }); b.append(list);
    const req = capi("POST", "/loan/decide", { amount_inr: L.amount, tenure_months: L.tenure, purpose: L.purpose,
      employment: L.employment === "self" ? "self_employed" : "salaried", monthly_income_inr: L.income, existing_emi_inr: L.emi, age: L.age });
    for (const n of ["Checking your KYC on file", "Running a soft credit check (simulated)", "Comparing your income with your EMIs", "Asking the lender for a decision"]) {
      const r = el("div", { class: "arow" }, el("span", { class: "dot" }), el("span", { text: n })); list.append(r); await sleep(520); r.classList.add("done");
    }
    const res = await req;
    if (!res.ok) { failBox(b, "Couldn't check your offer", res, () => lstep1(), () => lstep2()); return; }
    const dd = res.data.decision; LQ = res.data.quote_id; LKFS = res.data.kfs; LHANDOFF = null; KFSP = null;
    LD = dd.eligible ? { eligible: true, amount: dd.amount_inr, requested: dd.requested_inr, max: dd.max_inr, apr: dd.apr_pct, tenure: dd.tenure_months, emi: dd.emi_inr,
      total: dd.total_inr, interest: dd.interest_inr, fee: dd.fee_inr, feeGst: dd.fee_gst_inr, net: dd.net_inr, reasons: [] } : { eligible: false, reasons: dd.reasons };
    const fair = el("p", { class: "s", style: "font-size:.8rem;margin-top:.8rem", text: "This decision used only age, income and EMIs. Not your name, gender, location or language." });
    if (!LD.eligible) {
      b.append(el("div", { class: "gr BLOCK" }, el("span", { class: "v BLOCK", text: "NOT ELIGIBLE · " + LD.reasons[0] }), el("h3", { text: "The lender can't offer this loan right now" }),
        el("ul", {}, ...LD.reasons.map(r => el("li", { class: "no", text: PLAIN[r] || r })))), fair,
        el("div", { class: "navrow" }, el("button", { class: "czbtn", text: "Change details", onclick: () => lstep1() })));
      refresh(); return;
    }
    const d = (k, v) => [el("dt", { text: k }), el("dd", { text: v })];
    b.append(el("div", { class: "gr ALLOW" }, el("span", { class: "v ALLOW", text: "OFFER · from " + LKFS.lender_name }), el("h3", { text: inr(LD.amount) + " approved" }),
      LD.amount < LD.requested ? el("div", { class: "why2", text: "You asked for " + inr(LD.requested) + ". The lender can offer up to " + inr(LD.max) + " on these details, so the offer is " + inr(LD.amount) + "." }) : null,
      el("dl", { class: "sum" }, ...d("Interest rate (APR)", LD.apr + "% a year"), ...d("Repay over", LD.tenure + " months"), ...d("EMI", inr(LD.emi) + " / month"),
        ...d("Processing fee + GST", inr(LD.fee + LD.feeGst)), ...d("You receive", inr(LD.net)))), fair,
      el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Change details", onclick: () => lstep1() }), el("button", { class: "czbtn", text: "See Key Facts", onclick: () => lstep3() })));
    refresh();
  }

  function lstep3() {
    setSteps(3, "Key Facts and consent", "lnw"); const b = $("lnw-body"); b.replaceChildren();
    KFSP = capi("POST", "/loan/quotes/" + LQ + "/kfs-shown");   /* the server records WHEN the Key Facts were on screen */
    const k = LKFS, dd = (a, v) => [el("dt", { text: a }), el("dd", { text: v })];
    b.append(el("h2", { class: "t", text: "Key Facts Statement" }), el("p", { class: "s", text: "Every cost, before you agree. No insurance or other product is required to get this loan." }));
    b.append(el("dl", { class: "sum kfs" }, ...dd("Lender", k.lender_name), ...dd("Loan amount", inr(k.amount_inr)), ...dd("Repay over", k.tenure_months + " months"), ...dd("Interest rate (APR)", k.apr_pct + "% a year"),
      ...dd("EMI", inr(k.emi_inr) + " / month"), ...dd("Total interest", inr(k.interest_inr)), ...dd("Processing fee", inr(k.processing_fee_inr) + " + " + inr(k.processing_fee_gst_inr) + " GST"),
      ...dd("You receive", inr(k.net_disbursal_inr)), ...dd("You repay in total", inr(k.total_repayable_inr)), ...dd("Cooling-off", k.cooling_off_days + " days: exit by repaying the principal and interest for the days used, no penalty (demo terms)"),
      ...dd("Grievance contact", k.grievance_contact)));
    b.append(el("span", { class: "lbl", text: "Identity (KYC)" }), kycCard());
    const c1 = el("input", { type: "checkbox" }), c2 = el("input", { type: "checkbox" });
    c1.checked = LC1; c2.checked = LC2; c1.onchange = () => { LC1 = c1.checked; }; c2.onchange = () => { LC2 = c2.checked; };
    b.append(el("label", { class: "consent" }, c1, el("span", { text: "I have read the Key Facts Statement above and I accept this offer from " + k.lender_name + "." })),
      el("label", { class: "consent" }, c2, el("span", { text: "Share my PAN and income details with " + k.lender_name + " for the credit check only. I can withdraw this consent from Privacy & AI activity." })));
    b.append(el("p", { class: "s", style: "margin-top:.8rem;font-size:.82rem", text: "Money goes to your Paytm-linked bank account ••••4321 (demo)." }));
    const res = el("div", { id: "lnw-res" });
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: () => lstep2() }), el("button", { class: "czbtn", id: "lnw-accept", text: "Accept and get money", onclick: () => attemptLoan({}) })), res);
    refresh();
  }

  async function attemptLoan(extra) {
    extra = extra || {}; const btn = $("lnw-accept"); if (btn) btn.disabled = true;
    try {
      if (KFSP) await KFSP;
      if (extra.humanApprove && LHANDOFF) { const a = await approveAsStaff(LHANDOFF); if (!a.ok) { toast(errMsg(a)); return; } }
      const r = await capi("POST", "/loan/accept", { quote_id: LQ, consent_offer: LC1, consent_credit_check: LC2, pan: PAN || null, mobile: L.mobile,
        holder_name: L.name, pincode: L.pincode, otp: extra.otp || null, handoff_id: LHANDOFF ? LHANDOFF.id : null });
      if (!r.ok) { toast(errMsg(r)); return; }
      const d = r.data.decision; if (r.data.handoff) LHANDOFF = r.data.handoff;
      if (extra.otp && d.reason_code === "OVER_AUTO_LIMIT") toast("That code is not right.");
      if (d.verdict !== "ALLOW") { renderDecision(d, { mount: $("lnw-res"), humanTitle: "Needs a human to verify income", who: "credit officer", handoff: LHANDOFF, retry: attemptLoan, back: () => lstep1() }); return; }
      showLoan(r.data.loan);
    } finally { if (btn) btn.disabled = false; }
  }

  function showLoan(ln) {
    refreshLoanLanding(); setSteps(4, "Done", "lnw"); const b = $("lnw-body"); b.replaceChildren();
    b.append(el("div", { class: "done-mark", text: "✓" }), el("h2", { class: "t", style: "text-align:center", text: "Loan approved" }), el("p", { class: "s", style: "text-align:center", text: ln.lender_name + " · " + ln.id }),
      el("dl", { class: "sum" }, el("dt", { text: "Credited to ••••4321" }), el("dd", { text: inr(ln.net_inr) }), el("dt", { text: "First EMI" }), el("dd", { text: inr(ln.emi_inr) + " on " + day(ln.first_emi_date) }),
        el("dt", { text: "Cooling-off until" }), el("dd", { text: day(ln.cooling_off_end) })),
      el("p", { class: "s", text: "Demo only: no money moved and no real loan exists. The decision is saved in Privacy & AI activity." }),
      el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "My loans", onclick: () => openLoans() }), el("button", { class: "czbtn", text: "Ask Kaamezy", onclick: () => openChat(undefined, "ln") })));
    refresh();
  }

  function emiSchedule(x) {
    const rows = []; const n = Math.min(3, x.tenure_months);
    for (let i = 0; i < n; i++) { const d = new Date(x.first_emi_date); d.setDate(d.getDate() + 30 * i); rows.push(el("div", { class: "prow2" }, el("span", { text: "EMI " + (i + 1) + " · " + d.toLocaleDateString("en-IN") }), el("code", { text: inr(x.emi_inr) }))); }
    if (x.tenure_months > n) rows.push(el("div", { class: "prow2" }, el("span", { text: "…and " + (x.tenure_months - n) + " more, same amount each month" }), el("code", { text: "Total " + inr(x.total_inr) })));
    return el("details", { class: "priv", style: "margin-top:.6rem" }, el("summary", { text: "EMI schedule" }), ...rows);
  }
  async function openLoans() {
    showScreen("lnl"); const b = $("lnl-body"); b.replaceChildren(el("div", { class: "empty2", text: "Loading…" }));
    const r = await capi("GET", "/loans"); b.replaceChildren();
    if (!r.ok) { b.append(el("div", { class: "empty2", text: errMsg(r) })); return; }
    if (!r.data.length) b.append(el("div", { class: "empty2", text: "No loans yet. Check your offer to start." }), el("button", { class: "czbtn", text: "Check my offer", onclick: () => startLoan(blankLoan()) }));
    r.data.slice().reverse().forEach(x => b.append(el("div", { class: "polcard" }, el("div", { class: "h" }, el("span", { text: inr(x.amount_inr) + " · " + x.purpose }), el("span", { class: "v ALLOW", text: x.status })),
      x.purpose.indexOf("Medical bridge") === 0
        ? el("div", { class: "m" }, x.lender_name + " · " + x.id, el("br"), "One repayment of " + inr(x.total_inr) + " from your claim payout by " + day(x.first_emi_date), el("br"), "Cooling-off until " + day(x.cooling_off_end))
        : el("div", { class: "m" }, x.lender_name + " · " + x.id, el("br"), "EMI " + inr(x.emi_inr) + " × " + x.tenure_months + " at " + x.apr_pct + "% APR", el("br"), "Next EMI " + day(x.first_emi_date) + " · Cooling-off until " + day(x.cooling_off_end)),
      x.purpose.indexOf("Medical bridge") === 0 ? null : emiSchedule(x))));
    refresh();
  }
  async function refreshLoanLanding() {
    const [lr, pols] = await Promise.all([capi("GET", "/loans"), listPolicies()]);
    const n = lr.ok ? lr.data.length : 0, p = pols[pols.length - 1];
    $("ln-r-loans-s").textContent = n ? n + " active" : "Your loans will show up here";
    $("ln-start").hidden = n > 0; $("ln-bridge").hidden = !p;
    if (p) $("ln-bridge-s").textContent = "You have a health policy with " + p.insurer_name + ".";
  }
  function startLoan(l, policyId) {
    L = l; LD = null; LQ = null; LKFS = null; KFSP = null; LHANDOFF = null; LC1 = false; LC2 = false;
    if (l.name === "Demo Sharma" && !PAN) PAN = "ABCDE1234F";
    showScreen("lnw"); lstep1();
  }
  /* The assistant read figures out of a chat message. The SERVER validated them (stated, in range); this only fills the form.
     Whatever the person did not state is left EMPTY (never defaulted), and nothing runs until they tap "Check my offer". */
  const PREFILL_FIELD = { amount_inr: "amount", tenure_months: "tenure", monthly_income_inr: "income", existing_emi_inr: "emi", age: "age", employment: "employment", purpose: "purpose" };
  const PREFILL_LABEL = { amount_inr: "Amount", tenure_months: "Repay over", monthly_income_inr: "Monthly income", existing_emi_inr: "Existing EMIs", age: "Age", employment: "Employment", purpose: "Purpose" };
  const PREFILL_WHY = { outside_range: "is outside what the lender offers", not_offered: "is not an offered repayment period", not_in_message: "could not be read clearly" };
  const prefillValue = (k, v) => k === "tenure_months" ? v + " months" : k === "age" ? String(v) : k === "employment" ? (v === "self_employed" ? "Self-employed" : "Salaried") : k === "purpose" ? v : inr(v);
  function prefillLoan(pf) {
    const l = blankLoan();
    Object.entries(PREFILL_FIELD).forEach(([k, f]) => { l[f] = k in pf.fields ? (k === "employment" ? (pf.fields[k] === "self_employed" ? "self" : "salaried") : pf.fields[k]) : null; });
    startLoan(l);
    const dropped = Object.keys(pf.dropped || {});
    if (dropped.length) toast("Please check: " + dropped.map(k => PREFILL_LABEL[k] + " " + PREFILL_WHY[pf.dropped[k]]).join("; "));
  }
  function prefillCard(pf) {
    const rows = Object.keys(PREFILL_FIELD).filter(k => k in pf.fields).map(k => el("div", { class: "pf-row" }, el("span", { text: PREFILL_LABEL[k] }), el("b", { text: prefillValue(k, pf.fields[k]) })));
    const need = pf.missing.map(k => PREFILL_LABEL[k]);
    return el("div", { class: "pf" }, ...rows, need.length ? el("div", { class: "pf-need", text: "Still needed from you: " + need.join(", ") }) : null,
      el("div", { class: "acts" }, el("button", { class: "mini-cta", type: "button", text: "Review the loan form", onclick: () => prefillLoan(pf) })));
  }
  /* ================= CLAIMS + BRIDGE ADVANCE =================
     A claim is filed with a SIMULATED insurer; its decision and the advance both come from rules on the server.
     The advance goes through the same steps as a loan: Key Facts first, identity, consent, a one-time code above the limit. */
  const CLAIM_STATE = { under_review: "Under review", approved_payout_pending: "Approved, payout pending", paid: "Paid" };
  const CLAIM_AMOUNTS = [50000, 100000, 200000];
  const BRIDGE_PLAIN = { CLAIM_NOT_APPROVED_YET: "The insurer has not approved this claim yet.", CLAIM_ALREADY_PAID: "The insurer has already paid this claim.",
    AMOUNT_TOO_LOW: "The approved amount is too small for an advance.", ADVANCE_ALREADY_TAKEN: "An advance has already been taken against this claim." };
  let BQ = null, BKFS = null, BKFSP = null, BC1 = false, BC2 = false, BID = { pan: "", name: "", mobile: "", pincode: "" };
  const bfield = (label, key, attrs) => el("label", { class: "auth-field", style: "margin-top:.7rem" }, label,
    el("input", Object.assign({ value: BID[key] || "", oninput: e => { BID[key] = key === "name" ? e.target.value : key === "pan" ? e.target.value.toUpperCase().trim() : e.target.value.replace(/\D/g, ""); } }, attrs)));

  async function openClaims() {
    showScreen("clm"); const b = $("clm-body"); b.replaceChildren(el("div", { class: "empty2", text: "Loading…" }));
    const [cr, pols] = await Promise.all([capi("GET", "/claims"), listPolicies()]);
    b.replaceChildren();
    if (!cr.ok) { b.append(el("div", { class: "empty2", text: errMsg(cr) })); return; }
    b.append(el("p", { class: "s", text: "Demo: the insurer and the lender are simulated. The insurer's decision and any advance come from rules in code, never from the AI." }));
    if (!pols.length) b.append(el("div", { class: "empty2", text: "You need a health policy before you can file a claim." }), el("button", { class: "czbtn", text: "Get a quote", onclick: () => startWizard(blank()) }));
    else b.append(claimForm(pols[pols.length - 1]));
    if (!cr.data.length) b.append(el("div", { class: "empty2", text: "No claims yet." }));
    cr.data.slice().reverse().forEach(c => b.append(claimCard(c)));
    refresh();
  }

  function claimForm(pol) {
    const inp = el("input", { type: "number", inputmode: "numeric", step: "1000", "aria-label": "Claim amount", value: "100000" });
    const chips = el("div", { class: "chips", style: "margin-top:.5rem" }); CLAIM_AMOUNTS.forEach(a => chips.append(chip(inr(a), false, () => { inp.value = String(a); })));
    return el("div", { class: "polcard" }, el("b", { text: "File a claim" }),
      el("p", { class: "s", style: "margin:.3rem 0 .6rem;font-size:.85rem", text: "Policy " + pol.id + " · cover " + inr(pol.cover_inr) + ". Enter the hospital bill you are claiming." }),
      el("label", { class: "auth-field" }, inp), chips,
      el("button", { class: "czbtn", style: "margin-top:.7rem", text: "File claim (demo)", onclick: async e => {
        const btn = e.currentTarget; btn.disabled = true;
        try {
          const r = await capi("POST", "/claims", { policy_id: pol.id, claimed_inr: Number(inp.value) });
          if (!r.ok) { toast((r.data && r.data.detail && r.data.detail.message) || errMsg(r)); return; }
          toast("Claim filed with the simulated insurer."); openClaims();
        } finally { btn.disabled = false; }
      } }));
  }

  function claimCard(c) {
    const card = el("div", { class: "polcard" }, el("div", { class: "h" }, el("span", { text: inr(c.claimed_inr) + " claim" }), el("span", { class: "v " + (c.state === "paid" ? "BLOCK" : "ALLOW"), text: CLAIM_STATE[c.state] || c.state })),
      el("div", { class: "m" }, "Claim " + c.id, el("br"), "Insurer approves " + inr(c.approved_inr), el("br"), (c.state === "paid" ? "Paid on " : "Payout expected by ") + day(c.payout_due)));
    if (c.bridge_loan_id) card.append(el("p", { class: "s", style: "margin:.5rem 0 0;font-size:.85rem", text: "An advance " + c.bridge_loan_id + " was taken against this claim. It is repaid from the payout." }));
    else if (c.bridge_up_to_inr) card.append(el("p", { class: "s", style: "margin:.6rem 0 .4rem;font-size:.85rem", text: "Need cash before the payout? You can ask for a short advance of up to " + inr(c.bridge_up_to_inr) + " against this claim. It is repaid from the claim payout, and you see every cost first." }),
      el("button", { class: "czbtn soft", text: "See my advance offer", onclick: () => startBridge(c) }));
    else if (c.bridge_blocked && BRIDGE_PLAIN[c.bridge_blocked]) card.append(el("p", { class: "s", style: "margin:.5rem 0 0;font-size:.85rem", text: BRIDGE_PLAIN[c.bridge_blocked] }));
    return card;
  }

  async function startBridge(c) {
    const r = await capi("POST", "/bridge/decide", { claim_id: c.id }); if (!r.ok) { toast(errMsg(r)); return; }
    const d = r.data.decision;
    if (!d.eligible) { toast(BRIDGE_PLAIN[d.reasons[0]] || "No advance is possible on this claim."); openClaims(); return; }
    BQ = r.data.quote_id; BKFS = r.data.kfs; BKFSP = null; BC1 = false; BC2 = false;
    BID = { pan: PAN || "", name: BID.name || W.proposer.name || "", mobile: BID.mobile || W.proposer.mobile || "", pincode: BID.pincode || W.pincode || "" };
    bridgeKfs();
  }

  function bridgeKfs() {
    const b = $("clm-body"); b.replaceChildren(); const k = BKFS, dd = (a, v) => [el("dt", { text: a }), el("dd", { text: v })];
    BKFSP = capi("POST", "/loan/quotes/" + BQ + "/kfs-shown");   /* the server records WHEN the Key Facts were on screen */
    b.append(el("h2", { class: "t", text: "Key Facts Statement" }), el("p", { class: "s", text: "Every cost, before you agree. No insurance or other product is required to get this advance." }));
    b.append(el("dl", { class: "sum kfs" }, ...dd("Lender", k.lender_name), ...dd("Advance amount", inr(k.amount_inr)), ...dd("Repaid from", k.repaid_from), ...dd("Repay by", day(k.repay_by) + " (" + k.days + " days)"),
      ...dd("Interest rate (APR)", k.apr_pct + "% a year, only for those days"), ...dd("Interest", inr(k.interest_inr)), ...dd("Processing fee", inr(k.processing_fee_inr) + " + " + inr(k.processing_fee_gst_inr) + " GST"),
      ...dd("You receive", inr(k.net_disbursal_inr)), ...dd("You repay in total", inr(k.total_repayable_inr)), ...dd("Cooling-off", k.cooling_off_days + " days: exit by repaying the principal and interest for the days used, no penalty (demo terms)"),
      ...dd("Grievance contact", k.grievance_contact)));
    b.append(el("span", { class: "lbl", text: "Identity (KYC)" }), kycCard(), bfield("PAN", "pan", { placeholder: "ABCDE1234F", maxlength: "10", autocapitalize: "characters" }),
      bfield("Your name", "name"), bfield("Mobile number", "mobile", { inputmode: "numeric", maxlength: "10" }), bfield("Pincode", "pincode", { inputmode: "numeric", maxlength: "6" }));
    const c1 = el("input", { type: "checkbox" }), c2 = el("input", { type: "checkbox" });
    c1.checked = BC1; c2.checked = BC2; c1.onchange = () => { BC1 = c1.checked; }; c2.onchange = () => { BC2 = c2.checked; };
    b.append(el("label", { class: "consent" }, c1, el("span", { text: "I have read the Key Facts Statement above. I accept this advance and agree that it is repaid from my insurance claim payout." })),
      el("label", { class: "consent" }, c2, el("span", { text: "Share the details of this claim with " + k.lender_name + " for this advance only. I can withdraw this consent from Privacy & AI activity." })));
    b.append(el("p", { class: "s", style: "margin-top:.8rem;font-size:.82rem", text: "Money goes to your Paytm-linked bank account ••••4321 (demo)." }));
    b.append(el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "Back", onclick: () => openClaims() }), el("button", { class: "czbtn", id: "clm-accept", text: "Accept and get money", onclick: () => attemptBridge({}) })), el("div", { id: "clm-res" }));
    refresh();
  }

  async function attemptBridge(extra) {
    extra = extra || {}; const btn = $("clm-accept"); if (btn) btn.disabled = true;
    try {
      if (BKFSP) await BKFSP;
      const r = await capi("POST", "/bridge/accept", { quote_id: BQ, consent_offer: BC1, consent_claim_share: BC2, pan: BID.pan || null, mobile: BID.mobile,
        holder_name: BID.name, pincode: BID.pincode, otp: extra.otp || null });
      if (!r.ok) { toast((r.data && r.data.detail && r.data.detail.message) || errMsg(r)); return; }
      const d = r.data.decision;
      if (extra.otp && d.reason_code === "OVER_AUTO_LIMIT") toast("That code is not right.");
      if (d.verdict !== "ALLOW") { renderDecision(d, { mount: $("clm-res"), humanTitle: "", who: "", handoff: null, retry: attemptBridge, back: () => openClaims() }); return; }
      showBridgeDone(r.data.loan);
    } finally { if (btn) btn.disabled = false; }
  }

  function showBridgeDone(ln) {
    refreshLoanLanding(); const b = $("clm-body"); b.replaceChildren();
    b.append(el("div", { class: "done-mark", text: "✓" }), el("h2", { class: "t", style: "text-align:center", text: "Advance approved" }), el("p", { class: "s", style: "text-align:center", text: ln.lender_name + " · " + ln.id }),
      el("dl", { class: "sum" }, el("dt", { text: "Credited to ••••4321" }), el("dd", { text: inr(ln.net_inr) }), el("dt", { text: "Repaid from your claim payout" }), el("dd", { text: inr(ln.total_inr) + " by " + day(ln.first_emi_date) }),
        el("dt", { text: "Cooling-off until" }), el("dd", { text: day(ln.cooling_off_end) })),
      el("p", { class: "s", text: "Demo only: no money moved and no real advance exists. The decision is saved in Privacy & AI activity." }),
      el("div", { class: "navrow" }, el("button", { class: "czbtn soft", text: "My loans", onclick: () => openLoans() }), el("button", { class: "czbtn", text: "My claims", onclick: () => openClaims() })));
    refresh();
  }
  const bridgeLoan = () => openClaims();

  /* ---------------- lists ---------------- */
  async function openPolicies() {
    showScreen("hip"); const b = $("hip-body"); b.replaceChildren(el("div", { class: "empty2", text: "Loading…" }));
    const r = await capi("GET", "/policies"); b.replaceChildren();
    if (!r.ok) { b.append(el("div", { class: "empty2", text: errMsg(r) })); return; }
    const pols = r.data;
    if (!pols.length) b.append(el("div", { class: "empty2", text: "No policies yet. Get a quote to buy one." }), el("button", { class: "czbtn", text: "Get a quote", onclick: () => startWizard(blank()) }));
    pols.slice().reverse().forEach(p => b.append(el("div", { class: "polcard" }, el("div", { class: "h" }, el("span", { text: p.insurer_name }), el("span", { class: "v ALLOW", text: p.status })),
      el("div", { class: "m" }, "Policy " + p.id, el("br"), p.holder + " · PAN " + p.pan + " · Nominee " + p.nominee, el("br"), p.members.join(", "), el("br"), "Cover " + inr(p.cover_inr) + " · Premium " + inr(p.premium_inr) + " / year", el("br"),
        "Renews " + day(p.end_date) + " (" + Math.max(0, Math.ceil((new Date(p.end_date) - Date.now()) / 86400000)) + " days left)"))));
    if (pols.length) b.append(el("div", { class: "polcard" }, el("b", { text: "Need cash while a claim is pending?" }),
      el("p", { class: "s", style: "margin:.3rem 0 .6rem;font-size:.85rem", text: "File a claim, and once it is approved you can ask for a short advance against the payout." }),
      el("button", { class: "czbtn soft", text: "Open My claims", onclick: () => openClaims() })));
    refresh();
  }
  async function openActivity(from) {
    document.querySelector("#hia [data-back]").dataset.back = from || "hi";
    showScreen("hia"); const b = $("hia-body"); b.replaceChildren(el("div", { class: "empty2", text: "Loading…" }));
    const r = await capi("GET", "/activity"); b.replaceChildren();
    b.append(el("p", { class: "s", text: "Every step the AI took with your data, with the guardrail's verdict. Entries can be added but never changed or removed: the database itself refuses." }));
    if (!r.ok) { b.append(el("div", { class: "empty2", text: errMsg(r) })); return; }
    if (!r.data.length) b.append(el("div", { class: "empty2", text: "Nothing yet. Get a quote and it will show up here." }));
    r.data.forEach(x => b.append(el("div", { class: "act" }, el("span", { class: "a", text: x.actor + " · " + x.action }), el("span", { class: "v " + x.verdict, text: x.verdict.replace("_", " ") }),
      el("span", { class: "m", text: x.reason_code + (x.amount ? " · " + inr(x.amount) : "") + (x.note ? " · " + x.note : "") }), el("time", { text: when(x.ts) }))));
    refresh();
  }
  async function refreshLanding() {
    const n = (await listPolicies()).length;
    $("hi-r-pol-s").textContent = n ? n + " added" : "Add a policy to see it here";
    $("hi-start").hidden = n > 0;
  }
  function startWizard(w) { W = w; PICK = null; CONSENT = false; QID = null; HANDOFF = null; PAN = w.proposer.name === "Demo Sharma" ? "ABCDE1234F" : ""; showScreen("hiw"); step1(); }

  /* ---------------- assistant: the backend LLM agent (POST /assistant/*) ----------------
     The server understands the message (topic + language), words a short reply from its knowledge base, checks the reply
     in code, and falls back to the knowledge-base text if the model is down. The model never chooses an action: the
     server maps the topic to one of the buttons below. Nothing here can buy, issue or approve anything. */
  const ACTIONS = {
    start_insurance_quote: ["Start my quote", () => startWizard(blank())],
    open_loan_check: ["Check my loan offer", () => startLoan(blankLoan())],
    open_bridge_loan: ["Open My claims", () => bridgeLoan()],
    open_policies: ["Open My policies", () => openPolicies()],
  };
  let ASESS = null, AINTRO = null, chatFrom = "hi";
  const scrollChat = () => { $("ic-chat").scrollTop = $("ic-chat").scrollHeight; };
  function bubble(kind, text, extra) {
    const m = el("div", { class: "msg " + kind }, kind === "agent" ? el("div", { class: "who" }, "Kaamezy", el("b", { text: "AI" })) : null, el("div", { text }));
    if (extra) m.append(extra); $("ic-chat").append(m); scrollChat(); return m;
  }
  async function ensureSession() {
    if (ASESS) return ASESS;
    const r = await capi("POST", "/assistant/session" + uiLangQuery());
    if (!r.ok) return null;
    ASESS = r.data.session_id; AINTRO = r.data.disclosure + " " + r.data.intro;
    return ASESS;
  }
  async function chatSay(text, opts) {
    text = (text || "").trim(); if (!text) return;
    const voiceTurn = !!(opts && opts.voice);
    if (voiceTurn) await ensureIntro();
    bubble("me", (opts && opts.shown) || text); $("ic-text").value = "";
    const ty = el("div", { class: "msg agent typing", text: "Kaamezy is typing…" }); $("ic-chat").append(ty); scrollChat();
    const screen = chatFrom === "ln" ? "loan" : chatFrom === "home" ? "home" : "insurance";
    let r = { ok: false };
    for (let tries = 0; tries < 2 && !r.ok; tries++) {
      const sid = await ensureSession(); if (!sid) break;
      r = await capi("POST", "/assistant/message", { session_id: sid, text, screen, ui_lang: appLang() });
      if (!r.ok && r.status === 404) ASESS = null;   /* the server no longer knows this chat: start a new one and retry once */
      else break;
    }
    ty.remove();
    if (!r.ok) { bubble("agent", "I couldn't reach the assistant just now. Please try again in a moment."); return; }
    const d = r.data, act = ACTIONS[d.action];
    const extra = d.action === "prefill_loan" && d.prefill ? prefillCard(d.prefill)
      : act ? el("div", { class: "acts" }, el("button", { class: "mini-cta", type: "button", text: act[0], onclick: act[1] })) : null;
    const m = bubble("agent", d.reply, extra);
    if (canSpeak(d.lang)) m.append(el("button", { class: "spk", type: "button", "aria-label": "Listen to this reply", text: "🔊", onclick: () => playReply(d.reply, d.lang) }));
    if (voiceTurn) {   /* spoken request: say the answer, then do exactly what a tap on the button would have done (open a screen / fill the form) */
      playReply(d.reply, d.lang);
      if (d.action === "prefill_loan" && d.prefill) { toast(d.reply); prefillLoan(d.prefill); return; }
      if (act) { toast(d.reply); act[1](); return; }
      showScreen("ich");
    }
    $("ic-sub").textContent = d.source === "keyword" ? "Backup helper (the AI model is unavailable)" : "Live AI model · insurance and loan help";
  }
  async function ensureIntro() {
    if ($("ic-chat").children.length) return;
    await ensureSession();
    bubble("agent", AINTRO || "Main Kaamezy hoon, ek AI assistant. I can help with health insurance and loans.");
    const h = $("ic-hints"); h.replaceChildren();
    ["How do I claim?", "I want a quote", "I need a loan", "Is my Aadhaar safe?"].forEach(t => h.append(el("button", { type: "button", text: t, onclick: () => chatSay(t) })));
  }
  async function openChat(prefill, from) {
    if (from) chatFrom = from;
    showScreen("ich");
    await ensureIntro();
    if (prefill) chatSay(prefill);
  }

  /* ---------------- voice ----------------
     The mic records a short clip, the server turns it into text with Sarvam (nothing is stored), and the text goes
     through the same assistant path as typing: PII masked, injection screened, the topic mapped to a screen in code.
     Voice can fill the form and open a screen. Consent, Key Facts, OTP, KYC and the guardrail stay taps on the screen. */
  let VCFG = null, REC = null, CURAUDIO = null;
  const authHeaders = async () => { try { const s = await sb.auth.getSession(); const t = s && s.data && s.data.session && s.data.session.access_token; return t ? { Authorization: "Bearer " + t } : {}; } catch (e) { return {}; } };
  async function voiceConfig() {
    if (VCFG) return VCFG;
    const r = await capi("GET", "/voice/config");
    VCFG = r.ok ? r.data : { tts_languages: [], language_aliases: {}, max_seconds: 20 };
    return VCFG;
  }
  const canSpeak = lang => !!VCFG && VCFG.tts_languages.includes((VCFG.language_aliases || {})[lang] || lang);
  function stopAudio() { if (CURAUDIO) { try { CURAUDIO.pause(); } catch (e) {} CURAUDIO = null; } }
  async function playReply(text, lang) {
    if (!canSpeak(lang) || !ASESS) return;
    stopAudio();
    try {
      const r = await fetch(API + "/api/consumer/voice/speak", { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, await authHeaders()),
        body: JSON.stringify({ session_id: ASESS, text, lang }) });
      if (!r.ok) { toast("Voice playback is not available right now."); return; }
      const url = URL.createObjectURL(await r.blob()), a = new Audio(url); CURAUDIO = a;
      a.onended = () => URL.revokeObjectURL(url);
      await a.play();
    } catch (e) { /* autoplay can be blocked: the speaker button on the reply still works */ }
  }
  async function startVoice() {
    if (REC) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) { toast("Voice isn't supported in this browser."); return; }
    const cfg = await voiceConfig(); stopAudio();
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { toast("Please allow the microphone to speak to Kaamezy."); return; }
    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(t => MediaRecorder.isTypeSupported(t)) || "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : {}), chunks = [];
    REC = { rec, stream, chunks, cancelled: false, timer: setTimeout(stopVoice, cfg.max_seconds * 1000) };
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = finishVoice;
    $("vs-title").textContent = "Listening…"; $("vs-said").textContent = ""; $("vs-stop").hidden = false; $("vs-orb").classList.add("live");
    openSheet("sheet-voice"); rec.start();
  }
  function stopVoice() { if (REC && REC.rec.state !== "inactive") REC.rec.stop(); }
  function cancelVoice() { if (REC) { REC.cancelled = true; stopVoice(); } closeSheets(); }
  async function finishVoice() {
    const R = REC; REC = null; if (!R) return;
    clearTimeout(R.timer); R.stream.getTracks().forEach(t => t.stop()); $("vs-orb").classList.remove("live");
    if (R.cancelled) return;
    const blob = new Blob(R.chunks, { type: (R.rec.mimeType || "audio/webm").split(";")[0] });
    if (blob.size < 1500) { toast("We could not hear anything. Please try again."); closeSheets(); return; }
    $("vs-title").textContent = "Understanding…"; $("vs-stop").hidden = true;
    let ok = false, data = null;
    try {
      const r = await fetch(API + "/api/consumer/voice/transcribe", { method: "POST", headers: Object.assign({ "Content-Type": blob.type }, await authHeaders()), body: blob });
      ok = r.ok; try { data = await r.json(); } catch (e) {}
      if (!ok) { toast((data && data.detail && data.detail.message) || "The voice service could not read that. Please try again."); closeSheets(); return; }
    } catch (e) { toast("The voice service could not be reached."); closeSheets(); return; }
    closeSheets();
    chatFrom = "home";
    await chatSay(data.understood || data.text, { voice: true, shown: data.text });
  }

  /* ---------------- account and language ----------------
     The app's fixed wording is translated ahead of time (web/i18n.js + web/i18n/*.json, built with Sarvam Translate); the
     assistant answers in the same language from its translated knowledge base (the server checks the language). */
  const appLang = () => (window.ChaukasI18n && ChaukasI18n.code !== "en-IN") ? ChaukasI18n.code : undefined;
  const uiLangQuery = () => appLang() ? "?lang=" + encodeURIComponent(appLang()) : "";
  const VOICE_HTML = `
<div class="sheet voicesheet" id="sheet-voice" hidden role="dialog" aria-label="Speak to Kaamezy">
  <div class="vorb" id="vs-orb"><i data-lucide="mic"></i></div>
  <h3 id="vs-title">Listening…</h3>
  <p class="s" id="vs-said" translate="no"></p>
  <p class="s" style="font-size:.85rem">Say what you need. For example: I need a loan, and tell me the amount, how long and your income. Or: I want health insurance.</p>
  <p class="s" style="font-size:.78rem">Your voice is sent to Sarvam to turn it into text. We do not keep the recording. You still confirm every step on the screen.</p>
  <div class="navrow"><button class="czbtn soft" id="vs-cancel">Cancel</button><button class="czbtn" id="vs-stop">Done</button></div>
</div>`;
  const ACCT_HTML = `
<section class="flow" id="acct" hidden aria-label="Account and language">
  ${bar("home", "Account and language", "Profile")}
  <div class="czbody">
    <div class="czcard">
      <b id="ac-name" translate="no" style="font-size:1.05rem"></b>
      <div class="s" id="ac-email" translate="no" style="margin:.15rem 0 .8rem"></div>
      <button class="czbtn soft" id="ac-pw"><i data-lucide="key-round"></i> Change password</button>
      <form id="ac-pwform" hidden autocomplete="off">
        <label class="auth-field" style="margin-top:.7rem">New password<input type="password" id="ac-pw1" minlength="8" autocomplete="new-password"></label>
        <label class="auth-field" style="margin-top:.7rem">Confirm password<input type="password" id="ac-pw2" minlength="8" autocomplete="new-password"></label>
        <div class="navrow"><button type="button" class="czbtn soft" id="ac-pwcancel">Cancel</button><button type="submit" class="czbtn">Update password</button></div>
      </form>
      <button class="czbtn soft" id="ac-out" style="margin-top:.6rem"><i data-lucide="log-out"></i> Sign out</button>
    </div>
    <p class="s" style="margin:1rem .2rem .6rem">Explanations, letters and the assistant use this language.</p>
    <div class="czcard menu langlist" id="ac-langs"></div>
  </div>
</section>`;
  function renderAccount() {
    const u = (typeof user !== "undefined" && user) || {}, md = u.user_metadata || {};
    $("ac-name").textContent = (typeof md.full_name === "string" && md.full_name) || (u.email || "").split("@")[0] || "You";
    $("ac-email").textContent = u.email || "";
    const I = window.ChaukasI18n, box = $("ac-langs"); box.replaceChildren();
    (I ? I.list() : [{ code: "en-IN", native: "English", english: "English" }]).forEach(l => {
      const on = !!I && I.code === l.code;
      box.append(el("button", { type: "button", class: "lrow", "aria-pressed": on ? "true" : "false", translate: "no", lang: l.code === "en-IN" ? "en" : l.code,
        onclick: async () => {
          if (!I || on) return;
          const ok = await I.set(l.code);
          if (!ok) { toast("That language could not be loaded. Please try again."); return; }
          renderAccount(); toast(l.native);
        } },
        el("span", { class: "ln" }, el("b", { text: l.native }), " · " + l.english), on ? el("span", { class: "tick", "aria-hidden": "true", text: "✓" }) : null));
    });
  }
  function openAccount() { renderAccount(); showScreen("acct"); }

  /* ---------------- wire up ---------------- */
  function init() {
    $("screen").insertAdjacentHTML("beforeend", HTML + ACCT_HTML + VOICE_HTML);
    const mic = $("mic-fab"); if (mic) mic.onclick = startVoice;
    $("vs-stop").onclick = stopVoice; $("vs-cancel").onclick = cancelVoice;
    $("sheetbg").addEventListener("click", () => { if (REC) cancelVoice(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && REC) cancelVoice(); });
    if (window.ChaukasI18n) ChaukasI18n.onChange(() => { ASESS = null; $("ic-chat").replaceChildren(); $("ic-hints").replaceChildren(); });   /* a new language starts a new chat greeting */
    voiceConfig();
    const av = $("avatar");
    if (av) { av.setAttribute("role", "button"); av.setAttribute("tabindex", "0"); av.setAttribute("aria-label", "Account and language"); av.style.cursor = "pointer";
      av.onclick = openAccount; av.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openAccount(); } }; }
    $("ac-out").onclick = () => $("home-out").click();
    $("ac-pw").onclick = () => { $("ac-pwform").hidden = false; $("ac-pw").hidden = true; $("ac-pw1").focus(); };
    $("ac-pwcancel").onclick = () => { $("ac-pwform").reset(); $("ac-pwform").hidden = true; $("ac-pw").hidden = false; };
    $("ac-pwform").addEventListener("submit", async e => {
      e.preventDefault();
      const a = $("ac-pw1").value, b = $("ac-pw2").value;
      if (a.length < 8) { toast("Use at least 8 characters."); return; }
      if (a !== b) { toast("The two passwords don’t match."); return; }
      try { const { error } = await sb.auth.updateUser({ password: a }); if (error) throw error; toast("Password updated."); $("ac-pwcancel").click(); }
      catch (err) { toast((err && err.message) || "We couldn’t update your password. Please try again."); }
    });
    document.querySelectorAll("[data-back]").forEach(b => b.addEventListener("click", () => showScreen(b.dataset.back)));
    document.querySelectorAll("#sheet-hihelp [data-close], #sheet-lnhelp [data-close]").forEach(b => b.addEventListener("click", closeSheets));
    const lt = $("open-hi"), ln = $("open-ln");
    if (lt) lt.onclick = () => { showScreen("hi"); refreshLanding(); };
    if (ln) ln.onclick = () => { showScreen("ln"); refreshLoanLanding(); };
    $("ln-help").onclick = () => openSheet("sheet-lnhelp");
    $("ln-new").onclick = () => startLoan(blankLoan());
    $("ln-sample").onclick = () => startLoan(SAMPLE_LOAN());
    $("ln-bridge-btn").onclick = () => bridgeLoan();
    $("ln-r-loans").onclick = openLoans; $("ln-r-act").onclick = () => openActivity("ln");
    $("ln-r-charges").onclick = () => openChat("What is APR?", "ln"); $("ln-ask").onclick = () => openChat(undefined, "ln");
    $("hi-help").onclick = () => openSheet("sheet-hihelp");
    $("hi-new").onclick = () => startWizard(blank());
    $("hi-sample").onclick = () => startWizard(SAMPLE());
    $("hi-r-new").onclick = () => startWizard(blank()); $("ln-r-new").onclick = () => startLoan(blankLoan());
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeSheets(); });
    $("hi-r-pol").onclick = openPolicies; $("hi-r-claims").onclick = openClaims; $("hi-r-act").onclick = () => openActivity("hi");
    $("hi-r-claim").onclick = () => openChat("How do I claim?", "hi"); $("hi-ask").onclick = () => openChat(undefined, "hi");
    $("ic-close").onclick = () => showScreen(chatFrom);
    $("ic-new").onclick = () => { $("ic-chat").replaceChildren(); ASESS = null; openChat(); };
    $("ic-form").addEventListener("submit", e => { e.preventDefault(); chatSay($("ic-text").value); });
    $("ic-human").onclick = () => chatSay("insaan se baat karao");
    $("ic-attach").onclick = () => toast("Attachments aren't part of this demo yet.");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    $("ic-mic").onclick = () => {
      if (!SR) { toast("Voice input isn't supported in this browser."); return; }
      const r = new SR(); r.lang = "hi-IN"; r.interimResults = false;
      r.onresult = e => chatSay(e.results[0][0].transcript); r.onerror = () => toast("Couldn't hear that. Please try again.");
      try { r.start(); toast("Listening…"); } catch (e) {}
    };
    refresh();
  }
  init();
})();
