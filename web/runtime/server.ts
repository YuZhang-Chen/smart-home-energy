import { createServer } from "node:http";
import { EventType, HttpAgent, type BaseEvent } from "@ag-ui/client";
import {
  CopilotRuntime,
  InMemoryAgentRunner,
  TranscriptionService,
  type TranscribeFileOptions,
} from "@copilotkit/runtime/v2";
import { createCopilotNodeListener } from "@copilotkit/runtime/v2/node";
import { Observable } from "rxjs";

const pythonApi = process.env.HEMS_PYTHON_API_URL ?? "http://127.0.0.1:8502";

/**
 * Copilot's runner owns only transport subscriptions. Python/SQLite remains the
 * authority for accepted messages, run status and replayable AG-UI events.
 */
class PythonBackedRunner extends InMemoryAgentRunner {
  override connect(request: Parameters<InMemoryAgentRunner["connect"]>[0]) {
    return new Observable<BaseEvent>(subscriber => {
      const controller = new AbortController();
      void (async () => {
        try {
          const response = await fetch(`${pythonApi}/api/assistant`, { signal: controller.signal });
          if (!response.ok) throw new Error("無法恢復家庭對話");
          const state = await response.json() as {
            messages: { id: string; role: "user" | "assistant" | "tool"; content: string; toolCalls?: unknown[]; toolCallId?: string }[];
            active_run_id?: string | null;
          };
          if (request.threadId !== "home") throw new Error("找不到此家庭對話");
          if (state.active_run_id) {
            const lastEventId = Number(request.headers?.["last-event-id"] ?? request.headers?.["Last-Event-ID"] ?? 0);
            const replay = await fetch(`${pythonApi}/api/assistant/runs/${encodeURIComponent(state.active_run_id)}/events?after=${Number.isSafeInteger(lastEventId) ? Math.max(0, lastEventId) : 0}`, { signal: controller.signal });
            if (!replay.ok || !replay.body) throw new Error("無法重新連接 Agent 事件串流");
            const reader = replay.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const frames = buffer.split("\n\n");
              buffer = frames.pop() ?? "";
              for (const frame of frames) {
                const data = frame.split("\n").find(line => line.startsWith("data: "))?.slice(6);
                if (data) subscriber.next(JSON.parse(data));
              }
            }
          } else if (state.messages.length) {
            subscriber.next({ type: EventType.MESSAGES_SNAPSHOT, messages: state.messages.map(message => ({ ...message })) } as BaseEvent);
          }
          subscriber.complete();
        } catch (error) {
          if (!controller.signal.aborted) subscriber.error(error);
        }
      })();
      return () => controller.abort();
    });
  }

  override async isRunning(request: Parameters<InMemoryAgentRunner["isRunning"]>[0]) {
    try {
      const response = await fetch(`${pythonApi}/api/assistant`);
      if (!response.ok) return false;
      const state = await response.json() as { active_run_id?: string | null };
      return !!state.active_run_id && request.threadId === "home";
    } catch {
      return false;
    }
  }

  override async stop(_request: Parameters<InMemoryAgentRunner["stop"]>[0]) {
    // Appliance cancellation is handled by durable approval/task requests.
    // Closing a browser stream must never cancel the resident Python worker.
    return false;
  }
}

class PythonBackedTranscriptionService extends TranscriptionService {
  constructor(private readonly baseUrl: string) {
    super();
  }

  override async transcribeFile({ audioFile, mimeType }: TranscribeFileOptions): Promise<string> {
    const contentType = mimeType || audioFile.type || "audio/webm";
    const filename = audioFile.name || "recording.webm";
    const response = await fetch(`${this.baseUrl}/api/asr/transcribe`, {
      method: "POST",
      headers: {
        "Content-Type": contentType,
        "X-Audio-Filename": encodeURIComponent(filename),
      },
      body: await audioFile.arrayBuffer(),
    });
    const payload = await response.json().catch(() => ({})) as { text?: string; detail?: string };
    if (!response.ok) {
      throw new Error(payload.detail || `ASR HTTP ${response.status}`);
    }
    return (payload.text ?? "").trim();
  }
}

const runtime = new CopilotRuntime({
  agents: {
    home: new HttpAgent({ url: `${pythonApi}/api/assistant/ag-ui` }),
  },
  runner: new PythonBackedRunner(),
  transcriptionService: new PythonBackedTranscriptionService(pythonApi),
});

const port = Number(process.env.HEMS_COPILOT_RUNTIME_PORT ?? 8503);
createServer(createCopilotNodeListener({ runtime, basePath: "/api/copilotkit", cors: true }))
  .listen(port, "127.0.0.1", () => {
    console.log(`Copilot Runtime listening on http://127.0.0.1:${port}/api/copilotkit`);
  });
