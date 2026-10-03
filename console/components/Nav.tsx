"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "Live" },
  { href: "/explorer", label: "Explorer" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="rail" aria-label="Console">
      <div className="brand">
        Albert
        <small>memory console</small>
      </div>
      {links.map((link) => {
        const current = link.href === "/" ? pathname === "/" || pathname.startsWith("/traces") : pathname.startsWith(link.href);
        return (
          <Link key={link.href} href={link.href} aria-current={current ? "page" : undefined}>
            {link.label}
          </Link>
        );
      })}
      <div className="foot">Traces hold ids and scores, never memory text.</div>
    </nav>
  );
}
