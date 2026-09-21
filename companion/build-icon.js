// companion/build-icon.js — generates a unique app icon procedurally (no
// external asset, no image-conversion tool) so packaging never depends on
// downloading extra binaries. Produces build/icon.png (256x256, used at
// runtime for tray/window) and build/icon.ico (multi-size, used by
// electron-builder as the installer/exe icon).
//
// Mark: a rounded-square "chip" badge split diagonally between two colors
// (dual runtime) with a white core dot in the center (matches the tray's
// connected/disconnected status-dot language used elsewhere in the app).

const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

const COLOR_A = [26, 179, 148]; // teal
const COLOR_B = [74, 58, 255]; // indigo
const BG = [15, 23, 32]; // near-black chip base, shows through at the corners

function smoothstep(edge0, edge1, x) {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

function renderLogo(size) {
  const buf = Buffer.alloc(size * size * 4);
  const n = 4; // superellipse exponent for the rounded-square silhouette
  for (let y = 0; y < size; y++) {
    const ny = ((y + 0.5) / size) * 2 - 1;
    for (let x = 0; x < size; x++) {
      const nx = ((x + 0.5) / size) * 2 - 1;

      const shapeVal = Math.pow(Math.abs(nx), n) + Math.pow(Math.abs(ny), n);
      const shapeAlpha = 1 - smoothstep(0.85, 1.05, shapeVal);
      if (shapeAlpha <= 0) {
        buf.fill(0, (y * size + x) * 4, (y * size + x) * 4 + 4);
        continue;
      }

      const split = (nx - ny) / 2; // -1..1 diagonal blend factor
      const blend = smoothstep(-0.12, 0.12, split);
      let r = COLOR_A[0] + (COLOR_B[0] - COLOR_A[0]) * blend;
      let g = COLOR_A[1] + (COLOR_B[1] - COLOR_A[1]) * blend;
      let b = COLOR_A[2] + (COLOR_B[2] - COLOR_A[2]) * blend;

      // subtle vignette toward the rounded corners using the chip base color
      const corner = smoothstep(0.55, 1.0, shapeVal);
      r = r + (BG[0] - r) * corner * 0.35;
      g = g + (BG[1] - g) * corner * 0.35;
      b = b + (BG[2] - b) * corner * 0.35;

      // center "core" dot
      const dist = Math.sqrt(nx * nx + ny * ny);
      const core = 1 - smoothstep(0.16, 0.24, dist);
      const ring = smoothstep(0.24, 0.27, dist) * (1 - smoothstep(0.27, 0.32, dist));
      r = r + (255 - r) * core;
      g = g + (255 - g) * core;
      b = b + (255 - b) * core;
      r = r + (255 - r) * ring * 0.6;
      g = g + (255 - g) * ring * 0.6;
      b = b + (255 - b) * ring * 0.6;

      const i = (y * size + x) * 4;
      buf[i] = Math.round(r);
      buf[i + 1] = Math.round(g);
      buf[i + 2] = Math.round(b);
      buf[i + 3] = Math.round(255 * shapeAlpha);
    }
  }
  return buf;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const typeBuf = Buffer.from(type, "ascii");
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(data.length, 0);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([lenBuf, typeBuf, data, crcBuf]);
}

function encodePNG(size, rgba) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

  const ihdrData = Buffer.alloc(13);
  ihdrData.writeUInt32BE(size, 0);
  ihdrData.writeUInt32BE(size, 4);
  ihdrData[8] = 8; // bit depth
  ihdrData[9] = 6; // color type RGBA
  ihdrData[10] = 0;
  ihdrData[11] = 0;
  ihdrData[12] = 0;
  const ihdr = chunk("IHDR", ihdrData);

  const rowBytes = size * 4;
  const raw = Buffer.alloc((rowBytes + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (rowBytes + 1)] = 0; // filter: none
    rgba.copy(raw, y * (rowBytes + 1) + 1, y * rowBytes, y * rowBytes + rowBytes);
  }
  const idat = chunk("IDAT", zlib.deflateSync(raw));
  const iend = chunk("IEND", Buffer.alloc(0));

  return Buffer.concat([sig, ihdr, idat, iend]);
}

function buildIco(pngsBySize) {
  const sizes = Object.keys(pngsBySize)
    .map(Number)
    .sort((a, b) => a - b);
  const count = sizes.length;
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(count, 4);

  const entries = [];
  const images = [];
  let offset = 6 + count * 16;
  for (const size of sizes) {
    const png = pngsBySize[size];
    const entry = Buffer.alloc(16);
    entry[0] = size >= 256 ? 0 : size;
    entry[1] = size >= 256 ? 0 : size;
    entry[2] = 0;
    entry[3] = 0;
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(png.length, 8);
    entry.writeUInt32LE(offset, 12);
    entries.push(entry);
    images.push(png);
    offset += png.length;
  }
  return Buffer.concat([header, ...entries, ...images]);
}

const buildDir = path.join(__dirname, "build");
fs.mkdirSync(buildDir, { recursive: true });

const icoSizes = [16, 32, 48, 64, 128, 256];
const pngsBySize = {};
for (const size of icoSizes) {
  pngsBySize[size] = encodePNG(size, renderLogo(size));
}

fs.writeFileSync(path.join(buildDir, "icon.png"), pngsBySize[256]);
fs.writeFileSync(path.join(buildDir, "icon.ico"), buildIco(pngsBySize));

console.log("wrote", path.join(buildDir, "icon.png"));
console.log("wrote", path.join(buildDir, "icon.ico"));
