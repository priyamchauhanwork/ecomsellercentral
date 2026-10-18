import { useState } from "react";
import { thumbnailUrl } from "../format";

// A product picture (resized on Shopify's CDN), or a neutral placeholder when there is no
// image or it fails to load. Give it key={url} so a new image gets a fresh attempt.
export default function ProductThumbnail({ url, name, size = 48 }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed) {
    return (
      <div
        className="thumbnail placeholder"
        role="img"
        aria-label={`No image for ${name}`}
        style={{ width: size, height: size }}
      >
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
          <path
            fill="currentColor"
            d="M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm1 2v9.6l3.3-3.3a1 1 0 0 1 1.4 0L13 14.6l1.3-1.3a1 1 0 0 1 1.4 0L18 15.6V6H6Zm9 3a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z"
          />
        </svg>
      </div>
    );
  }
  return (
    <img
      className="thumbnail"
      style={{ width: size, height: size }}
      src={thumbnailUrl(url, size * 2)}
      alt={name}
      width={size}
      height={size}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
