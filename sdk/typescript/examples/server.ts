import { ServerAlbertClient } from "../src/server.js";
const client = new ServerAlbertClient({ baseUrl: "http://127.0.0.1:8080", apiKey: "example-only" });
void client.memories.search({ query: "deployment decisions" });
