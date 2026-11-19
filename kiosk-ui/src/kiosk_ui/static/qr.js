// QR codes for the phone walkthrough (kiosk place cards, the phone page's own link, the printable lift stickers).
// Drawn in the browser with qrcode-generator (MIT): the vendored copy in /static/vendor/ when present (offline
// kiosk), otherwise from jsDelivr, like the 360-degree viewer.
"use strict";

const QR = (() => {
  let loading = null;

  function script(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.onload = resolve; s.onerror = reject;
      document.head.append(s);
    });
  }

  function load() {
    if (window.qrcode) return Promise.resolve();
    loading ||= script("/static/vendor/qrcode.js")
      .catch(() => script("https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"));
    return loading;
  }

  // Put a QR code for `text` into `el` (an <img> or a container). Resolves false when the library can't load.
  async function render(el, text, { cell = 6 } = {}) {
    try { await load(); } catch { return false; }
    const qr = window.qrcode(0, "M");
    qr.addData(text);
    qr.make();
    const img = el instanceof HTMLImageElement ? el : document.createElement("img");
    img.src = qr.createDataURL(cell, 2);
    img.alt = `QR code: ${text}`;
    img.className ||= "qr-img";
    if (img !== el) el.replaceChildren(img);
    return true;
  }

  return { render };
})();
