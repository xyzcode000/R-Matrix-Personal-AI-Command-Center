"use client";

import { useEffect, useRef } from "react";

type OtpInputProps = Readonly<{
  value: string;
  onChange: (value: string) => void;
  length?: number;
  disabled?: boolean;
  invalid?: boolean;
  autoFocus?: boolean;
}>;

/**
 * Six compact digit boxes with automatic focus movement, paste support and
 * backspace navigation. The full code is never displayed outside these boxes.
 */
export function OtpInput({
  value,
  onChange,
  length = 6,
  disabled = false,
  invalid = false,
  autoFocus = true,
}: OtpInputProps) {
  const inputs = useRef<Array<HTMLInputElement | null>>([]);
  const characters = Array.from({ length }, (_, index) => value[index] ?? "");

  const focusBox = (index: number) => {
    const bounded = Math.max(0, Math.min(length - 1, index));
    inputs.current[bounded]?.focus();
    inputs.current[bounded]?.select();
  };

  useEffect(() => {
    if (autoFocus) inputs.current[0]?.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (!value) {
      inputs.current[0]?.focus();
      inputs.current[0]?.select();
    }
  }, [value]);

  function writeDigit(index: number, digit: string) {
    const next = [...characters];
    next[index] = digit;
    onChange(next.join("").trimEnd());
  }

  function handleChange(index: number, raw: string) {
    const digits = raw.replace(/\D/g, "");
    if (!digits) {
      writeDigit(index, "");
      return;
    }
    if (digits.length > 1) {
      const merged = [...characters];
      digits.split("").forEach((digit, offset) => {
        if (index + offset < length) merged[index + offset] = digit;
      });
      onChange(merged.join("").trimEnd());
      focusBox(index + digits.length);
      return;
    }
    writeDigit(index, digits);
    focusBox(index + 1);
  }

  function handleKeyDown(index: number, event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Backspace") {
      event.preventDefault();
      if (characters[index]) {
        writeDigit(index, "");
        return;
      }
      if (index > 0) {
        const previous = index - 1;
        const next = [...characters];
        next[previous] = "";
        onChange(next.join("").trimEnd());
        focusBox(previous);
      }
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      focusBox(index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      focusBox(index + 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusBox(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusBox(length - 1);
    }
  }

  function handlePaste(event: React.ClipboardEvent<HTMLDivElement>) {
    const pasted = event.clipboardData.getData("text").replace(/\D/g, "").slice(0, length);
    if (!pasted) return;
    event.preventDefault();
    onChange(pasted);
    focusBox(pasted.length);
  }

  return (
    <div
      className={`auth-otp${invalid ? " is-invalid" : ""}${disabled ? " is-disabled" : ""}`}
      role="group"
      aria-label="Six digit verification code"
      onPaste={handlePaste}
    >
      {characters.map((character, index) => (
        <input
          key={index}
          ref={(element) => {
            inputs.current[index] = element;
          }}
          className="auth-otp__box"
          type="text"
          inputMode="numeric"
          autoComplete={index === 0 ? "one-time-code" : "off"}
          pattern="[0-9]*"
          maxLength={1}
          value={character}
          disabled={disabled}
          aria-label={`Digit ${index + 1} of ${length}`}
          aria-invalid={invalid || undefined}
          onChange={(event) => handleChange(index, event.target.value)}
          onKeyDown={(event) => handleKeyDown(index, event)}
          onFocus={(event) => event.target.select()}
        />
      ))}
    </div>
  );
}
