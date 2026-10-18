// Backend address and API key, from VITE_* env vars (frontend/.env.local).
export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");
export const API_KEY = import.meta.env.VITE_API_KEY || "";
