import { redirect } from "next/navigation";

// Operations is the console's home: the first thing an operator wants is whether
// memory is being used and working, not a scrolling feed.
export default function HomePage() {
  redirect("/ops");
}
