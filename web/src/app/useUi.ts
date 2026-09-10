import { useContext } from "react";
import { UiContext, type UiContextValue } from "./ui-context";

export function useUi(): UiContextValue {
  const context = useContext(UiContext);
  if (!context) throw new Error("useUi 必须在 UiProvider 内使用");
  return context;
}
