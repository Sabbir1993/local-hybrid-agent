// TSX fixture: JSX nodes + components so the tsx parser gets real coverage.
import { TokenVault } from "./types";

function Badge({ label }: { label: string }) {
  return <span className="badge">{label}</span>;
}

export const VaultView = () => {
  const vault = new TokenVault();
  vault.add("k1");
  return <Badge label={`count=${vault.keys.length}`} />;
};
