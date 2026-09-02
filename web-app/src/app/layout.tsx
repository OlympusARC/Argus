import type { Metadata } from "next";
import { Crimson_Text, Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

/**
 * A serif for the wordmark only. The table is dense and set in the UI sans;
 * giving the name a different voice is what makes it read as a mark rather
 * than as the first row of the page.
 */
const crimson = Crimson_Text({
  variable: "--font-crimson",
  subsets: ["latin"],
  weight: ["400", "600", "700"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Argus",
  description: "Open engineering, product and adjacent roles, aggregated from nine ATSs.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${geistMono.variable} ${crimson.variable} h-full antialiased`}
      /**
       * Committed to dark rather than following the system. The palette below
       * is tuned for it -- the family tones are picked to sit on black, and
       * they wash out on white.
       */
      style={{ colorScheme: "dark" }}
    >
      <body className="bg-background text-foreground flex min-h-full flex-col">{children}</body>
    </html>
  );
}
