const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
// This file is copied into the repository's tests directory before execution.
const source = fs.readFileSync(path.join(__dirname, '../src/garmin_mcp/dashboard.py'), 'utf8');
const helper = source.slice(source.indexOf('function fitnessWindow('), source.indexOf('function drawFitness('));
const windowFor = new Function(helper + '; return fitnessWindow;')();
function history(end) {
  const last = new Date(end + 'T00:00:00Z');
  return Array.from({length: 800}, (_, i) => ({date: new Date(+last - (799-i)*86400000).toISOString().slice(0,10)}));
}
for (const [days, expected] of [[30,'2026-08-15'],[90,'2026-06-15'],[180,'2026-03-15'],[365,'2025-09-15'],[731,'2024-09-15']]) {
  assert.equal(windowFor(history('2026-09-15'),days)[0].date, expected);
}
assert.equal(windowFor(history('2024-03-31'),30)[0].date,'2024-02-29');
assert.equal(windowFor(history('2025-03-31'),30)[0].date,'2025-02-28');
assert.equal(windowFor(history('2024-02-29'),365)[0].date,'2023-02-28');
assert.equal(windowFor(history('2025-02-28'),731)[0].date,'2023-02-28');
assert.deepEqual(windowFor([],30),[]);
const short=[{date:'2026-09-14'},{date:'2026-09-15'}];
assert.deepEqual(windowFor(short,30),short);
// Parse all inline dashboard scripts as JavaScript, catching syntax errors.
for (const match of source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new Function(match[1]);
console.log('Fitness calendar periods, leap days, partial history and dashboard JavaScript syntax passed.');
