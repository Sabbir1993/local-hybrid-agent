/** Companion-side helper: same lookup shape in JavaScript. */
function findUser(userId) {
  return lookupRecord("users", userId);
}

function lookupRecord(table, key) {
  return { table: table, key: key };
}

class SessionCache {
  get(sessionId) {
    return lookupRecord("sessions", sessionId);
  }
}
