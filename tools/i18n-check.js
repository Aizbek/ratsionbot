/* Renders every screen in RU and UZ inside a stub DOM and checks for leaks:
   untranslated Russian, unfilled {placeholders}, undefined, missing keys. */
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const js = html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];

/* ---------------- minimal DOM stub ---------------- */
function el() {
  const e = {
    innerHTML: '', textContent: '', value: '', scrollTop: 0, dataset: {},
    style: { setProperty() {}, opacity: '' },
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute: () => null,
    scrollTo() {}, remove() {}, appendChild() {}, focus() {}, click() {},
    firstElementChild: null, children: [],
  };
  return e;
}
const store = {};
const sandbox = {
  console,
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  document: {
    documentElement: el(),
    title: '',
    getElementById: () => el(),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => el(),
    addEventListener() {},
  },
  window: { Telegram: undefined, open() {} },
  requestAnimationFrame: cb => cb(),
  setTimeout: () => 0,
  clearTimeout: () => {},
  alert: () => {},
  fetch: () => Promise.reject(new Error('no network in test')),
  AbortController: function () { this.signal = {}; this.abort = () => {}; },
  Date, Math, JSON, Number, String, Object, Array, RegExp, Error, isNaN, parseInt, parseFloat,
  Promise, Intl,
};
sandbox.globalThis = sandbox;

const EXPORTS = `
globalThis.__X = { SCREENS, L, setLang, S, CATEGORIES, ACT, calcStep, orderStep,
  t, sum, statusLabel, durLabel, goalLabel, payLabel, payLabelRu, tierForKcal,
  computeCalories, langSwitch, applyStaticLang, writeOrders };
globalThis.__setLang = function(l){ LANG = l; };
`;
vm.createContext(sandbox);
vm.runInContext(js + '\n' + EXPORTS, sandbox, { filename: 'app.js' });
const X = sandbox.__X;

/* ---------------- 1. key parity ---------------- */
const ruK = Object.keys(X.L.ru), uzK = Object.keys(X.L.uz);
const missingUz = ruK.filter(k => !(k in X.L.uz));
const extraUz = uzK.filter(k => !(k in X.L.ru));
console.log(`keys: ru=${ruK.length} uz=${uzK.length}`);
if (missingUz.length) console.log('  MISSING in uz:', missingUz.join(', '));
if (extraUz.length) console.log('  EXTRA in uz (no ru twin):', extraUz.join(', '));

/* ---------------- 2. render every screen in both languages ---------------- */
const CYR = /[А-яЁё]/;

function seedState() {
  const S = X.S;
  const cat = X.CATEGORIES[0], tier = cat.tiers[4];
  S.calc = { gender: 'm', age: 30, height: 178, weight: 80, activity: 'mid',
             goal: 'loss', bodyDone: true, result: 2100, bmr: 1750, tdee: 2700,
             recCat: cat, recTier: tier };
  S.order = { cat, tier, duration: '15 дней', days: 15, date: '20.10.2026',
              time: '08:30', timeH: 8, timeM: 30, detailsDone: true, pay: 'online',
              address: 'Ташкент, Чиланзар, 12-45', name: 'Иван', surname: 'Петров',
              phone: '+998901234567', height: 178, weight: 80,
              orderNo: 'R-171026-1042', delivered: true, failReason: null };
  store['ratsion_orders'] = JSON.stringify([{
    no: 'R-171026-1042', catId: cat.id, category: cat.name, kcal: '1600–1800',
    duration: '15 дней', days: 15, date: '20.10.2026', time: '08:30',
    pay: 'online', total: 2875000, ts: Date.now(), status: 'Доставляется',
  }]);
}

const SCREEN_NAMES = ['menu', 'about', 'catering', 'promo', 'contacts',
  'contactManager', 'calc', '_calcResult', 'orders', 'programs', 'tiers',
  'order', 'success'];

// the multi-step screens, forced through each of their steps
const STEPS = {
  calc:  [{ gender: null, bodyDone: false, activity: null, goal: null, result: null },
          { gender: 'm', bodyDone: false, activity: null, goal: null, result: null },
          { gender: 'm', bodyDone: true, activity: null, goal: null, result: null },
          { gender: 'm', bodyDone: true, activity: 'mid', goal: null, result: null }],
  order: [{ duration: null, date: null, time: null, detailsDone: false, pay: null },
          { duration: '15 дней', days: 15, date: null, time: null, detailsDone: false, pay: null },
          { duration: '15 дней', days: 15, date: '20.10.2026', time: null, detailsDone: false, pay: null },
          { duration: '15 дней', days: 15, date: '20.10.2026', time: '08:30', detailsDone: false, pay: null },
          { duration: '15 дней', days: 15, date: '20.10.2026', time: '08:30', detailsDone: true, pay: null },
          { duration: '15 дней', days: 15, date: '20.10.2026', time: '08:30', detailsDone: true, pay: 'online' }],
};

