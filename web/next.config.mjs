/** @type {import('next').NextConfig} */

// Static export: `next build` emits plain HTML/JS that Flask serves from
// ../static. There is no Node process in production, so the whole console is a
// client component calling the Flask API on the same origin - which is what
// lets it inherit the Databricks Apps OAuth session.
//
// assetPrefix applies to the production build only: Flask mounts the export at
// /static, while `next dev` serves chunks from the root.
const isProd = process.env.NODE_ENV === "production";

export default {
  turbopack: { root: import.meta.dirname },
  output: "export",
  assetPrefix: isProd ? "/static" : undefined,
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_ASSET_PREFIX: isProd ? "/static" : "" },
  reactStrictMode: true,
};
