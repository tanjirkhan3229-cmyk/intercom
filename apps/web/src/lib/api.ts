import { RelayClient } from "@relay/sdk-ts";

const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Shared browser-side API client. Access token is attached by the auth layer (P0.1). */
export const api = new RelayClient({ baseUrl });
