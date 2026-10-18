import { useCallback, useEffect, useRef, useState } from "react";
import type { Command, Home, RequestStatus } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    signal: init?.signal ?? AbortSignal.timeout(8000),
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const message =
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail
              .map((item: { msg: string }) =>
                item.msg.replace(/^Value error, /, ""),
              )
              .join("；")
          : "服務暫時無法處理請求";
    throw new ApiError(message, response.status);
  }
  return data as T;
}
export function useHome() {
  const [home, setHome] = useState<Home | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(Date.now());
  const arrival = useRef({ server: Date.now(), client: Date.now() });
  const refreshRef = useRef<() => void>(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);
  useEffect(() => {
    let disposed = false,
      timer: ReturnType<typeof setTimeout>,
      busy = false;
    const controller = new AbortController();
    const poll = async () => {
      if (busy || disposed) return;
      clearTimeout(timer);
      busy = true;
      try {
        const next = await json<Home>("/api/home", {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(8000),
          ]),
        });
        if (!disposed) {
          arrival.current = {
            server: Date.parse(next.server_time),
            client: Date.now(),
          };
          setHome(next);
          setError("");
          setTick(Date.now());
        }
      } catch {
        if (!disposed) setError("本機服務暫時無法連線，正在重新連線。");
      } finally {
        busy = false;
        if (!disposed) timer = setTimeout(poll, 2000);
      }
    };
    refreshRef.current = () => {
      void poll();
    };
    void poll();
    const clockTimer = setInterval(() => setTick(Date.now()), 1000);
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timer);
      clearInterval(clockTimer);
    };
  }, []);
  return {
    home,
    error,
    refresh,
    realNow: arrival.current.server + (tick - arrival.current.client),
  };
}

export function useCommands(refresh: () => void) {
  const [pending, setPending] = useState<Command | null>(null);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(
    null,
  );
  const locked = useRef(false);
  const submit = useCallback(
    (kind: string, payload: Record<string, unknown>) => {
      if (locked.current) return;
      locked.current = true;
      setNotice(null);
      setPending({ request_id: crypto.randomUUID(), kind, payload });
    },
    [],
  );
  useEffect(() => {
    if (!pending) return;
    let disposed = false,
      timer: ReturnType<typeof setTimeout>,
      posted = false;
    const started = Date.now(),
      controller = new AbortController();
    const finish = (text: string, error: boolean) => {
      if (disposed) return;
      locked.current = false;
      setPending(null);
      setNotice({ text, error });
      refresh();
    };
    const attempt = async () => {
      try {
        if (!posted) {
          // A lost POST response is safe to retry with the same durable identifier.
          await json("/api/requests", {
            method: "POST",
            body: JSON.stringify(pending),
            signal: AbortSignal.any([
              controller.signal,
              AbortSignal.timeout(8000),
            ]),
          });
          posted = true;
        }
        const status = await json<RequestStatus>(
          `/api/requests/${pending.request_id}`,
          {
            signal: AbortSignal.any([
              controller.signal,
              AbortSignal.timeout(8000),
            ]),
          },
        );
        if (disposed) return;
        if (status.status !== "pending") {
          finish(status.result || "操作已處理", status.status === "error");
          return;
        }
        setNotice({
          text:
            Date.now() - started > 15_000
              ? "請求仍在等待處理，請確認控制服務已啟動。請勿重複提交。"
              : "已送出，等待控制服務處理…",
          error: false,
        });
      } catch (error) {
        if (disposed) return;
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500
        ) {
          finish(error.message, true);
          return;
        }
        setNotice({ text: "連線中斷，正在查核並重試原請求…", error: true });
      }
      if (!disposed) timer = setTimeout(attempt, 2000);
    };
    void attempt();
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [pending, refresh]);
  return { submit, pending, notice };
}