let problems = 0;
function check(label, out) {
  const issues = [];
  // Cyrillic is legitimate only in data-dur="" (the manager-facing payload label)
  let stripped = out.replace(/data-dur="[^"]*"/g, '');
  // the seeded CUSTOMER DATA is Russian on purpose - it must never be translated
  ['Ташкент, Чиланзар, 12-45','Иван','Петров'].forEach(v=>{ stripped = stripped.split(v).join('<DATA>'); });
  if (CYR.test(stripped)) {
    const m = stripped.match(new RegExp('[^<>"]*' + CYR.source + '[^<>"]*', 'g')) || [];
    issues.push('Russian left: ' + JSON.stringify([...new Set(m)].slice(0, 6)));
  }
  if (/\{(n|p|no|msg|i|phone)\}/.test(out)) issues.push('unfilled placeholder');
  // NOTE: `this.onerror=null` in the logo fallback is legitimate markup, so only `undefined` is a defect
  if (/undefined/.test(out)) issues.push('undefined in output');
  if (issues.length) { problems += issues.length; console.log(`  FAIL ${label}: ${issues.join(' | ')}`); }
  return issues.length === 0;
}

for (const lang of ['ru', 'uz']) {
  console.log(`\n--- ${lang.toUpperCase()} ---`);
  sandbox.__setLang(lang);
  let ok = 0, total = 0;
  for (const name of SCREEN_NAMES) {
    seedState();
    if (STEPS[name]) {
      STEPS[name].forEach((patch, i) => {
        seedState();
        Object.assign(name === 'calc' ? X.S.calc : X.S.order, patch);
        total++;
        const out = X.SCREENS[name]();
        if (lang === 'ru' || check(`${name}[step ${i}]`, out)) ok++;
        if (lang === 'uz' && CYR.test(out.replace(/data-dur="[^"]*"/g, ''))) {} // reported above
      });
      continue;
    }
    total++;
    const out = X.SCREENS[name]();
    if (lang === 'ru' || check(name, out)) ok++;
  }
  // the failure variant of the success screen
  for (const fr of ['network', 'timeout', 'server']) {
    seedState(); X.S.order.delivered = false; X.S.order.failReason = fr;
    total++;
    if (lang === 'ru' || check(`success[${fr}]`, X.SCREENS.success())) ok++;
  }
  console.log(`  rendered ${total} screens/steps, ${lang === 'ru' ? 'ru baseline' : ok + '/' + total + ' clean'}`);
}

/* ---------------- 3. manager payload must stay Russian ---------------- */
console.log('\n--- manager-facing values ---');
sandbox.__setLang('uz');
seedState();
console.log('  cat.name   :', X.CATEGORIES[0].name);
console.log('  payLabelRu :', X.payLabelRu('online'), '| payLabel(uz):', X.payLabel('online'));
console.log('  status(uz) :', X.statusLabel('Доставляется'));
sandbox.__setLang('ru');
console.log('  status(ru) :', X.statusLabel('Доставляется'));

/* ---------------- 4. the switch itself ---------------- */
console.log('\n--- switch ---');
sandbox.__setLang('ru');
console.log('  pill(ru):', X.langSwitch().replace(/\s+/g, ' ').match(/class="lang-b[^"]*" data-lang="\w+"/g).join('  '));
sandbox.__setLang('uz');
console.log('  pill(uz):', X.langSwitch().replace(/\s+/g, ' ').match(/class="lang-b[^"]*" data-lang="\w+"/g).join('  '));
console.log('  sum(uz) :', (sandbox.__setLang('uz'), X.sum(265000)));
console.log('  sum(ru) :', (sandbox.__setLang('ru'), X.sum(265000)));

console.log(`\n==> ${problems === 0 && !missingUz.length ? 'ALL CHECKS PASSED' : problems + ' problem(s)'}`);
process.exit(problems === 0 && !missingUz.length ? 0 : 1);
