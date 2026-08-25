import { useEffect, useRef, type ComponentPropsWithoutRef, type RefObject } from "react";

type Props = Omit<ComponentPropsWithoutRef<"textarea">, "value"> & {
  value: string;
  maxHeight: number;
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
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [value, maxHeight, resetWhenEmpty, ref]);

  return <textarea ref={ref} value={value} {...rest} />;
}
