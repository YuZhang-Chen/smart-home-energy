# Changelog

記錄各版本的重要變更，最新版本放最上方。格式參考 [Sample CHANGELOG](https://gist.github.com/juampynr/4c18214a8eb554084e21d6e288a18a2c)，使用版本號、日期及 Added／Changed／Fixed 分類；版本 commit 以對應的 `v{版本號}:` 開頭。既有未編版紀錄保留原標題。

## [v1.7.0] - 2026-11-29

串接 vLLM OpenAI-compatible Whisper ASR（`model="Asr"`）至前端「家庭助理」側邊欄對話框，支援在瀏覽器直接錄製語音、自動轉為 16 kHz 單聲道 WAV 上傳辨識（保留語言自動偵測以利中英混合指令），並將轉錄結果填入輸入框供使用者確認後送出。對應 commit：`v1.7.0: integrate vLLM Whisper ASR into frontend chat dialog`。

### Added

- 新增後端 ASR 服務模組 `src/smart_home_energy/asr.py`：`AsrConfig.from_env()` 預設沿用 `VLLM_BASE_URL` 與 `VLLM_API_KEY`、預設模型為 `"Asr"`（可由 `VLLM_ASR_MODEL`／`VLLM_ASR_LANGUAGE` 覆寫），透過 `openai.AsyncOpenAI` 呼叫 `/v1/audio/transcriptions` 並內建 10 MB 上限與安全錯誤轉換（不外洩憑證）。
- 新增 FastAPI 端點 `POST /api/asr/transcribe`（直接接收原始音訊位元組與 `Content-Type`／`X-Audio-Filename` 標頭，無需額外安裝 `python-multipart`），並於 `/api/diagnostics` 回報 `asr` 設定狀態。
- 新增瀏覽器音訊轉檔工具 `web/src/audioWav.ts`：使用原生 Web Audio API（`AudioContext` + `OfflineAudioContext`）將 `MediaRecorder` 產生的 `audio/webm` 自動重取樣並編碼為 16 kHz 單聲道 16-bit PCM WAV，解決上游 LiteLLM／Whisper 不接受 `webm` 格式的問題。
- 新增後端測試 `tests/test_asr.py`（8 項涵蓋設定載入、副檔名對齊、大小／格式驗證與 HTTP 錯誤對應）與前端測試 `web/src/audioWav.test.ts`（驗證 16 kHz PCM WAV 標頭與資料長度）。

### Changed

- 更新 `web/runtime/server.ts`：新增 `PythonBackedTranscriptionService` 並註冊至 `CopilotRuntime`，自動啟用 CopilotKit v2 原生語音錄製與 `/api/copilotkit/transcribe` 流程。
- 更新 `web/src/App.tsx`：新增 `VoiceChatInput` 包裝 `CopilotChatInput` 於上傳前自動轉為 WAV，並於 `COPILOT_LABELS` 補齊繁體中文語音按鈕標籤（「語音輸入」、「取消錄音」、「完成錄音並辨識」）。
- 更新 `src/smart_home_energy/api.py`：將 `/api/copilotkit/{path:path}` Proxy 的 `write` 逾時由 10 秒放寬至 30 秒以支援較長錄音上傳。
- 更新 `.env.example`：補充 `VLLM_ASR_MODEL=Asr` 與選用 `VLLM_ASR_LANGUAGE` 設定說明。

### Validation

- 後端 pytest：`tests/test_asr.py`（8/8）與 `tests/test_api.py`（14/14）全數通過。
- 前端 Vitest（30/30）、`npm run check:runtime` 與 `npm run build` 正式建置皆通過。

---

## [v1.6.6.2] - 2026-11-29

補強智慧電視（`tv-1`）與其他設備一致的常駐 Controller／SQLite 持久化操作日誌架構，將前端 `TVControl` 手動控制接入 `/api/requests` 佇列、自然語言指令改經 Main Agent（AG-UI）委派執行，並為 `tv-1` 與 `curtain-1` 補齊與客廳燈一致的極簡聊天室狀態回執卡。與 `v1.6.6.1` 一同收錄於 commit：`v1.6.6: add 3D live YouTube TV, TVController durable logs, Main Agent TV panel, and TV/curtain status cards`。

### Added

- 新增常駐電視控制器 `TVController`（`src/smart_home_energy/tv_control.py`）並掛載至 `control.py` 主迴圈，統一消費 `/api/requests` 佇列中的 `tv_control` 請求並同步裝置狀態至 SQLite。
- `tv.py` 新增 `TVDeviceState`、`TVCommand`、`TVCommandResult` 及 SQLite 操作日誌寫入（`devices`、`commands`、`audit`），並新增 `create_tv_router(repo)` 讓相容端點同步寫入稽核紀錄。
- 補齊聊天室狀態回執卡：`agent.py` 與 `assistant_service.py` 新增 `tv_card`／`curtain_card` 持久化 AG-UI 事件（`render_tv_card`／`render_curtain_card`），前端 `App.tsx` 新增 `TVActionCard` 與 `CurtainActionCard` 純狀態回執卡（與 `LightActionCard` 風格一致，不含多餘按鈕）。
- 新增後端專屬測試 `tests/test_tv.py`（6 項涵蓋 `TVController` 佇列、422 驗證、`tv_session` 確定性與 LLM 降級、跨設備越權與 AG-UI 卡片序列化），並於 `web/src/controls.test.tsx` 補上 `TVControl` 與 `TVActionCard`／`CurtainActionCard` 測試。

### Changed

- `api.py` 新增 `TVControlPayload`／`TVControlRequest`（`kind: "tv_control"`）納入 `WebRequest` 驗證模型，`/api/home` 改呼叫 `tv.get_status(repo=repo)`，並改掛 `create_tv_router(repo)`。
- 更新 `config/device_capabilities.json` 與 `src/smart_home_energy/data/device_capabilities.json`，將 `"tv-1"` 正式納入 `Controller.devices`。
- 前端 `TVControl.tsx` 與 `DevicePanel.tsx`：手動控制按鈕改透過 `submit("tv_control", ...)` 進入持久化請求佇列；自然語言指令框改呼叫 `POST /api/assistant/ag-ui` 經 Main Agent 委派 TV Agent（MCP）執行；`tv-1` 與 `curtain-1` 面板狀態標記更新為「已串接虛擬設備」。

### Fixed

- 補齊 `tv_session` 在 LLM 不可用降級（`deterministic-tv-fallback`）分支的 `tool_logs` 紀錄。
- 清除 `multi_agent.py` 中不再使用的舊死碼函式 `run_tv_agent`。
- 移除 `TVControl.tsx` 的錯誤靜默忽略（`.catch(() => {})`），改為明確顯示錯誤提示。

### Validation

- 後端 pytest：`tests/test_tv.py`（6/6）、`tests/test_assistant_service.py`（5/5）及既有核心測試（44 passed, 1 live skipped）皆通過。
- 前端 Vitest 29 項單元與互動測試與 `npm run build` 正式建置皆通過。

---

## [v1.6.6.1] - 2026-11-28

升級 3D 智慧客廳電視（`tv-1`）螢幕渲染為真實可播放的嵌入式 YouTube 頻道畫面（含即時新聞 24H 直播與 OSD 介面），並修復電視控制面板靜音按鈕未同步後端的問題。對應 commit：`v1.6.6.1: embed live YouTube channels on 3D TV screen and fix TV mute sync`。

### Added

- 新增 `web/src/TVScreen.tsx`：以 `@react-three/drei` 的 `<Html transform>` 搭配正交攝影機比例（`615 × 330px`、`distanceFactor={4 / 3}`）將 YouTube 嵌入式播放器精準貼合至 3D 電視框。
- 內建 4 組可嵌入 YouTube 頻道供 `HDMI1`–`HDMI4` 切換：`HDMI1`（公視 13 台 Live）、`HDMI2`（即時新聞 24H Live）、`HDMI3`（4K 自然風景）、`HDMI4`（Lo-Fi 音樂頻道）。
- 透過 YouTube IFrame `postMessage` API（`enablejsapi=1` + `origin`）即時同步 `playback`（播放／暫停）、`volume`（0–100）與 `muted` 狀態，並於螢幕疊加訊號源、暫停遮罩與底部音量條 OSD 浮層；攝影機旋轉至電視背面時自動隱藏浮層防穿幫。

### Changed

- 更新 `web/src/Scene.tsx`：將原本純色方塊電視畫面替換為 `<TVScreen tvState={tvState} />`，保留關機待機紅點指示燈與開機時的客廳環境柔光（`pointLight`）。
- 更新 `web/src/style.css`：新增 `.tv-3d-*` 螢幕與 OSD 樣式，並為 `.scene-canvas` 加上 `isolation: isolate; overflow: hidden;` 確保 3D HTML 浮層不遮蓋畫布上方 UI 或左右側邊對話框。

### Fixed

- 修正 `web/src/TVControl.tsx` 靜音按鈕僅修改本地 React 狀態而未呼叫 `syncToBackend({ muted })` 的問題，使靜音切換能即時同步至後端與 3D YouTube 電視播放器。

### Validation

- 前端 Vitest 27 項單元與互動測試全數通過。
- 前端 `npm run build`（`check:runtime && tsc -b && vite build`）型別檢查與生產建置通過。

---

## [v1.6.5] - 2026-11-28

為 Three.js 智慧客廳新增 Low-Poly 溫馨庭園微縮底座場景與左側落地窗透視效果，整合日夜自動點亮庭園燈與輕微環境動態，並重構左右側邊對話框為 75% / 25% 平滑並排佈局以徹底解決渲染畫布被擠壓與鏡頭跳動問題。對應 commit：`v1.6.5: add 3D outdoor garden diorama, transparent window view, and 75/25 anti-squeeze sidebar layout`。

### Added

- 新增室外庭園場景元件 `web/src/OutdoorScene.tsx`：於客廳四周建立雙層圓角微縮底座（大地土壤基座與莫蘭迪草皮層）、客廳前緣石階與石板踏石小徑、6 棵松樹／圓冠 Low-Poly 樹群、多組灌木花叢、左側與後側低矮木圍籬及前庭戶外休閒長椅。
- 新增 4 盞日夜聯動庭園柱燈（分布於左窗外、前庭、右庭與後庭）及輕微環境動態（樹冠微風搖曳與高空 Low-Poly 飄浮雲朵，皆遵循 `prefers-reduced-motion`）。
- 擴充 `web/src/dayNight.ts` 新增 `outdoorLampFactor(minute)` 純函式與 `DayNightLighting.outdoorLampIntensity` 欄位（白天熄滅、黃昏平滑點亮、夜晚維持、清晨漸暗），並於 `web/src/dayNight.test.ts` 補齊單元測試。

### Changed

- 更新 `web/src/Scene.tsx`：將左側牆面依落地窗洞（Z `-2.2`–`1.5`、Y `0.2`–`2.85`）分段開孔並加上外框收邊，將 `curtain-1` 落地窗改為半透明玻璃材質，拉開窗簾時可直接從客廳透視左側庭園景致。
- 擴大 `DayNightLighting` 太陽陰影覆蓋範圍至 `±15`（`shadow-mapSize: [2048, 2048]`、`far: 42`），並下移底部透明陰影平面至微縮底座下方（`y = -0.87`），讓整座庭園與樹木皆具清晰日夜投影。
- 調整 `web/src/App.tsx` 與 `web/src/style.css`：於 `.app-shell` 加入 `data-panel-open` 狀態標記，將 `CopilotSidebar` 寬度改為 `25vw`，於桌機版（`≥ 861px`）實作主畫面 `75vw` 與左／右側邊面板 `25vw` 的平滑並排切換（關閉後恢復 `100%` 滿版），且開啟左側設備控制面板時仍可直接操作右側 3D 畫面。

### Fixed

- 鎖定 `body` 水平外距（`margin-inline: 0 !important`）以防止 `CopilotSidebar` 動態修改 `document.body.style` 造成主畫面重複推移與畫布擠壓，並移除 `.workspace` 殘留的舊版雙欄空列。
- 重構 `Scene.tsx` 的 `CameraRig`，將「重設視角」與「畫布尺寸變更」拆離，使畫布於 `100%` ↔ `75%` 切換時保留使用者當前旋轉角度與目標點、僅等比微調縮放，消除切換對話框時的 3D 視角跳動。

### Validation

- 前端 Vitest 27 項單元與互動測試全數通過（`dayNight.test.ts` 11/11、`controls.test.tsx` 16/16）。
- 前端 `npm run build`（`check:runtime && tsc -b && vite build`）型別檢查與生產建置通過。

---

## [v1.6.4] - 2026-11-28

補強智慧窗簾（`curtain-1`）與其他設備一致的 Main Agent／Controller 整合架構：納入常駐 `CurtainController` 與持久化操作日誌、將每日排程改用 `repo.clock()` 邏輯時鐘以完整支援 60 倍展示模式、補齊 `Curtain Agent` 刪除與啟用／停用排程的 MCP 工具，並修復多設備同時協作時的跨設備路由與開關語意誤判。對應 commit：`v1.6.4: integrate CurtainController, durable operation logs, 60x logical-clock schedules, and multi-device routing fixes`。

### Added

- 新增常駐窗簾控制器 `CurtainController`（`src/smart_home_energy/curtain_control.py`）並接入 `control.py` 主迴圈，統一處理 6 種持久化請求（`curtain_position`、`curtain_stop`、`curtain_scene`、`curtain_schedule_create`、`curtain_schedule_toggle`、`curtain_schedule_delete`），並在 SQLite 的 `devices`、`commands`、`audit` 與 `requests` 表寫入完整操作日誌。
- 新增 `evaluate_due_schedules(repo, real)` 邏輯時鐘區間掃描排程引擎（`curtain.py`），依 `repo.clock().now()` 與 `(last_logical_at, logical_now]` 區間觸發每日窗簾排程，完整支援 1×–100×（含 60× 展示模式）不漏觸發、同邏輯日不重複觸發，並於切換時鐘倍率時自動重置游標與寫入 `curtain_scheduler_clock_reset` 稽核紀錄。
- 擴充 `CurtainTools`（`curtain.py`）與 `SmartHomeMCPServer`（`mcp_server.py`）新增 `delete_curtain_schedule` 與 `set_curtain_schedule_enabled` 兩項 MCP 工具，並同步補強無模型確定性指令解析（支援新增、啟用、停用與刪除排程）。
- 新增 `tests/test_curtain.py`（5 項窗簾操作日誌、60× 邏輯時鐘排程與排程管理測試）與 `tests/test_multi_device_collab.py`（5 項 2–5 台設備同時協作、跨子句反義詞、多百分比數值隔離與部分失敗容錯測試），並於 `web/src/controls.test.tsx` 補上 `CurtainControl` 佇列與 Main Agent 測試。

### Changed

- 前端 `CurtainControl.tsx` 與 `DevicePanel.tsx`：手動位置、場景、停止與排程管理改透過 `POST /api/requests` 進入持久化佇列；面板內自然語言指令框改呼叫 `POST /api/assistant/ag-ui` 經由 Main Agent 委派給 Curtain Agent（MCP）執行，即時顯示處理進度並同步記錄於全屋對話與系統診斷日誌。
- `api.py` 擴充 `WebRequest` 驗證模型、於 `/api/home` 的 `curtain-1` 附帶 `schedules` 清單，改以 `create_curtain_router(repo)` 綁定資料庫記錄相容端點操作日誌，並移除 API process 內重複啟動的系統時鐘排程執行緒。
- 更新 `config/device_capabilities.json`、`src/smart_home_energy/data/device_capabilities.json`（`Controller.devices` 納入 `curtain-1`、`Curtain Agent` 補齊 `manage_curtain_schedule`）與 `multi_agent.py`（更新 Main Agent 提示詞並移除未經 MCP 的舊 `run_curtain_agent`）。

### Fixed

- 修正 `light_agent.py` 的 `_OTHER_DEVICE_RE` 漏列窗簾與電視關鍵字，避免包含窗簾或電視的複合指令（如「開一點窗簾並把客廳燈調到 60%」）被純燈泡快速路徑攔截而跳過 Main Agent。
- 修正 `light_agent.py` 開關燈權限正則跨子句比對（`關掉.*燈`），改為限制於同一子句內比對，解決「把電視關掉，再把客廳燈打開」誤判為關燈而拒絕執行的問題。
- 補強 `multi_agent.py` 的 `_AC_OTHER_DEVICE_RE`（納入 `音量`、`靜音`、`hdmi`、`blinds` 等關鍵字），避免混合多設備請求誤入冷氣快速路徑。
- 補齊 `curtain_session` 在 LLM 不可用 fallback 分支的 `tool_logs` 紀錄。

### Validation

- Python `test_curtain.py`（5/5）、`test_multi_device_collab.py`（5/5）、`test_api.py`（14/14）、`test_light_agent.py`（5/5，1 live test skipped）、`test_lighting_runner.py`（10/10）、`test_control.py`（8/8）、`test_agent.py`（7/7）均通過。
- 前端 Vitest 26 項互動測試與 Vite 正式建置通過。

---

## [v1.6.3] - 2026-11-26

將既有的日夜自動照明純規則（`lighting_rules.py`）接入 Controller 背景迴圈、REST API 與 React 中控台（不含人體感應），並讓 `Light Agent` 可讀取及說明規則狀態。

### Added

- 新增 `LightingRuleRunner`（`src/smart_home_energy/lighting_runner.py`）並接入 `control.py` 背景迴圈（排在 `BulbController` 之前），支援白天關燈、晚間暖光設定、手動操作自動覆寫至下一個時段邊界、恢復自動、去重與最多 3 次失敗重試。
- FastAPI 新增 `GET /api/lighting`、`lighting_rule`／`lighting_resume` 持久化請求驗證（拒絕版本不符、相同時段邊界與 `presence_enabled=true`），並於 `/api/home` 的 `bulb-1` 附帶 `automation` 摘要。
- 前端「偏好設定」（`SystemViews.tsx`）新增「自動照明」設定與狀態區塊；燈泡抽屜（`DevicePanel.tsx`）顯示自動照明狀態徽章與「恢復自動」按鈕。
- 新增 `tests/test_lighting_runner.py`（10 項整合情境），並補齊 `test_api.py`、`test_light_agent.py` 與 `web/src/controls.test.tsx` 測試。

### Changed

- `LightTools.get_light_context()`（含 MCP `device://bulb-1/context`）附帶 `automation` 狀態，`Light Agent` 可向使用者說明自動照明與手動暫停原因，維持不得建立或修改規則的權限邊界。
- 更新 `docs/PENDING_WORK.md`、`docs/OFFLINE_LIGHTING.md` 與 `README.md`，更正燈泡指令取代仲裁已完成並反映最新 P1 狀態。

---

## [1.6.2] - 2026-11-25

將 `Main Agent` 與 5 個設備子代理（`Washer Agent`、`AC Agent`、`Light Agent`、`TV Agent`、`Curtain Agent`）的工具與資源存取層正式升級為官方標準 MCP（Model Context Protocol）架構，支援基於角色的權限過濾（Role-Scoped Tools & Resources）與系統診斷頁 MCP 協定目錄檢視。對應 commit：`v1.6.2: integrate official MCP protocol layer with role-scoped tools, resources, and diagnostics`。

### Added

- 導入官方 Python `mcp` SDK（`mcp>=1.2,<2`），新增 `SmartHomeMCPServer`（`mcp_server.py`），以標準 `mcp.server.Server` 與 `mcp.types` 定義全系統 MCP Resources（`home://state`、`home://roles`、`home://tariffs/taipower` 及 5 台設備的 `device://<id>/context`）與 Role-Scoped MCP Tools。
- 在 FastAPI `8502` 服務公開標準 MCP JSON-RPC 2.0 端點（`POST /api/mcp`，支援 `initialize`、`ping`、`resources/list`、`resources/read`、`tools/list`、`tools/call`，可透過 `X-Agent-Role` Header 或 `params.role` 指定角色）與協定目錄端點（`GET /api/mcp/manifest`），同時支援 `python -m smart_home_energy.mcp_server` stdio 模式。
- 前端「系統診斷」頁（`SystemViews.tsx`）新增 `mcp_server: "online (MCP SDK)"` 服務狀態卡與「MCP 協定目錄（Resources & Role-Scoped Tools）」摺疊面板，直觀展示各角色授權工具與資源 URI。
- 新增標準 MCP JSON-RPC 端點、資源讀取、角色專屬工具清單過濾、跨角色／跨設備越權呼叫攔截，以及前端診斷頁 MCP 目錄渲染測試（`test_api.py`、`controls.test.tsx`）。

### Changed

- 擴充 `config/device_capabilities.json`、`src/smart_home_energy/data/device_capabilities.json` 與 `agent_roles.py`，將 `TV Agent`（`tv-1`）與 `Curtain Agent`（`curtain-1`）正式納入權責登錄表，並將 `Main Agent` 可委派設備擴充至 5 台設備，作為 MCP 協定層的唯一權限真理來源（Single Source of Truth）。
- 將 `multi_agent.py`、`light_agent.py`、`tv.py` 與 `curtain.py` 的工具呼叫與上下文讀取統一接上 `SmartHomeMCPServer`，於 `tool_logs` 統一標記 `"protocol": "MCP"` 與 `"mcp_method": "tools/call"`。
- `pyproject.toml` 與 `scripts/start.ps1` 啟動檢查納入 `mcp` 依賴；於 `README.md` 與 `docs/ASSISTANT_UI.md` 補足 `.env` 環境變數配置與快速啟動說明。

### Validation

- Python `test_api.py`、`test_light_agent.py`、`test_agent.py` 與 `test_assistant_service.py` 共 30 項測試通過（1 項 opt-in 真實模型測試略過）。
- 前端 Vitest 24 項互動測試與 Vite 正式建置通過。


## [1.6.1] - 2026-11-24

將 `TV Agent` 與 `Curtain Agent` 升級為由 `Main Agent` 階層委派、真正串接 AI Model（vLLM／Azure OpenAI）的 Tool-Calling 情境感知子代理，並補齊 3D 客廳電視模型與設備清單的即時視覺回饋。對應 commit：`v1.6.1: upgrade TV and Curtain agents to hierarchical LLM tool-calling and add 3D TV visual feedback`。

### Added

- 實作 `TVTools`／`tv_session`（`tv.py`）與 `CurtainTools`／`curtain_session`（`curtain.py`）LLM Tool-Calling 子代理：與既有代理共用 `SharedBudget`，支援自然情境與相對狀態推理（如陽光刺眼遮光、微開透光、相對音量調整、切換 HDMI 與自動開機），並具備嚴格的單一設備權限邊界與無模型確定性備援。
- 3D 客廳電視模型（`Scene.tsx`）新增動態自發光螢幕、隨 `HDMI1`–`HDMI4` 切換的主題色與環境點光源、動態音量進度條及關機待機紅色指示燈；下方客廳設備清單（`App.tsx`）即時顯示電視運作／待機、HDMI 訊號源、音量與播放狀態。
- 新增階層式 Multi-Agent 情境委派（`Main Agent` ➔ `Curtain Agent` + `TV Agent`）與跨設備越權攔截自動化測試（`test_api.py`）。

### Changed

- `Main Agent` 的 `get_home_state` 擴充包含 `tv-1` 與 `curtain-1` 即時狀態，並在設定有 AI Model 時統一透過 `delegate_tasks` 階層委派給 `tv_session` 與 `curtain_session`，在 `agent_runs` 診斷紀錄中建立完整的父子工作關聯。
- 左側電視抽屜面板（`TVControl.tsx`）操作後立即觸發 `onRefresh`，加速 `/api/home` 與 3D 場景同步。

### Validation

- Python `test_api.py`、`test_light_agent.py` 與 `test_agent.py` 共 23 項測試通過。
- 前端 Vitest 23 項互動測試與 Vite 正式建置通過。


## [1.6.0] - 2026-11-24

將虛擬窗簾與智慧電視統一掛載至 FastAPI 8502 單一服務入口、併入 `/api/home` 家庭狀態樹，並擴充 Multi-Agent 家庭助理對話視窗支援電視與窗簾控制。對應 commit：`v1.6.0: unify TV and curtain into 8502 API, /api/home snapshot, and household assistant`。

### Added

- 新增虛擬電視同源 API（`tv.py`，`/api/tv/*`）與窗簾同源路由掛載（`curtain.py`，`/api/curtain/*`），支援跨程序狀態持久化同步（`curtain_state.json`、`tv_state.json`）。
- 擴充 `multi_agent.py` 支援 `TV Agent`（`tv-1`）與 `Curtain Agent`（`curtain-1`）單一指令快速直通與跨設備協同委派（如「開窗簾並且關燈」），並將執行紀錄寫入診斷日誌。
- 3D 客廳新增窗簾動態開合模型（`CurtainPanels.tsx`）以及電視／窗簾操作抽屜面板（`TVControl.tsx`、`CurtainControl.tsx`），同時保留面板內的本機指令輸入框。

### Changed

- `/api/home` 統一回傳 `curtain-1` 與 `tv-1` 設備狀態快照；移除 `App.tsx`、`CurtainPanels.tsx` 與 `TVControl.tsx` 中各自獨立的高頻常駐輪詢（0.2s–1.5s），改由 `useHome` 狀態樹統一驅動 3D 場景與控制面板。
- 窗簾排程執行緒隨 `8502` 主服務自動啟動，不再需要額外啟動 `8001` 埠的獨立窗簾 Server。

### Validation

- Python `test_api.py` 與 `test_light_agent.py` 通過（含同源 `/api/curtain`、`/api/tv` 及「開窗簾並且關燈」跨程序 Multi-Agent 同步測試）。
- 前端 Vitest 23 項互動測試與 Vite 正式建置通過。


## [1.5.0] - 2026-11-04

完成 Light Agent 明確照明指令到虛擬燈泡的控制與回報串接。對應 commit：`v1.5.0: connect Light Agent feedback and prioritize newer commands`。

### Added

- 家庭助理照明動作卡，顯示指令目標、處理進度、設備確認、拒絕、未知及被取代狀態；重新整理後由持久化對話還原。
- 燈泡請求狀態 API 回傳動作紀錄與使用者指定目標，供卡片追蹤設備回報。
- 新增 Agent 到虛擬燈泡閉環、對話卡還原、指令競爭，以及 MQTT／Matter 漸變取代測試；真實模型驗收腳本增加燈泡情境。

### Changed

- 燈泡控制依原始家庭或手動請求順序仲裁，避免較晚返回的舊 Agent 動作覆蓋新指令；取得已派送命令的設備版本後，再派送新目標。
- 燈泡動作確認只比對該次指定的亮度與色溫，避免在漸變中取代關燈時，因未指定亮度的中間變化而誤判結果未知。

### Validation

- Python 完整回歸 122 passed、4 skipped；前端 23 項測試及正式建置通過。真實 Light Agent 模型測試另行啟用並通過。
- 隔離環境中，真實模型經 Copilot Runtime、Matter、MQTT 控制虛擬燈泡，40%／2700K 收到設備確認；網頁另驗證關燈及重新整理後還原卡片。
- MQTT 與 Matter 的隔離燈泡測試通過開關、調光、色溫和漸變中由新指令取代的情境。詳細結果見 [Light Agent 整合驗證](docs/LIGHT_AGENT_INTEGRATION_2026-11-04.md)。

### Known limitations

- 對話服務一次只接受一筆處理中的聊天工作；前一筆仍在分析時，第二筆聊天需求須稍候。手動設備請求可與 Agent 工作競爭並依原始順序仲裁。
- 日夜／presence 規則尚未串接設備，實體燈泡、聊天 SSE 游標及對話交易／重啟恢復不在本次驗收範圍。


## [1.4.0] - 2026-11-04

將家庭助理整合至 React／CopilotKit，保留 Python／SQLite 的任務與設備執行路徑，並新增可獨立驗證的照明純規則及離線研究報告。對應 commit：`v1.4.0: migrate assistant to CopilotKit and add offline lighting research`。

### Added

- CopilotKit v2 Sidebar、Node Copilot Runtime 與 Python AG-UI 串流；FastAPI 提供同源代理、對話歷史與任務查詢。SQLite 遷移至 v4，保存對話、Agent 工作與可重播事件，並匯入舊對話紀錄。
- React 用電紀錄、偏好／費率／展示時鐘及系統診斷頁，沿用持久化請求與背景 Controller。
- P1 純照明規則、獨立 1／60 倍環境時鐘、模擬 presence、手動覆寫及可序列化狀態；規則不直接操作設備。
- P2 離線情境執行器 `python -m smart_home_energy.research`，預設 17 個情境各 10 次，輸出逐次 JSON、統計 CSV 與 Markdown；包含樣本數、成功率、nearest-rank P50／P95，以及版本、工作區差異與執行環境資訊。
- 對話服務、計畫卡與純規則／報告測試，opt-in 真實模型 CopilotKit→Matter 虛擬設備驗收腳本，以及專題總覽、驗收與待處理文件。
- Python `uv.lock`，固定 AG-UI／CopilotKit 相依版本及前端鎖定檔。

### Changed

- 預設入口統一為 React／FastAPI 8502 與 Copilot Runtime 8503；啟動腳本改用 `-ApiPort`／`-RuntimePort`，保留 `-With3D` 相容參數。
- Main／設備 Agent 可回報進度；對話、提案與家庭管理介面共用原有背景控制、核准及設備回報流程。
- 助理計畫卡補上完整年月日、時間、溫度／洗衣限制、費率、費用上限與版本資訊；提供結構化修改及自然語言補充，載入最新版本後才能操作更新的計畫。

### Removed

- 舊 Streamlit 首頁、助理、監測與家電 UI 模組及其專屬測試；移除 Streamlit 相依，介面驗證轉至 React 與 API 測試。

### Fixed

- 家庭工作部分成功時保留成功設備的計畫卡，不再一律當成整體失敗。
- 相同對話識別碼重送時統一前後空白處理，避免內容正規化差異造成衝突。
- 提供明確費率來源更新請求；下載或資料格式異常時保留既有來源。

### Validation

- 2026-11-04 提交前檢查：Python 回歸 118 passed、3 skipped；React 22 tests passed；TypeScript、Runtime 型別檢查與 Vite 正式建置通過。略過項目為 opt-in 真實模型測試。
- 2026-11-02：17 個離線規則情境各執行 10 次，共 170 次通過；CLI 測試封鎖 socket 建立，核對 JSON／CSV／Markdown 樣本及未量測指標。
- 2026-11-02：隔離 Broker 的 MQTT、HTTP／MQTT 與 Matter 復原驗收通過，涵蓋核准、設備回報、斷線／重啟、去重、中止與故障解除；Matter 失聯不繞道 MQTT。未連接實體家電。
- CopilotKit 真實 vLLM／Matter 虛擬雙設備流程沿用 2026-10-22 驗收證據；本次提交未重跑真實模型及 MQTT／Matter。

### Known limitations

- 照明純規則與離線研究框架已完成；Controller／REST／設定頁整合、燈泡手動／自動指令仲裁及新目標取代仍待實作。
- SSE 逐訊框游標與對話完成交易／重啟恢復依使用者指示延後；部分卡片訊框仍共用游標，完成寫入中斷仍可能留下未結束對話。
- 新版瀏覽器完整操作、375px、雙頁與啟停驗收尚未全部完成；前端建置仍有大型 chunk 提示。P0–P2 整體未宣告通過。
- 離線耗時僅代表純規則情境；不代表模型回覆、設備確認或聊天恢復延遲。未量測指標記為空值，完整驗收狀態維持 incomplete。

操作與證據見 [家庭助理](docs/ASSISTANT_UI.md)、[離線照明規則](docs/OFFLINE_LIGHTING.md)、[驗收紀錄](docs/ACCEPTANCE_2026-11-02.md)及[待處理工作](docs/PENDING_WORK.md)。

## [1.3.0] - 2026-10-22

新增智慧燈泡的手動／自然語言控制、MQTT 狀態同步與 Matter 橋接，並明確限制各 Agent 的設備權責。對應 commit：`v1.3.0: add smart bulb control with Matter and scoped Light Agent`。

### Added

- 持久化虛擬燈泡 `bulb-1`，支援開關、亮度、2700–6500K 色溫與漸變，保存狀態、命令去重及動作生命週期；控制端資料庫遷移至 v3。
- Matter endpoint 4，透過 On/Off、Level Control、Color Control 的色溫功能控制燈泡，並與 MQTT 模擬設備狀態同步。
- Light Agent 與設備能力登錄表：Main Agent 負責查詢與委派，Light Agent 僅能提交明確授權的燈泡動作，Controller 負責驗證、派送及確認。
- 燈泡 API、3D 客廳開關／亮度／色溫面板，以及領域、API、Agent、前端與 MQTT／Matter 復原測試；新增智慧燈泡實作與測試文件。

### Changed

- 3D 燈光改由設備確認回報驅動，重新整理後可還原狀態；處理中與回報過期時顯示對應提示，助理區分已入列與已確認完成。
- 啟動腳本納入燈泡服務；Matter 模式保留洗衣機與冷氣原有核准流程。

### Fixed

- 修正 Main Agent 委派「關燈」時誤帶排程欄位而顯示「燈泡即時動作不能引用排程任務」：燈泡委派忽略 `task_id`／`expected_version`，不建立排程任務。
- 未取得燈泡最終回報的動作不再標示成功；較晚抵達的拒絕訊息不會覆蓋已確認結果。
- 修正 Matter 色溫群集初始化、亮度命令必要參數及重複讀取結果時的解碼問題。

### Validation

- 首版 Python 回歸共 93 項，2 項真實模型測試略過，其餘通過；前端 21 項測試及正式建置通過。Python 編譯、Node 語法與差異格式檢查通過。
- 隔離 Docker Mosquitto 的 MQTT 與真實 Matter 配對整合驗收通過，涵蓋開關、40% 亮度、暖／冷色溫、Broker／控制器／橋接器復原及 Matter 離線不繞道 MQTT。
- 關燈修正另以 3 項 Light Agent 測試驗證，包含模型誤帶排程欄位仍正常排入關燈動作；一般 Agent 測試使用假模型。
- 本次版本紀錄沿用上述已完成的測試結果；實測報告位於本機 `artifacts/phase2-mqtt-smoke.json` 與 `artifacts/phase2-matter-smoke.json`，測試容器已清理。

### Known limitations

- 日夜時間僅影響場景自然光，自動照明規則留待下一階段。
- Color Control 首版提供色溫，不含 RGB／Hue／Saturation；設備為模擬燈泡，未宣稱 Matter 認證或第三方控制器互通。
- 真實 vLLM／Azure 模型需另行設定及驗證；模型不可用時仍可使用手動燈泡面板。

操作範圍與測試細節見 [智慧燈泡實作計畫](docs/SMART_BULB_IMPLEMENTATION_PLAN.md)及[測試計畫](docs/SMART_BULB_TEST_PLAN.md)。

## [1.2.0] - 2026-10-20

新增客廳晝夜循環與時間控制，並統一 Streamlit 畫面刷新間隔。對應 commit：`v1.2.0: add day-night cycle and five-second Streamlit refresh`。

### Added

- 3D 客廳新增「客廳時光」控制卡，提供瀏覽器本機真實時間、逐分鐘手動時間，以及 120 秒循環展示 24 小時的加速模擬；支援暫停、續播、暫停時拖曳選時與從午夜重播。
- 獨立的場景時鐘與照明計算，依展示時間改變自然光、天空漸層、窗外色調與太陽位置；家具及地板投影隨日照移動，夜間保留最低環境照明。
- 時間與照明計算測試，涵蓋模式切換、跨午夜、完整循環、暫停續播、重播，以及時間操作不送出設備控制請求。

### Changed

- 晝夜自然光與智慧燈泡分開管理，切換時刻不覆蓋燈泡開關；設備排程持續使用後端邏輯時鐘，頁尾分別標示設備排程的 Asia/Taipei 與晝夜的本機／展示時間。
- 將 Streamlit 全部 6 個定時區塊改為每 5 秒刷新，包含家庭助理、監測、洗衣、冷氣、用電紀錄及設定；原間隔為 2 秒。重新整理頁面即可載入。
- 更新 README 與客廳操作文件，說明刷新間隔、晝夜模式及獨立燈泡控制；MQTT 設備回報與指令處理頻率維持原設定。

### Validation

- 前端正式建置成功，19 項測試通過；瀏覽器確認中午與午夜的場景變化、夜間燈泡開關的照明差異，以及 375px 手機版時間控制卡。
- Streamlit 相關 15 項測試通過，涵蓋監測、家庭助理、洗衣及首頁操作。
- 本次補寫更新紀錄沿用上述已完成的驗證結果，未重新執行功能測試；提交前檢查暫存檔案與 `git diff --cached --check`。

### Known limitations

- 晝夜採固定 06:00 日出、18:00 日落的風格化模型，不計算所在地緯度、季節或天氣。
- 展示時間與燈泡開關僅保存在本頁，重新載入後恢復真實時間與燈泡開啟；不包含 Agent 自動開關燈泡功能。

操作說明見 [3D 智慧客廳](docs/LIVING_ROOM_3D.md)；啟動與刷新說明見 [README](README.md)。

## [1.1.1] - 2026-10-18

將智慧插座替換為可選取的電風扇，維持六項場景設備。對應 commit：`v1.1.1: replace smart plug with selectable electric fan`。

### Changed

- 設備識別碼由 `plug-1` 改為 `fan-1`，同步更新電風扇按鈕、圖示、英文名稱與右側位置資訊。
- 既有右側電風扇模型加入點擊、懸停名稱與選取標示，和設備清單共用選取狀態；移除原智慧插座模型，電風扇位置由選取群組統一管理。
- 更新設備資訊展示測試與操作說明；電風扇維持靜態資訊展示，冷氣、洗衣機與燈泡行為不變。

### Validation

- 前端正式建置與 8 項互動測試通過，確認清單維持六項設備、電風扇資訊正確且不再顯示智慧插座按鈕。
- 瀏覽器確認直接點擊電風扇模型、按鈕與資訊面板同步、浮動名稱，以及拖曳旋轉不誤選；本次版本紀錄沿用上述驗證，未重跑功能測試。

## [1.1.0] - 2026-10-18

新增本機 3D 智慧客廳，透過場景選取設備、安排冷氣與洗衣計畫，核准後沿用既有服務執行。對應 commit：`v1.1.0: add interactive 3D living room with appliance controls`。

### Added

- React 19／React Three Fiber 9 Low-poly 客廳，包含牆面、木地板、沙發、茶几、地毯與側邊家事區；前端依賴及鎖定檔獨立放在 `web/`。
- 六項設備支援場景點擊、浮動名稱與 HTML 設備清單；正交相機提供初始 45° 俯視、旋轉、縮放、重設視角及遮擋牆面隱藏。
- 冷氣與洗衣機提供條件表單、計畫估算、核准、修改、取消、確認中止、狀態查核與人工解除異常；面板及運轉動畫使用既有設備回報。
- 智慧燈泡開關同步改變發光材質及場景光源，保留環境光；提供響應式版面、鍵盤設備操作、減少動態效果與 WebGL 不可用時的提示。
- 新增隔離資料庫的 API 測試、前端互動測試及真實 HTTP／MQTT 整合驗收腳本。

### Changed

- 新增 FastAPI 本機 API 與前端靜態檔案入口，預設為 `http://127.0.0.1:8502`；操作進入原有持久化請求佇列，沿用版本檢查、請求去重、核准與 MQTT／Matter 控制流程。
- Repository 新增逐設備最近任務與單筆請求查詢，不新增資料庫遷移；狀態每兩秒更新，超過十秒未回報時保留最後數值、標記過期並停止運轉動畫。
- 啟動腳本加入 `-With3D`、`-LivingRoomPort` 與前端建置檢查，API 納入現有啟停紀錄；保留 Streamlit，家庭助理連結跟隨設定的連接埠。
- 補充安裝、建置、開發與驗收文件，忽略 TypeScript 建置快取；套件版本維持原設定。

### Validation

- 2026-10-18：前端正式建置成功，8 項互動測試通過；Python 共 81 項測試，79 項通過、2 項需另行啟用的真實模型測試略過；`pip check` 通過。
- 隔離 Docker Mosquitto 的 HTTP／MQTT 驗收通過，包含未核准不啟動、雙設備運轉與完成、Broker 中斷後回報過期、恢復原行程、重複請求不重複啟動及人工中止。
- 五個服務的啟停腳本、前端／API 與 Streamlit 健康檢查通過；瀏覽器確認模型渲染、設備點擊、旋轉不誤選、浮動名稱、燈光互動、待核准卡片及 375px 手機單欄版面無橫向溢位。
- 驗收使用隔離資料庫、主題與連接埠，測試程序及容器已清理，原 Broker 與任務資料保留；本次補寫更新紀錄沿用上述驗證結果，未重跑功能測試。

### Known limitations

- 冷氣與洗衣機為虛擬設備；電視、窗簾、插座僅提供資訊展示，電風扇為場景擺設。
- 燈泡為本頁展示互動，不連接 MQTT，重新整理後恢復開啟。
- 本輪新增介面的完整整合驗收使用 MQTT，未重新執行 Matter 配對驗收。

操作及驗證結果見 [3D 智慧客廳](docs/LIVING_ROOM_3D.md)。

## [1.0.0] - 2026-10-18

整合多 Agent 家庭助理、冷氣控制、台電增量費用估算與 Matter 虛擬家電展示。對應 commit：`v1.0.0: add multi-agent household assistant with tariffs and Matter demo`。

### Added

- Main 委派 Washer／AC 子 Agent，支援部分成功、持久化去重、父子工作紀錄與逐設備核准；共用 15 次模型請求、120 秒總預算、單次 20 秒上限。
- 可控冷氣支援準時啟停、舒適與降溫預測、精確用電、關機回溫及費用提醒。
- 台電住宅非時間、簡易二／三段式增量估算，包含每月基準、合併負載、費率快照及 2026／2027 離峰日日曆；記錄來源與取得限制。
- Matter controller／bridge、標準操作與核准上下文群集、配對及命令去重持久化；鎖定 `@matter/main@0.17.9`，設備使用 mock 展示，失聯不繞過 Matter。
- 對話式首頁、「開冷氣 1 小時」「安排洗衣」快捷按鈕、同頁計畫卡與可保存偏好。

### Changed

- 模型順序採 vLLM 主用、Azure 備援，切換後保留上下文。
- SQLite 原地遷移至 v2，保留舊洗衣資料；每設備獨立任務與核准，共用傳輸介面並記錄累積用電區段。
- 設備入口改為冷氣、洗衣機、用電紀錄，計畫確認、修改、取消、進度與停止集中在同一張卡；進階設定與工具紀錄收至側欄。
- 冷氣「確認後開啟」在核准當下重算起訖與室溫，明確預約維持原時間；修改會暫停尚未啟動的舊計畫。
- 每月基準改為月份與度數輸入，缺少基準不默認零，跨月設定保留。
- 啟停腳本支援 `-Transport` 與隔離執行目錄，並清除檔尾多餘空白行。

### Fixed

- 修正連續需求可能誤用上次已結束任務的問題，保留既有任務與核准紀錄。

### Validation

- 2026-10-18 提交前複驗：unittest／AppTest 執行 75 項，`OK (skipped=2)`，約 24.061 秒；兩個真實模型項目未啟用。`pip check`、三個 Matter 程式的 Node 語法與暫存變更空白檢查通過。
- 2026-10-17 第二階段驗收：67 項測試、`OK (skipped=2)`；實際 vLLM 雙設備提案及真實 MQTT／Matter 復原、配對、中止、重啟、禁止繞過與瀏覽器獨立核准／取消通過。
- 2026-10-17 家庭助理驗收：75 項測試、`OK (skipped=2)`；真實 vLLM 以 10 次請求、57.375 秒完成未指定時間的雙設備提案，瀏覽器實測同頁核准冷氣、Matter 開機與停止，洗衣維持未核准。
- 家庭助理驗收使用隔離的 8505 環境，已執行任務已停止／取消，交付時另保留一份未核准冷氣卡供試用，原任務資料保留。這是驗收當時狀態，不代表服務目前仍在線。
- 本次版本格式調整僅修改文件與 commit message，未重跑測試、真實模型、MQTT／Matter 或瀏覽器驗收。

### Known limitations

- 模型連線成功另記為使用者回報；Azure 實際故障切換、硬體／跨主機 Matter 尚未驗收；完整帳單不在本版範圍。

操作與證據見 [第二階段操作指南](docs/PHASE2_GUIDE.md)、[第二階段驗收](docs/PHASE2_VALIDATION.md)、[家庭助理操作](docs/ASSISTANT_UI.md) 與 [家庭助理驗收](docs/ASSISTANT_UI_VALIDATION.md)。

## 2026-10-17 — feat: implement approval-controlled washer agent

- 新增虛擬洗衣機、跨夜排程、安靜時段、模擬電價及 60 倍展示模式。
- 新增 SQLite 任務與核准紀錄、MQTT 控制、命令去重、取消／中止及重啟恢復。
- 新增多輪 Main Agent 工具流程、Azure／vLLM 備援與請求預算；保留無模型的表單排程。
- 擴充 Streamlit 洗衣操作畫面，保留既有冷氣監測。
- 補上設定範本、PowerShell 啟停腳本、實作計畫、操作指南及驗收紀錄。
- 驗證：46 項測試、真實 Mosquitto 復原測試、瀏覽器操作及啟停腳本通過；真實 Azure／vLLM 尚未實測。

詳細內容見 [操作指南](docs/MAIN_AGENT_GUIDE.md) 與 [驗收紀錄](docs/MAIN_AGENT_VALIDATION.md)。
