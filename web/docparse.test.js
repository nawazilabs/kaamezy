/* Run: node web/docparse.test.js */
const assert = require("assert");
const { parse } = require("./docparse.js");
const NOW = new Date("2026-11-07T00:00:00Z");

// 1. clean PAN card
let r = parse("INCOME TAX DEPARTMENT\nGOVT. OF INDIA\nPermanent Account Number Card\nABCDE1234F\nName\nDEMO KUMAR SHARMA\nFather's Name\nRAMESH SHARMA\nDate of Birth\n01/01/1990\nSignature", NOW);
assert.strictEqual(r.pan, "ABCDE1234F");
assert.strictEqual(r.name, "Demo Kumar Sharma");
assert.strictEqual(r.dob, "1990-01-01");
assert.strictEqual(r.age, 36);
assert.strictEqual(r.aadhaarSeen, false);

// 2. OCR look-alike errors in the PAN (I for 1, O for 0, S for 5)
r = parse("Permanent Account Number\nABCDEI23SF\nName\nASHA VERMA\n12-03-1985", NOW);
assert.strictEqual(r.pan, "ABCDE1235F");
assert.strictEqual(r.name, "Asha Verma");
assert.strictEqual(r.age, 41);

// 3. Aadhaar front: the number is detected so it can be ignored, and is never returned
r = parse("Government of India\nDemo Sharma\nDOB: 05/06/1992\nMALE\n1234 5678 9012", NOW);
assert.strictEqual(r.aadhaarSeen, true);
assert.strictEqual(r.name, "Demo Sharma");
assert.strictEqual(r.gender, "M");
assert.strictEqual(r.age, 34);
assert.ok(!JSON.stringify(r).includes("9012"), "Aadhaar digits must not appear in the result");
assert.strictEqual(r.pan, null);

// 4. FEMALE is not mistaken for MALE
assert.strictEqual(parse("Anita Rao\nDOB 02/02/2000\nFEMALE", NOW).gender, "F");

// 5. words that happen to be 10 letters are not PANs
assert.strictEqual(parse("INCOME TAX DEPARTMENT", NOW).pan, null);

// 6. garbage in, nothing out
r = parse("@@ ## 12", NOW);
assert.deepStrictEqual([r.pan, r.name, r.dob, r.gender, r.aadhaarSeen], [null, null, null, null, false]);

// 7. impossible dates are ignored
assert.strictEqual(parse("DOB 31/02/1990 and 15/08/2090", NOW).dob, null);

// 8. Aadhaar with a Hindi line, OCR noise around the name, a space-separated date and O for 0
r = parse("Government of India\nशाहनवाज\n~ Shahnawaz Shah |\nजन्म तिथि / DOB: O5 06 1995\nMALE / पुरुष\n4321 8765 2109", NOW);
assert.strictEqual(r.name, "Shahnawaz Shah");
assert.strictEqual(r.dob, "1995-06-05");
assert.strictEqual(r.gender, "M");
assert.ok(!JSON.stringify(r).includes("2109"));

// 9. year of birth only: an age, never an invented date of birth
r = parse("Government of India\nAnita Rao\nYear of Birth : 1990\nFEMALE", NOW);
assert.deepStrictEqual([r.dob, r.age, r.yearOnly, r.name], [null, 36, true, "Anita Rao"]);

console.log("docparse: all tests passed");
