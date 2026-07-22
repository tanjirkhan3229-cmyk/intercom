import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Relay",
  description: "Customer messaging, help center, and AI support — one thread model.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
