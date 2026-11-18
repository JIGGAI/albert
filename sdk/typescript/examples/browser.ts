import { BrowserAlbertClient } from "../src/browser.js";
const client = new BrowserAlbertClient({ baseUrl: "https://brain.example.com", getAccessToken: async () => "short-lived-token" });
void client.context.sessionStart({ query: "current project" });
