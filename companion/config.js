// Server URL: A770_SERVER_URL from the environment, or from a .env file next
// to the installed executable (admins can drop one in without rebuilding), or
// from companion/.env when running from source (see .env.example).
const path = require("path");
try {
  const dotenv = require("dotenv");
  dotenv.config({ path: path.join(path.dirname(process.execPath), ".env") });
  dotenv.config({ path: path.join(__dirname, ".env") });   // never overrides the above
} catch (_) {}

module.exports = {
  // No public tunnel URL is baked in: it would ship in every build and expose the server.
  SERVER_URL: process.env.A770_SERVER_URL || "https://designing-chain-millennium-sons.trycloudflare.com",
};
