// Edit before packaging, or override via the A770_SERVER_URL env var at runtime.
const path = require("path");
try {
  require("dotenv").config({ path: path.join(__dirname, ".env") });
} catch (_) {}

module.exports = {
  SERVER_URL: process.env.A770_SERVER_URL || "https://petite-inflation-happen-baseball.trycloudflare.com",
};

