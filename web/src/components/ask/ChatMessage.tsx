import type { ReactNode } from "react";

export function ChatMessage({ from, children }: { from: "user" | "scope"; children: ReactNode }) {
  const user = from === "user";
  return (
    <div className={user ? "msg user" : "msg"}>
      <span className="who">{user ? "我" : "P"}</span>
      <div className="bubble">{children}</div>
    </div>
  );
}
