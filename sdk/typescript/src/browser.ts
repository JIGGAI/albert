import { AlbertClient, AlbertConfigurationError, type AlbertClientOptions } from "./client.js";

export interface BrowserAlbertClientOptions extends Omit<AlbertClientOptions, "authorization" | "headers"> {
  getAccessToken: () => string | Promise<string>;
  headers?: Readonly<Record<string, string>>;
}

export class BrowserAlbertClient extends AlbertClient {
  constructor(options: BrowserAlbertClientOptions) {
    if ("apiKey" in options) throw new AlbertConfigurationError("Server API keys cannot be used by the browser client; provide a short-lived access token callback");
    const { getAccessToken, ...rest } = options;
    super({ ...rest, authorization: async () => {
      const token = (await getAccessToken()).trim();
      if (!token) throw new AlbertConfigurationError("getAccessToken returned an empty token");
      return `Bearer ${token}`;
    } });
  }
}
