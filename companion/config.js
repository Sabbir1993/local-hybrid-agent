// Edit before packaging, or override via the A770_SERVER_URL env var at runtime.
module.exports = {
  SERVER_URL: process.env.A770_SERVER_URL || "http://127.0.0.1:8000",
};
