import type { Metadata } from "next";
import "./globals.css";


export const metadata: Metadata = {
  title: "NotebookLens Review Workspace",
  description: "Notebook-aware pull request review workspace with inline discussion threads.",
};


export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
