import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import "./globals.css";
import "./workspace.css";
import "./experience.css";
import "./landing.css";
import "./projects.css";
import "./vault.css";
import "./tunnel.css";
import "./journey.css";

export const metadata: Metadata = {
  title: "R MATRIX AI | Personal Command Center",
  description: "A private, local-first command center for AI, personal knowledge, projects, and tasks.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
