"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { scopeLabel, useConsoleScope } from "@/lib/scope";

const links = [
  { href: "/ops", label: "Operations" },
  { href: "/map", label: "Map" },
  { href: "/live", label: "Live" },
  { href: "/backends", label: "Backends" },
  { href: "/workspaces", label: "Workspaces" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  const scope = useConsoleScope();
  return (
    <nav className="rail" aria-label="Console">
      <div className="brand">
        Albert
        <small>memory console</small>
      </div>
      {scope ? (
        <div className="scope" data-testid="console-scope" title="What this console's key can see">
          <span className="label">Viewing</span>
          {scopeLabel(scope)}
        </div>
      ) : null}
      {links.map((link) => {
        // A trace's replay belongs to Live, where traces are listed.
        const current =
          pathname.startsWith(link.href) || (link.href === "/live" && pathname.startsWith("/traces"));
        return (
          <Link key={link.href} href={link.href} aria-current={current ? "page" : undefined}>
            {link.label}
          </Link>
        );
      })}
      <div className="foot">Traces hold ids and scores, never memory text. Every memory you open here is audited.</div>
    </nav>
  );
}
