/*
 * Uji logika ekstraksi DOM tanpa membuka Facebook.
 *
 * Skrip ini mengambil dua fungsi JavaScript dari Python
 * (`_CONTAINER_JS`, `_POST_BODY_JS`), menjalankannya pada HTML tiruan
 * (satu postingan + dua komentar + satu postingan lain), lalu memastikan:
 *
 *   1. kontainer yang dipilih hanya memuat SATU postingan,
 *   2. isi postingan terbaca,
 *   3. komentar dan tombol "Reply" tidak ikut,
 *   4. postingan lain tidak tercampur.
 *
 * Ini BUKAN pengganti POC di Facebook: struktur HTML di sini buatan sendiri.
 * Gunanya menangkap kesalahan logika JS sebelum Anda menjalankan POC.
 *
 * Cara pakai (butuh Node.js + jsdom, tidak dipakai aplikasi):
 *   npm install jsdom
 *   node scripts/check_extraction_dom.cjs
 *
 * Python yang dipakai: env FBW_PYTHON, default "python".
 * Di Windows dengan venv:
 *   $env:FBW_PYTHON=".\.venv\Scripts\python.exe"; node scripts/check_extraction_dom.cjs
 */

"use strict";

const path = require("path");
const { execFileSync } = require("child_process");
const { JSDOM } = require("jsdom");

const python = process.env.FBW_PYTHON || "python";
const repoRoot = path.resolve(__dirname, "..");

const dump = `
import json
from fbworkspace.browser.playwright_fb import _CONTAINER_JS, _POST_BODY_JS
print(json.dumps({"container": _CONTAINER_JS, "body": _POST_BODY_JS}))
`;

const source = JSON.parse(
  execFileSync(python, ["-c", dump], { cwd: repoRoot, encoding: "utf8" })
);

const html = `<!doctype html><html><body>
<div role="feed">
  <div id="post1">
    <a href="/groups/g/posts/111/"><span>2h</span></a>
    <div dir="auto">Dijual iPhone X 64GB mulus 5jt COD Tangerang</div>
    <div><div>Like</div><div>Comment</div><div>Share</div></div>
    <ul>
      <li><div role="article"><a href="/groups/g/posts/111/?comment_id=222">1h</a><div>brp</div><div>Reply</div></div></li>
      <li><div role="article"><a href="/groups/g/posts/111/?comment_id=333">1h</a><div>masih?</div><div>Reply</div></div></li>
    </ul>
  </div>
  <div id="post2">
    <a href="/groups/g/posts/444/"><span>3h</span></a>
    <div dir="auto">Cari iPhone X budget 3jt area Bandung</div>
  </div>
</div>
</body></html>`;

const dom = new JSDOM(html);
const { window } = dom;

// jsdom belum punya innerText; samakan dengan textContent agar mirip browser.
Object.defineProperty(window.HTMLElement.prototype, "innerText", {
  get() {
    return this.textContent;
  },
  set(value) {
    this.textContent = value;
  },
});

const containerFn = eval("(" + source.container + ")");
const bodyFn = eval("(" + source.body + ")");

const anchor1 = window.document.querySelector('a[href="/groups/g/posts/111/"]');
const container1 = containerFn(anchor1);
const body1 = bodyFn(container1);

const anchor2 = window.document.querySelector('a[href="/groups/g/posts/444/"]');
const container2 = containerFn(anchor2);
const body2 = bodyFn(container2);

const checks = {
  "kontainer terdeteksi": !!container1,
  "kontainer hanya memuat satu postingan": !!container1 && container1.id === "post1",
  "isi postingan terbaca": body1.includes("Dijual iPhone X 64GB mulus"),
  "komentar 'brp' dibuang": !body1.includes("brp"),
  "komentar 'masih?' dibuang": !body1.includes("masih?"),
  "tombol Reply dibuang": !/Reply/.test(body1),
  "postingan lain tidak ikut": !body1.includes("Cari iPhone X budget"),
  "postingan kedua: kontainer benar": !!container2 && container2.id === "post2",
  "postingan kedua: isi benar":
    body2.includes("Cari iPhone X budget 3jt") && !body2.includes("Dijual iPhone X"),
};

let failed = 0;
for (const [label, ok] of Object.entries(checks)) {
  console.log((ok ? "OK   " : "FAIL ") + label);
  if (!ok) failed += 1;
}

console.log(failed ? `\n${failed} pemeriksaan gagal.` : "\nSemua pemeriksaan lulus.");
process.exit(failed ? 1 : 0);
