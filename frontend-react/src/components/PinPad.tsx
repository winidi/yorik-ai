/**
 * 4-digit numeric keypad for kiosk PIN entry.
 *
 * Big touch targets (touch-friendly on a wall-mounted tablet), no
 * autofocus on touch (the browser's onscreen keyboard would
 * compete), error shake + clear on wrong submit, configurable
 * label so the same component is reusable for "set your PIN" /
 * "switch to Dirk" / "lock screen" flows.
 *
 * Submits via onSubmit(pin) when 4 digits entered. Caller decides
 * what to do with the pin (POST /api/auth/pin-switch, etc.). The
 * pad clears itself only on error — on success the caller
 * unmounts the pad.
 */
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Loader2, Delete } from "lucide-react";

interface Props {
  /** Shown above the pad — e.g. "Enter Dirk's PIN" */
  prompt:    string;
  /** Optional second line, smaller — e.g. "4 digits" */
  subline?:  string;
  /** Called when the user has typed exactly PIN_LEN digits. Parent
   *  decides what to do (verify against server). Return value:
   *   - true / undefined: success — parent unmounts the pad
   *   - false:            wrong PIN — pad shakes + clears */
  onSubmit:  (pin: string) => Promise<boolean | void> | boolean | void;
  /** Optional cancel — when set, a "Cancel" button shows */
  onCancel?: () => void;
  /** Disable input + show spinner while parent is verifying */
  busy?:     boolean;
  /** Error text shown above the pad (e.g. "Wrong PIN, try again") */
  errorText?: string;
}

const PIN_LEN = 4;

export function PinPad({ prompt, subline, onSubmit, onCancel, busy = false, errorText }: Props) {
  const [pin, setPin] = useState("");
  const [shaking, setShaking] = useState(false);
  // Internal busy mirrors parent busy so we can show the spinner during
  // our own submission flow even if the parent doesn't pass busy back.
  const [innerBusy, setInnerBusy] = useState(false);
  const submittedRef = useRef(false);

  const isDisabled = busy || innerBusy;

  function handleDigit(d: string) {
    if (isDisabled) return;
    if (pin.length >= PIN_LEN) return;
    const next = pin + d;
    setPin(next);
    if (next.length === PIN_LEN && !submittedRef.current) {
      submittedRef.current = true;
      // Defer submit so the last dot animates in first.
      setTimeout(() => doSubmit(next), 80);
    }
  }

  function handleDelete() {
    if (isDisabled) return;
    setPin(p => p.slice(0, -1));
    submittedRef.current = false;
  }

  async function doSubmit(value: string) {
    setInnerBusy(true);
    try {
      const ok = await onSubmit(value);
      // false explicitly = bad PIN. void / true = success or
      // parent-handled. On bad PIN clear + shake.
      if (ok === false) {
        setShaking(true);
        setTimeout(() => {
          setShaking(false);
          setPin("");
          submittedRef.current = false;
        }, 380);
      }
    } finally {
      setInnerBusy(false);
    }
  }

  // Reset shake / pin when error text changes (parent reports a
  // server-side error after submit, e.g. throttled).
  useEffect(() => {
    if (errorText) {
      setShaking(true);
      setTimeout(() => {
        setShaking(false);
        setPin("");
        submittedRef.current = false;
      }, 380);
    }
  }, [errorText]);

  return (
    <div className={cn("flex flex-col items-center gap-6 select-none", shaking && "pinpad-shake")}>
      {/* Prompt + dots */}
      <div className="text-center">
        <div className="text-xl font-semibold mb-1">{prompt}</div>
        {subline && (
          <div className="text-sm text-muted-foreground">{subline}</div>
        )}
        {errorText && (
          <div className="text-sm text-rose-500 mt-2">{errorText}</div>
        )}
      </div>

      <div className="flex items-center gap-4">
        {Array.from({ length: PIN_LEN }).map((_, i) => (
          <div
            key={i}
            className={cn(
              "w-4 h-4 rounded-full border-2 transition-all",
              i < pin.length
                ? "bg-foreground border-foreground scale-110"
                : "border-foreground/30",
            )}
          />
        ))}
      </div>

      {/* Numeric pad — 3×3 + bottom row [cancel] [0] [delete] */}
      <div className="grid grid-cols-3 gap-3 w-72">
        {["1","2","3","4","5","6","7","8","9"].map(d => (
          <button
            key={d}
            type="button"
            disabled={isDisabled}
            onClick={() => handleDigit(d)}
            className={cn(
              "h-20 rounded-xl text-3xl font-light transition",
              "bg-muted/40 hover:bg-muted/70 active:bg-muted",
              "disabled:opacity-40 disabled:cursor-not-allowed",
            )}
          >
            {d}
          </button>
        ))}
        {/* Bottom row */}
        {onCancel ? (
          <button
            type="button"
            onClick={onCancel}
            disabled={isDisabled}
            className="h-20 rounded-xl text-sm font-medium bg-transparent hover:bg-muted/40 transition disabled:opacity-40"
          >
            Cancel
          </button>
        ) : <div /> /* spacer when no cancel */}
        <button
          type="button"
          disabled={isDisabled}
          onClick={() => handleDigit("0")}
          className="h-20 rounded-xl text-3xl font-light bg-muted/40 hover:bg-muted/70 active:bg-muted transition disabled:opacity-40"
        >
          0
        </button>
        <button
          type="button"
          disabled={isDisabled || pin.length === 0}
          onClick={handleDelete}
          className="h-20 rounded-xl bg-transparent hover:bg-muted/40 flex items-center justify-center transition disabled:opacity-40"
          aria-label="Delete last digit"
        >
          {innerBusy
            ? <Loader2 className="w-6 h-6 animate-spin" />
            : <Delete className="w-6 h-6" />}
        </button>
      </div>

      <style>{`
        @keyframes pinpad-shake {
          0%, 100% { transform: translateX(0); }
          20%, 60% { transform: translateX(-8px); }
          40%, 80% { transform: translateX(8px); }
        }
        .pinpad-shake { animation: pinpad-shake 380ms ease-in-out; }
      `}</style>
    </div>
  );
}
