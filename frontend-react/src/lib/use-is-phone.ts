/**
 * `true` below Tailwind's `md` breakpoint (767px and narrower) — the
 * same line every `md:` class in the app switches on, so a component
 * that renders a different tree per device agrees with its classes.
 */
import { useEffect, useState } from "react";

const QUERY = "(max-width: 767px)";

export function useIsPhone(): boolean {
  const [phone, setPhone] = useState(() => typeof window !== "undefined" && window.matchMedia(QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(QUERY);
    const onChange = () => setPhone(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return phone;
}
