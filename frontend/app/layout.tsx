import type { Metadata } from "next";
import "./globals.css";
import "./diagram.css";
export const metadata: Metadata = { title: "GhostShift | Factory recovery", description: "Factory recovery control center." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
