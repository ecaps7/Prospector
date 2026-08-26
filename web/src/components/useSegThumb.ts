import { useLayoutEffect, useRef } from "react";

/**
 * Drives the sliding thumb shared by the segmented control and the job tabs.
 * Returns the refs to hang on the `.seg` container and its `.seg-thumb` span.
 */
export function useSegThumb<Root extends HTMLElement = HTMLDivElement>(activeIndex: number, itemCount: number) {
  const rootRef = useRef<Root>(null);
  const thumbRef = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const thumb = thumbRef.current;
    if (!root || !thumb || activeIndex < 0) return;

    const measure = () => {
      const active = root.querySelectorAll<HTMLElement>(":scope > a, :scope > button")[activeIndex];
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
  }, [activeIndex, itemCount]);

  return { rootRef, thumbRef };
}
