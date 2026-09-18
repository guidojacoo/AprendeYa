/* Run the dashboard's render() against a payload, with a stub DOM.
 *
 * The page had no test of any kind. An undeclared variable — `bfClause`, left
 * behind when the bidding funnel was rewritten — shipped, and because every
 * section ran as one straight-line block it did not break one section, it
 * blanked the whole page: "Error: Can't find variable: bfClause". Python tests
 * cannot see that. This can: it executes the real script out of index.html and
 * calls render() for real.
 *
 * Usage: node render_harness.js <index.html> <payload.json>
 * Prints nothing and exits 0 on success; prints the failure and exits 1.
 */
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).join('\n');

const el = () => {
  const node = {
    innerHTML: '', textContent: '', value: '', disabled: false,
    style: {}, dataset: {},
    classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}},
    addEventListener(){}, removeEventListener(){}, appendChild(){},
    closest(){return null;}, querySelectorAll(){return [];},
    scrollWidth: 0, scrollLeft: 0, clientWidth: 0,
    get lastElementChild(){return el();},
    get firstElementChild(){return el();},
  };
  return node;
};

const failures = [];
const sandbox = {
  console: {log(){}, warn(){},
            error(...a){failures.push(a.map(String).join(' '));}},
  document: {
    getElementById: () => el(),
    querySelectorAll: () => [],
    addEventListener(){}, hidden: false, body: el(),
  },
  localStorage: {getItem: () => null, setItem(){}, removeItem(){}},
  /* Nothing may reach the network or schedule a loop: this is a render test. */
  fetch: () => new Promise(() => {}),
  setTimeout: () => 0, clearTimeout(){}, setInterval: () => 0, clearInterval(){},
  addEventListener(){}, scrollTo(){}, requestAnimationFrame: () => 0,
  navigator: {language: 'es-ES'}, location: {href: '', reload(){}},
  Date, Math, JSON, Number, String, Object, Array, Intl, isNaN, parseInt,
  parseFloat, encodeURIComponent, decodeURIComponent, Promise, Error,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
try {
  new vm.Script(script).runInContext(sandbox);
} catch (err) {
  console.error('the page did not even load: ' + err.message);
  process.exit(1);
}

if (typeof sandbox.render !== 'function') {
  console.error('render() is not defined');
  process.exit(1);
}

try {
  sandbox.render(payload);
} catch (err) {
  console.error('render() threw: ' + err.message);
  process.exit(1);
}

/* A section that throws is caught by `part` now — which is the point — but it
 * must still fail the test. A caught error is a broken section, not a pass. */
if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
