import type { NextConfig } from "next";

// Static export served by the FastAPI process (same origin: cookies + CSRF work without CORS).
const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
