import { SettingsPanel } from "@/components/SettingsPanel";

export default function SettingsPage() {
  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <span className="sub">Connect agents and services to this Albert with the least access they need.</span>
      </div>
      <SettingsPanel />
    </>
  );
}
