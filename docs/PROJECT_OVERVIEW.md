# 專題總覽

## 題目

**基於多代理Agent、Matter 與 MQTT 之情境感知智慧家庭能源管理系統**

## 專題簡介

本專題建置一套結合多代理 Agent、數位孿生與地端大型語言模型串接能力的智慧家庭能源管理系統。使用者可用自然語言提出冷氣、洗衣與照明需求；系統結合設備即時狀態、使用者偏好、時間限制、費率與既有排程，判斷是否需要補充資訊，再由多個 Agent 協作產生可檢查的設備計畫。冷氣與洗衣計畫分別交由使用者核准，控制服務才會透過 MQTT 或 Matter 通訊路徑執行，並依設備回報更新任務、能耗與 3D 場景，讓系統狀態與畫面保持同步。

專題關注的不只是自然語言理解，也包含設備控制的安全邊界：模型負責理解、查詢與提出計畫；程式負責驗證任務條件、核准權限、計畫版本與設備回報。Agent、Runtime 或瀏覽器中斷時，已保存的對話與任務仍由 Python 服務及 SQLite 管理。

## 研究目標

1. 以 Main Agent 協調設備專屬子 Agent，將跨設備需求拆成可獨立檢查與核准的工作。
2. 根據設備回報、排程時間、安靜時段、使用者偏好及電價等情境資訊，提出可行的家庭能源操作計畫。
3. 以版本化任務、使用者核准與持久化請求，限制模型直接操作設備的能力。
4. 整合 MQTT 與 Matter 通訊路徑，讓命令確認、設備回報、失聯偵測與重新連線能回到同一套任務狀態。
5. 建立狀態同步型數位孿生，讓 3D 家庭場景、設備面板、排程與用電資訊共同反映後端確認的設備狀態。
6. 支援地端大型語言模型推論服務串接，使模型端點與部署位置可依研究及家庭環境設定。

## 使用技術

| 領域 | 技術 | 在專題中的用途 |
|---|---|---|
| 前端 | Vite、React、TypeScript | 建置本機單頁應用程式與家庭管理介面 |
| 3D 場景 | Three.js、React Three Fiber、Drei | 呈現客廳、設備、相機操作及晝夜效果 |
| 數位孿生 | 設備回報、SQLite 狀態快照、FastAPI、React 共用家庭狀態 | 將後端確認的設備狀態同步至 3D 場景與操作面板 |
| 對話介面 | CopilotKit React v2、Copilot Sidebar | 在 React 中提供家庭助理對話、進度與計畫卡 |
| Agent 串流 | AG-UI Protocol、Server-Sent Events（SSE） | 傳遞 Agent 執行事件、文字回覆與設備計畫卡 |
| Copilot Runtime | Node.js、TypeScript、CopilotKit Runtime、`HttpAgent` | 提供 `home` Agent discovery，將 CopilotKit 工作轉接至 Python API |
| 後端 API | Python、FastAPI、Pydantic | 提供 REST API、請求驗證、對話入口與 Runtime 同源代理 |
| 多代理與模型 | Python Main／Washer／AC／Light Agents、OpenAI SDK、vLLM、Azure OpenAI | 分析需求、呼叫工具、委派設備任務；可透過 vLLM 串接地端 LLM 推論服務，Azure OpenAI 為備援 |
| 資料保存 | SQLite | 保存設備狀態、請求、任務版本、核准資料、對話、Agent 事件及稽核紀錄 |
| 訊息傳輸 | MQTT、Eclipse Mosquitto、Paho MQTT | 傳送設備命令與遙測回報 |
| 智慧家庭標準 | Matter、Node.js Matter SDK（`@matter/main`） | 透過 Matter Controller 與 Bridge 傳遞設備操作及讀回狀態 |
| 測試與建置 | pytest、Vitest、TypeScript、Vite | 驗證後端規則、前端互動、型別與正式建置 |
| 本機服務 | PowerShell、Docker Compose | 啟停 API、Runtime、設備模擬服務與 MQTT Broker |

CopilotKit Runtime 負責前端 Agent 協定接合，不保存家庭業務狀態。React 透過 FastAPI 的 `/api/copilotkit/*` 同源代理連線；Python API 再以 AG-UI SSE 傳送 Agent 事件。任務與事件資料的唯一權威來源是 Python／SQLite。

## 數位孿生與狀態同步

本專題採用**狀態同步型數位孿生（Digital Twin）**：以設備的數位狀態對應 3D 客廳中的設備，讓系統狀態與畫面同步。設備透過 MQTT 或 Matter 路徑回報狀態，由 Controller 保存至 SQLite；React 約每兩秒讀取 `/api/home`，並由同一份家庭狀態更新 3D 場景與設備面板。例如燈泡回報的開關、亮度與色溫會對應到場景中的燈光，冷氣與洗衣的任務狀態則顯示於設備資訊與操作介面。

