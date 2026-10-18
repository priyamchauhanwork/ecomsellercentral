import { describe, expect, it } from "vitest";
import { thumbnailUrl } from "./format";

describe("thumbnailUrl", () => {
  it("asks Shopify's CDN for a resized image, keeping its other parameters", () => {
    const url = "https://cdn.shopify.com/s/files/1/0787/files/watch.png?v=1792072424";
    expect(thumbnailUrl(url, 96)).toBe(
      "https://cdn.shopify.com/s/files/1/0787/files/watch.png?v=1792072424&width=96",
    );
    expect(thumbnailUrl(`${url}&width=2000`, 96)).toContain("width=96");
    expect(thumbnailUrl(`${url}&width=2000`, 96)).not.toContain("width=2000");
  });

  it("leaves other hosts and unparseable values alone", () => {
    expect(thumbnailUrl("https://images.example.com/a.jpg", 96)).toBe("https://images.example.com/a.jpg");
    expect(thumbnailUrl("not a url", 96)).toBe("not a url");
  });
});
