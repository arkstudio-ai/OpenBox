/** Persist only automation page names/target IDs, scoped to one Chrome launch. */
import { mkdirSync, lstatSync, readFileSync, writeFileSync, renameSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { createHash, randomUUID } from "node:crypto";

export class LocalPageState {
  readonly pages = new Map<string, string>();
  private endpoint: string | null = null;
  private readonly file: string;

  constructor(chromeBase: string, directory = process.env.DEV_BROWSER_STATE_DIR ?? join(homedir(), ".local/state/openbox/dev-browser")) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const stat = lstatSync(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) ||
        (process.getuid && stat.uid !== process.getuid())) {
      throw new Error("Browser page state directory must be private and owned by the relay user");
    }
    this.file = join(directory, `pages-${createHash("sha256").update(chromeBase).digest("hex").slice(0, 20)}.json`);
  }

  restore(endpoint: string): void {
    if (this.endpoint === endpoint) return;
    let saved: { endpoint: string; pages: [string, string][] } | undefined;
    try {
      const stat = lstatSync(this.file);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1024 * 1024 || (stat.mode & 0o077) ||
          (process.getuid && stat.uid !== process.getuid())) throw new Error("Untrusted browser page state");
      saved = JSON.parse(readFileSync(this.file, "utf8"));
      if (!saved || typeof saved.endpoint !== "string" || !Array.isArray(saved.pages) ||
          !saved.pages.every(row => Array.isArray(row) && row.length === 2 && row.every(v => typeof v === "string"))) {
        throw new Error("Invalid browser page state; refusing to silently create duplicate pages");
      }
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
    }
    this.pages.clear();
    if (saved?.endpoint === endpoint) for (const [name, id] of saved.pages) this.pages.set(name, id);
    this.endpoint = endpoint;
    this.save();
  }

  save(): void {
    if (!this.endpoint) return;
    const stage = `${this.file}.${randomUUID()}.tmp`;
    try {
      writeFileSync(stage, JSON.stringify({ endpoint: this.endpoint, pages: [...this.pages] }), { flag: "wx", mode: 0o600 });
      renameSync(stage, this.file);
    } finally {
      try { unlinkSync(stage); } catch (err) { if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err; }
    }
  }
}
