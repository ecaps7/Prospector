import type { ReactNode } from "react";

export function Spinner({ inline }: { inline?: boolean }) {
  return (
    <span
      className="spinner"
      style={inline ? { display: "inline-block", verticalAlign: "middle" } : undefined}
    />
  );
}

/** Spinner + label, sized to sit inline in a flow of content. */
export function StatusLine({ children }: { children: ReactNode }) {
  return (
    <div className="scope-status">
      <Spinner />
      <span>{children}</span>
    </div>
  );
}

/** Whole-page loading state. */
export function LoadingView({ children }: { children: ReactNode }) {
  return (
    <section className="view">
      <StatusLine>{children}</StatusLine>
    </section>
  );
}

/** Whole-page failure state. `muted` is for expected absences, not errors. */
export function ErrorView({ message, tone = "error" }: { message: string; tone?: "error" | "muted" }) {
  return (
    <section className="view">
      <p className={tone === "muted" ? "muted" : "form-error"}>{message}</p>
    </section>
  );
}
