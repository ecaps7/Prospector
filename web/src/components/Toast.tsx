import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

export type ToastAction = {
  label: string;
  onAct: () => void;
};

type ToastContextValue = {
  /** An `action` turns the toast into an undo affordance and holds it open longer. */
  toast: (message: string, action?: ToastAction) => void;
};

const PLAIN_MS = 2600;
const ACTION_MS = 8000;

const ToastContext = createContext<ToastContextValue>({ toast: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const [action, setAction] = useState<ToastAction | null>(null);
  const [show, setShow] = useState(false);
  const timer = useRef<number | null>(null);

  const toast = useCallback((next: string, nextAction?: ToastAction) => {
    setMessage(next);
    setAction(nextAction ?? null);
    setShow(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setShow(false), nextAction ? ACTION_MS : PLAIN_MS);
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className={`toast${show ? " show" : ""}`} role="status">
        <span>{message}</span>
        {action ? (
          <button
            className="toast-act"
            type="button"
            onClick={() => {
              action.onAct();
              setShow(false);
            }}
          >
            {action.label}
          </button>
        ) : null}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}
