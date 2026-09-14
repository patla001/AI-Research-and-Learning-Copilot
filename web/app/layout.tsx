import type { Metadata, Viewport } from "next";
import { Instrument_Sans, Literata } from "next/font/google";
import "./globals.css";

// next/font downloads at build time and serves from our own origin - a runtime
// font CDN request is one more thing the Databricks Apps proxy can block.
//
// Literata was drawn for long-form reading on screens, so it carries what the
// learner reads: paper titles, abstracts, copilot answers. Instrument Sans runs
// the interface around it.
const reading = Literata({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-reading",
  display: "swap",
});

const ui = Instrument_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ui",
  display: "swap",
});

const ASSET_PREFIX = process.env.NEXT_PUBLIC_ASSET_PREFIX ?? "";

export const metadata: Metadata = {
  icons: { icon: `${ASSET_PREFIX}/icon.svg` },
  title: "Reading Route - research and learning copilot",
  description: "Turn a learning goal into papers, a sequenced reading plan, and grounded answers with citations.",
};

export const viewport: Viewport = {
  themeColor: "#e9eee8",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${reading.variable} ${ui.variable}`}>
      <body>{children}</body>
    </html>
  );
}
