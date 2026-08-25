import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

type ToastContextValue = {
  toast: (message: string) => void;
};

const ToastContext = createContext<ToastContextValue>({ toast: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const [show, setShow] = useState(false);
  const timer = useRef<number | null>(null);

  const toast = useCallback((next: string) => {
    setMessage(next);
    setShow(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setShow(false), 2600);
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className={`toast${show ? " show" : ""}`} role="status">
        {message}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}
