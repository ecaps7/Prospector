import { useEffect, useRef, type ComponentPropsWithoutRef, type RefObject } from "react";

type Props = Omit<ComponentPropsWithoutRef<"textarea">, "value"> & {
  value: string;
  /** Omit to let the field grow without a ceiling (the page then owns the scrolling). */
  maxHeight?: number;
  /**
   * Let the CSS min-height own an empty field, so a measurement taken before
   * the stylesheet lands can't lock in a bogus height.
   */
  resetWhenEmpty?: boolean;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
};

export function AutoGrowTextarea({ value, maxHeight, resetWhenEmpty, inputRef, ...rest }: Props) {
  const localRef = useRef<HTMLTextAreaElement>(null);
  const ref = inputRef ?? localRef;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (resetWhenEmpty && !value) {
      el.style.height = "";
      return;
    }
    el.style.height = "auto";
    el.style.height = `${maxHeight ? Math.min(el.scrollHeight, maxHeight) : el.scrollHeight}px`;
    // Fractional line heights round up in scrollHeight, leaving a 1-2px tail that an
    // unbounded field would clip off its last line. One corrective pass settles it.
    if (!maxHeight && el.scrollHeight > el.clientHeight) {
      el.style.height = `${el.scrollHeight + (el.scrollHeight - el.clientHeight)}px`;
    }
  }, [value, maxHeight, resetWhenEmpty, ref]);

  return <textarea ref={ref} value={value} {...rest} />;
}
