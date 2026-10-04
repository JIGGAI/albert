import { Feed } from "@/components/Feed";

export default function LivePage() {
  return (
    <>
      <div className="page-head">
        <h1>Live</h1>
        <span className="sub">Every request and worker job, as it happens.</span>
      </div>
      <Feed />
    </>
  );
}
