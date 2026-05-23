import type { Metadata } from "next";
import "./globals.css";
import { Header } from "@/components/Header";
import { Footer } from "@/components/Footer";
import { MethodologyBanner } from "@/components/MethodologyBanner";
import { DisclaimerModal } from "@/components/DisclaimerModal";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "WCED — War Carbon Emissions Dashboard",
  description:
    "Near-real-time CO₂ emission estimates from oil and fuel infrastructure fires during the 2026 Iran–US–Israel war.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="flex min-h-screen flex-col bg-slate-950">
        <Providers>
          <DisclaimerModal />
          <MethodologyBanner />
          <Header />
          <main className="flex-1">{children}</main>
          <Footer />
        </Providers>
      </body>
    </html>
  );
}
