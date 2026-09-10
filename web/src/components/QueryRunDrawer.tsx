import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, FileCode2 } from "lucide-react";
import { api, errorMessage } from "../api/client";
import { useUi } from "../app/useUi";
import { formatDateTime } from "../lib/format";
import { Drawer } from "./Drawer";
import { ErrorState, LoadingState } from "./Feedback";

export function QueryRunDrawer() {
  const { queryRunId, closeQueryRun } = useUi();
  const query = useQuery({
    queryKey: ["query-run", queryRunId],
    queryFn: () => api.getQueryRun(queryRunId!),
    enabled: Boolean(queryRunId),
  });
  const download = useMutation({
    mutationFn: () => api.downloadQueryRun(queryRunId!),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `query-run-${queryRunId}.xlsx`;
      anchor.click();
      URL.revokeObjectURL(url);
    },
  });

  return (
    <Drawer open={Boolean(queryRunId)} onClose={closeQueryRun} title="查询详情" description="只读执行记录，不提供 SQL 编辑或执行入口" width="wide" testId="query-drawer">
      {query.isLoading ? <LoadingState label="正在读取查询记录" /> : null}
      {query.isError ? <ErrorState message={errorMessage(query.error)} onRetry={() => void query.refetch()} /> : null}
      {query.data ? (
        <div className="query-detail">
          <div className="query-detail__topline">
            <div><span>执行状态</span><strong>{query.data.status}</strong></div>
            <div><span>执行时间</span><strong>{formatDateTime(query.data.created_at)}</strong></div>
          </div>
          <section>
            <h3>参数化 SQL</h3>
            <pre tabIndex={0}><code>{query.data.sql}</code></pre>
          </section>
          <section>
            <h3>绑定参数</h3>
            <pre tabIndex={0}><code>{JSON.stringify(query.data.parameters, null, 2)}</code></pre>
          </section>
          <section>
            <h3>查询规格</h3>
            <pre tabIndex={0}><code>{JSON.stringify(query.data.query_spec, null, 2)}</code></pre>
          </section>
          {download.isError ? <p className="form-error" role="alert">{errorMessage(download.error)}</p> : null}
          <button className="button button--secondary" type="button" onClick={() => download.mutate()} disabled={download.isPending}>
            <Download size={17} aria-hidden="true" />
            {download.isPending ? "正在生成" : "导出 Excel"}
          </button>
        </div>
      ) : null}
      {!query.data && !query.isLoading && !query.isError ? (
        <div className="feedback-state"><FileCode2 size={24} aria-hidden="true" /><p>没有可显示的查询记录。</p></div>
      ) : null}
    </Drawer>
  );
}
