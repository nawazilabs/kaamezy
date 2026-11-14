/* Run: node web/docread.test.js   (only the pure line-grouping function can run in Node; pdf.js and Tesseract need a browser) */
const assert = require("assert");
const { groupItems } = require("./docread.js");

const it = (str, x, y, width, height) => ({ str, transform: [height || 10, 0, 0, height || 10, x, y], width, height: height || 10 });

// 1. items arrive in any order: lines come out top to bottom, words left to right
let t = groupItems([it("Rahul Sharma", 120, 700, 60), it("Patient Name:", 40, 700, 70), it("CITY CARE HOSPITAL", 40, 760, 120)]);
assert.strictEqual(t, "CITY CARE HOSPITAL\nPatient Name: Rahul Sharma");

// 2. a small slip in the baseline stays on the same line (pdf.js often differs by a point or two)
t = groupItems([it("Sum Insured:", 40, 500, 60), it("Rs. 5,00,000", 110, 502, 55)]);
assert.strictEqual(t, "Sum Insured: Rs. 5,00,000");

// 3. a table: a wide gap is a column gap (three spaces), so "label   value" and "Bill No: 1   UHID: 2" stay readable
t = groupItems([it("Room Rent", 40, 400, 45), it("24,000", 400, 400, 30)]);
assert.strictEqual(t, "Room Rent   24,000");
t = groupItems([it("Bill No: CCH/9812", 40, 600, 80), it("UHID: 458213", 300, 600, 60)]);
assert.strictEqual(t, "Bill No: CCH/9812   UHID: 458213");

// 4. a word split into pieces by the PDF is joined back
t = groupItems([it("Hosp", 40, 300, 20), it("ital", 60, 300, 15)]);
assert.strictEqual(t, "Hospital");

// 5. blank items and junk are ignored; an empty page gives an empty string (the caller then OCRs it)
assert.strictEqual(groupItems([it("   ", 10, 10, 5), null, { str: 5 }]), "");
assert.strictEqual(groupItems([]), "");
assert.strictEqual(groupItems(undefined), "");

// 6. two lines closer than the tolerance are separate lines when their gap is bigger than the text height
t = groupItems([it("Line one", 40, 200, 40), it("Line two", 40, 185, 40)]);
assert.strictEqual(t, "Line one\nLine two");

console.log("docread.test.js: all passed");
