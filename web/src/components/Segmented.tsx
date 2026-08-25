import { useLayoutEffect, useRef } from "react";

type Option<T> = {
  value: T;
  label: string;
};

type Props<T> = {
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
};

export function Segmented<T extends string | number>({ options, value, onChange, label }: Props<T>) {
  const rootRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLSpanElement>(null);
  const activeIndex = options.findIndex((option) => option.value === value);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const thumb = thumbRef.current;
    if (!root || !thumb || activeIndex < 0) return;

    const measure = () => {
      const active = root.querySelectorAll("button")[activeIndex];
      if (!active) return;
      // Written straight to the node on purpose. Holding the offset in React
      // state puts the new value in the same pre-paint commit as the click, so
      // the browser never sees the old one change and the thumb jumps instead
      // of sliding.
      thumb.style.transform = `translateX(${active.offsetLeft}px)`;
      thumb.style.width = `${active.offsetWidth}px`;
    };

    measure();

    if (!thumb.dataset.ready) {
      // Commit the parked position while transitions are still off, then turn
      // them on, so the thumb doesn't slide in from the left edge on mount.
      void thumb.offsetWidth;
      thumb.dataset.ready = "true";
    }

    // Widths shift when the row reflows, and again when the CJK webfont swaps
    // in — either would leave the thumb parked over the wrong option.
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    void document.fonts?.ready.then(measure);
    return () => observer.disconnect();
  }, [activeIndex, options.length]);

  return (
    <div className="seg" role="radiogroup" aria-label={label} ref={rootRef}>
      <span className="seg-thumb" aria-hidden="true" ref={thumbRef} />
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          data-label={option.label}
          className={option.value === value ? "on" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
