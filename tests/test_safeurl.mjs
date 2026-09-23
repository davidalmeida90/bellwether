// Scheme allowlist check for the front end. Run: node tests/test_safeurl.mjs
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const body = src.slice(src.indexOf('const safeUrl'), src.indexOf('const fmt ='));
globalThis.location = { origin: 'http://127.0.0.1:8077' };
const safeUrl = eval(body + '; safeUrl');

const cases = [
  ['https://arxiv.org/pdf/1802.03042', 'https://arxiv.org/pdf/1802.03042'],
  ['http://example.com/x', 'http://example.com/x'],
  ['mailto:a@b.com', 'mailto:a@b.com'],
  ['javascript:alert(document.cookie)', ''],
  ['JaVaScRiPt:alert(1)', ''],
  ['  javascript:alert(1)', ''],
  ['data:text/html,<script>alert(1)</script>', ''],
  ['vbscript:msgbox(1)', ''],
  ['file:///C:/Windows/System32', ''],
  ['', ''],
  [null, ''],
  [undefined, ''],
  ['not a url at all', 'http://127.0.0.1:8077/not%20a%20url%20at%20all'],
];

let pass = 0, fail = 0;
for (const [input, want] of cases) {
  const got = safeUrl(input);
  if (got === want) { pass++; } else {
    fail++;
    console.log(`FAIL ${JSON.stringify(input)}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
