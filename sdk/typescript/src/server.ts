import { AlbertClient, AlbertConfigurationError, type AlbertClientOptions } from "./client.js";

export interface ServerAlbertClientOptions extends Omit<AlbertClientOptions, "authorization"> { apiKey: string }

export class ServerAlbertClient extends AlbertClient {
  constructor(options: ServerAlbertClientOptions) {
    const key = options.apiKey.trim();
    if (!key) throw new AlbertConfigurationError("apiKey is required for the server client");
    const { apiKey: _apiKey, ...rest } = options;
    super({ ...rest, authorization: () => `Bearer ${key}` });
  }
}
