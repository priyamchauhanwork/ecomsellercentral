import { API_KEY, API_URL } from "./config";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// FastAPI errors carry `detail`: a string, an object with `message`, or a list of
// validation errors.
function describeError(body, status) {
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).join("; ");
  if (detail?.message) return detail.message;
  return `Request failed (${status})`;
}

// Calls the backend and returns the parsed JSON. Empty query values are left out.
// Throws ApiError with a readable message; aborted requests rethrow the AbortError.
export async function api(path, { params, signal, method = "GET", body } = {}) {
  const url = new URL(API_URL + path);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  }
  const headers = { Accept: "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(`Can't reach the server at ${API_URL}. Is the backend running?`, 0);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(describeError(data, response.status), response.status);
  return data;
}
