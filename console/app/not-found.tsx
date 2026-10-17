import Link from "next/link";

export default function NotFound() {
  return (
    <section className="panel">
      <h1>Page not found</h1>
      <p className="muted">The requested Albert console page does not exist.</p>
      <Link href="/live">Return to the live console</Link>
    </section>
  );
}
