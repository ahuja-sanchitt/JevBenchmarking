// Zero-dependency static server for frontend work before the FastAPI backend exists.
// Serves static/ and maps / like the real app. /api/* returns 404,
// so the pages fall back to the in-browser mock.   Usage: node dev-server.mjs [port]
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "static");
const port = Number(process.argv[2]) || 5173;
const types = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png" };
const routes = { "/": "index.html" };

createServer(async (req, res) => {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const file = normalize(join(root, routes[path] ?? path));
  if (!file.startsWith(root) || path.startsWith("/api/")) { res.writeHead(404).end(); return; }
  try {
    const body = await readFile(file);
    res.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream", "cache-control": "no-store" }).end(body);
  } catch {
    res.writeHead(404).end();
  }
}).listen(port, () => console.log(`http://localhost:${port}`));
