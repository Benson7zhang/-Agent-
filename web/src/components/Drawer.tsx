import { X } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef } from "react";

interface DrawerProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  width?: "regular" | "wide";
  testId?: string;
}

export function Drawer({ open, title, description, onClose, children, width = "regular", testId }: DrawerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = closeRef.current?.closest("aside");
      const focusable = Array.from(drawer?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? []).filter((element) => !element.hasAttribute("hidden"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.classList.add("drawer-active");
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("drawer-active");
      returnFocusRef.current?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div className="drawer-layer" data-testid={testId}>
      <button className="drawer-scrim" type="button" onClick={onClose} aria-label={`关闭${title}`} />
      <aside className={`drawer drawer--${width}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="drawer__header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label={`关闭${title}`}>
            <X size={20} aria-hidden="true" />
          </button>
        </header>
        <div className="drawer__content">{children}</div>
      </aside>
    </div>
  );
}
