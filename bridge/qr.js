const QRCode = require('qrcode');
const url = process.argv[2];
const out = process.argv[3];
if (!url || !out) {
  console.error('usage: qr.js <url> <out.png>');
  process.exit(2);
}
QRCode.toFile(out, url, {
  type: 'png',
  width: 520,
  margin: 2,
  errorCorrectionLevel: 'M',
  color: { dark: '#000000', light: '#ffffff' }
}, err => {
  if (err) {
    console.error(String(err && err.message ? err.message : err));
    process.exit(1);
  }
  process.stdout.write(out);
});
