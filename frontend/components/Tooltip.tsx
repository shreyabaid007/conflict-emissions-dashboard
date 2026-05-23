"use client";

import { useState, useRef, useCallback, type ReactNode } from "react";

interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
}

export function Tooltip({ content, children }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState<"above" | "below">("below");
  const triggerRef = useRef<HTMLSpanElement>(null);

  const show = useCallback(() => {
    if (triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      setPosition(rect.top > 200 ? "above" : "below");
    }
    setVisible(true);
  }, []);

  return (
    <span
      ref={triggerRef}
      className="relative inline-block"
      onMouseEnter={show}
      onMouseLeave={() => setVisible(false)}
      onFocus={show}
      onBlur={() => setVisible(false)}
      tabIndex={0}
      role="button"
    >
      <span className="cursor-help border-b border-dashed border-slate-600 transition-colors hover:border-slate-400">
        {children}
      </span>
      {visible && (
        <span
          className={`absolute left-1/2 z-40 w-72 -translate-x-1/2 rounded-lg border border-white/[0.08] bg-slate-800/95 px-3.5 py-2.5 text-left text-xs font-normal leading-relaxed text-slate-400 shadow-xl backdrop-blur-sm ${
            position === "above" ? "bottom-full mb-2" : "top-full mt-2"
          }`}
        >
          {content}
        </span>
      )}
    </span>
  );
}
