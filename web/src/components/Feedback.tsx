import { AlertTriangle, Inbox, LoaderCircle, RotateCcw } from "lucide-react";

export function LoadingState({ label = "正在读取数据" }: { label?: string }) {
  return (
    <div className="feedback-state" role="status">
      <LoaderCircle className="spin" size={24} aria-hidden="true" />
      <p>{label}</p>
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="feedback-state">
      <Inbox size={28} aria-hidden="true" />
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="feedback-state feedback-state--error" role="alert">
      <AlertTriangle size={26} aria-hidden="true" />
      <strong>数据加载失败</strong>
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="button button--secondary" onClick={onRetry}>
          <RotateCcw size={17} aria-hidden="true" />
          重试
        </button>
      ) : null}
    </div>
  );
}
