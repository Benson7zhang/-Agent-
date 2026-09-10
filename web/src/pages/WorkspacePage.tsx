import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquarePlus, Send, ShieldCheck } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, createIdempotencyKey, errorMessage, type ConversationTurn } from "../api/client";
import { AnswerView } from "../components/AnswerView";
import { ErrorState } from "../components/Feedback";
import { formatDateTime } from "../lib/format";

interface RecentConversation {
  id: string;
  title: string;
  updatedAt: string;
}

const RECENTS_KEY = "financial-qa-recent-conversations";

function readRecents(): RecentConversation[] {
  try {
    const value = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]") as unknown;
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is RecentConversation => Boolean(item && typeof item === "object" && "id" in item && "title" in item && "updatedAt" in item));
  } catch {
    return [];
  }
}

function persistRecent(item: RecentConversation): RecentConversation[] {
  const next = [item, ...readRecents().filter((recent) => recent.id !== item.id)].slice(0, 12);
  localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  return next;
}

export function WorkspacePage() {
  const [conversationId, setConversationId] = useState<string | null>(() => readRecents()[0]?.id ?? null);
  const [version, setVersion] = useState(0);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [question, setQuestion] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [recents, setRecents] = useState<RecentConversation[]>(readRecents);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const activeConversationIdRef = useRef<string | null>(conversationId);
  const queryClient = useQueryClient();

  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => api.getConversation(conversationId!),
    enabled: Boolean(conversationId),
    retry: false,
  });

  useEffect(() => {
    if (!conversation.data) return;
    setVersion(conversation.data.version);
    setTurns(conversation.data.turns ?? []);
  }, [conversation.data]);

  useEffect(() => {
    activeConversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [turns]);

  const submitTurn = useMutation({
    mutationFn: async (text: string) => {
      let activeId = conversationId;
      let activeVersion = version;
      if (!activeId) {
        const created = await api.createConversation(text.slice(0, 32));
        activeId = created.conversation_id;
        activeVersion = created.version;
        activeConversationIdRef.current = activeId;
        setConversationId(activeId);
        setVersion(activeVersion);
      }
      return api.createTurn(activeId, text, activeVersion, createIdempotencyKey(`turn:${activeId}`));
    },
    onSuccess: (response, submittedQuestion) => {
      const id = response.conversation.conversation_id;
      queryClient.setQueryData(["conversation", id], response.conversation);
      setConversationId(id);
      setVersion(response.conversation.version);
      setTurns(response.conversation.turns?.length ? response.conversation.turns : (current) => [...current, response.turn]);
      setRecents(persistRecent({ id, title: response.conversation.title || submittedQuestion.slice(0, 32), updatedAt: response.conversation.updated_at }));
      setQuestion("");
      setFormError(null);
      inputRef.current?.focus();
    },
    onError: async (error) => {
      const turnError = errorMessage(error);
      const activeId = activeConversationIdRef.current;
      if (!activeId) {
        setFormError(turnError);
        return;
      }
      try {
        await queryClient.cancelQueries({ queryKey: ["conversation", activeId] });
        const latest = await api.getConversation(activeId);
        queryClient.setQueryData(["conversation", activeId], latest);
        setConversationId(latest.conversation_id);
        setVersion(latest.version);
        setTurns(latest.turns ?? []);
        setRecents(persistRecent({
          id: latest.conversation_id,
          title: latest.title || "失败的查询",
          updatedAt: latest.updated_at,
        }));
        setFormError(`${turnError}；已同步服务端会话状态，可以修改问题后继续。`);
      } catch (syncError) {
        setFormError(`${turnError}；会话状态同步失败：${errorMessage(syncError)}`);
      }
    },
  });

  const sortedTurns = useMemo(() => [...turns].sort((a, b) => a.sequence - b.sequence), [turns]);

  function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed) {
      setFormError("请输入需要查询的财务问题。");
      inputRef.current?.focus();
      return;
    }
    setFormError(null);
    submitTurn.mutate(trimmed);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    send(question);
  }

  function startNewConversation() {
    activeConversationIdRef.current = null;
    setConversationId(null);
    setVersion(0);
    setTurns([]);
    setQuestion("");
    setFormError(null);
    inputRef.current?.focus();
  }

  return (
    <div className="workspace-page">
      <aside className="conversation-rail" aria-label="最近会话">
        <button className="button button--primary new-conversation" type="button" onClick={startNewConversation}>
          <MessageSquarePlus size={17} aria-hidden="true" />新建会话
        </button>
        <h2>最近会话</h2>
        <div className="recent-list">
          {recents.length === 0 ? <p className="recent-list__empty">暂无会话记录</p> : null}
          {recents.map((recent) => (
            <button key={recent.id} className={conversationId === recent.id ? "recent-item is-active" : "recent-item"} type="button" onClick={() => setConversationId(recent.id)}>
              <span>{recent.title}</span><small>{formatDateTime(recent.updatedAt)}</small>
            </button>
          ))}
        </div>
      </aside>
      <section className="question-workspace" aria-labelledby="workspace-title">
        <header className="page-heading workspace-heading">
          <div><p className="eyebrow">FINANCIAL QUERY</p><h1 id="workspace-title">问数工作台</h1></div>
          <span className="trust-indicator"><ShieldCheck size={17} aria-hidden="true" />仅使用已验证事实</span>
        </header>
        <div className="conversation-feed" aria-live="polite">
          {conversation.isError ? <ErrorState message={errorMessage(conversation.error)} onRetry={() => void conversation.refetch()} /> : null}
          {!conversation.isError && sortedTurns.length === 0 ? (
            <div className="workspace-empty">
              <div className="ledger-lines" aria-hidden="true"><span /><span /><span /></div>
              <strong>尚未开始问数</strong>
              <p>答案将同时展示财务口径、计算公式与 PDF 原文证据。</p>
            </div>
          ) : null}
          {sortedTurns.map((turn) => (
            <article className="turn" key={turn.turn_id}>
              <div className="turn__question"><span>问</span><p>{turn.question}</p></div>
              {turn.answer ? <AnswerView answer={turn.answer} queryRunId={turn.query_run_id} /> : <p className="turn__pending">查询尚未产生答案。</p>}
            </article>
          ))}
          {submitTurn.isPending ? <div className="turn-loading" role="status"><span className="pulse-dot" />正在校验事实与来源…</div> : null}
          <div ref={messageEndRef} />
        </div>
        <form className="question-composer" onSubmit={onSubmit}>
          <label htmlFor="financial-question">财务问题</label>
          <div className="composer-row">
            <textarea id="financial-question" ref={inputRef} value={question} onChange={(event) => setQuestion(event.target.value)} rows={2} placeholder="例如：比较公司近三年的营业收入" disabled={submitTurn.isPending} />
            <button className="send-button" type="submit" disabled={submitTurn.isPending} aria-label="发送问题">
              <Send size={20} aria-hidden="true" />
            </button>
          </div>
          {formError ? <p className="form-error" role="alert">{formError}</p> : null}
        </form>
      </section>
    </div>
  );
}
