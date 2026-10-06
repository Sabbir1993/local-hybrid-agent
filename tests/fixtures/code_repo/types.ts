// TypeScript fixture: symbols under TS syntax (interfaces, typed params,
// classes with typed methods, arrow functions). Mirrors the JS fixture so the
// TS path is exercised to the same depth as the JS path.
export interface Session { userId: number; token: string }

export function issueToken(s: Session): string {
  return encodeToken(s.userId, s.token);
}

const encodeToken = (userId: number, token: string): string =>
  `${userId}:${token}`;

export class TokenVault {
  private keys: string[] = [];
  add(key: string): number {
    return this.keys.push(key);
  }
  lookup(userId: number): string | null {
    return this.keys[userId] ?? null;
  }
}