使用者從畫面提出操作後，請求須經控制服務驗證與設備回報確認。系統分別呈現需求處理、設備執行與確認結果；設備回報超過十秒未更新時，介面標示過期，供使用者判斷資料是否仍有效。目前數位孿生同步對象為專案內的虛擬設備端點。

## 地端大型語言模型串接

Python Agents 透過 OpenAI SDK 呼叫相容 API，**可串接在本機或區域網路伺服器部署的地端大型語言模型**。vLLM 負責提供模型推論服務，實際使用的模型由服務端部署，專案以端點及模型名稱設定連線。例如：

```dotenv
VLLM_BASE_URL=http://127.0.0.1:8000/v1
VLLM_MODEL=your-served-model-name
VLLM_API_KEY=
```

上述為設定範例；模型名稱與驗證金鑰須對應實際部署服務。系統優先使用設定的 vLLM 服務，服務異常時可切換至已設定的 Azure OpenAI 備援。模型推論服務與家庭控制服務分開執行，已核准的設備工作由本機 Controller 持續處理。

## 多代理分工

- **Main Agent：**取得家庭狀態與費率資訊，分析整體需求，將洗衣與冷氣工作交給對應子 Agent，彙整結果並回覆使用者。
- **Washer Agent：**檢查洗衣行程、完成期限、安靜時段與費用，提出可核准的洗衣計畫。
- **AC Agent：**依冷氣目前狀態、目標溫度、運轉時段與能源估算提出冷氣計畫。
- **Light Agent：**處理明確的燈泡開關、亮度及色溫需求；操作結果須由設備回報確認。

所有 Agent 共用請求數及時間預算。Agent 可以查詢狀態、計算與提交提案，但不能自行核准、中止或解除設備異常。

## 系統操作流程

1. 使用者從 CopilotKit Sidebar 輸入需求。
2. FastAPI 將 AG-UI 工作保存到 SQLite 的持久化佇列。
3. Python 背景 worker 呼叫 Main Agent，必要時委派設備子 Agent；Agent 查詢設備、偏好、費率與任務資料並建立提案。
4. CopilotKit 收到執行事件與保存好的計畫卡；前端可查詢任務版本，避免過期卡片送出核准。
5. 使用者分別核准冷氣與洗衣計畫。Controller 再檢查任務版本、核准內容、時間與設備狀態，將命令送往所選傳輸路徑。
6. MQTT 或 Matter 路徑回報設備結果；Controller 更新 SQLite，React 共用家庭狀態並刷新 3D 場景、任務進度及用電紀錄。

## 系統架構與入口

| 元件 | 預設位置 | 主要職責 |
|---|---|---|
| React SPA 與 FastAPI | `127.0.0.1:8502` | 家庭操作介面、REST／AG-UI API 與 Runtime 同源代理 |
| Copilot Runtime | `127.0.0.1:8503` | CopilotKit Runtime、Agent discovery 與 Python Agent 轉接 |
| MQTT Broker | `127.0.0.1:1883` | MQTT 命令與設備回報的訊息傳輸 |
| Python Controller／Agents | 本機背景程序 | 請求處理、核准檢查、設備控制及回報狀態 |
| Matter Controller／Bridge | 本機 Node.js 程序 | Matter 傳輸、設備狀態讀回及與 MQTT 設備模擬端點的橋接 |
| SQLite | `artifacts/` 下的本機資料庫 | 對話、任務、命令、設備狀態與事件持久化 |

## 專題實作範圍

目前 React 3D 場景、CopilotKit 對話、FastAPI、Python 多代理、排程核准、SQLite、MQTT 與 Matter Controller／Bridge 均已整合。測試涵蓋假模型與隔離資料庫的自動測試，也有可選擇執行的真實 vLLM、隔離 MQTT Broker 與 Matter bridge 端到端流程。Matter 驗收使用專案的虛擬設備端點，並未連接實體家電；相關結果記錄於[驗收文件](VALIDATION.md)。

本專題的電價與耗電數值依目前資料與設備模型計算，屬於家庭能源管理展示及研究原型，不代表實際電費帳單或特定家電的量測規格。

## 程式位置

```text
src/smart_home_energy/   FastAPI、Agents、控制服務、SQLite、MQTT／Matter 傳輸
web/src/                 React SPA、CopilotKit Sidebar、3D 場景與家庭管理頁面
web/runtime/             TypeScript Copilot Runtime 與 Python Agent 連接器
matter/                  Matter Controller、Bridge 與設備通訊程式
tests/                   單元測試、API／傳輸 smoke tests 與 opt-in live E2E
docs/                    專題介紹、操作指南、設計決策與驗收文件
```
