import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn's standard `cn()` — combines clsx + tailwind-merge so later
 * utility classes override earlier ones cleanly. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
