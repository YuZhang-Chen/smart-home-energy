# 第二階段操作指南

更新：2026-10-17（Asia/Taipei）。此版包含 Main → Washer / AC Agent、獨立核准、可控冷氣、住宅增量費用與真實 Matter 橋接。驗收證據見 [PHASE2_VALIDATION.md](PHASE2_VALIDATION.md)。

介面已改為對話、快捷按鈕與同頁計畫卡，最短流程見 [ASSISTANT_UI.md](ASSISTANT_UI.md)。

## 啟動

需求為 Python 3.12+、Docker Desktop；Matter 模式另需 Node.js 22+。保留既有 `.env`、SQLite 與 Matter 配對目錄，新設定參考 `.env.example`。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
npm.cmd ci --prefix matter
.\scripts\start.ps1 -Transport matter
```

開啟 [本機儀表板](http://127.0.0.1:8501)，直接輸入需求或點快捷按鈕。首次配對可能需要數十秒，日誌在 `artifacts/runtime/`。`-Transport mqtt` 為預設；Matter 驗收須選 `matter`。已有 Broker 時加 `-NoBroker`。測試可指定 `-DashboardPort 8505 -RuntimeDirectory artifacts/my-demo/runtime`；停止時使用相同 RuntimeDirectory。

啟動腳本只啟動一個可控冷氣程序 `smart_home_energy.ac`，它也發布相容的舊監測資料。請勿同時啟動舊 `publisher`。MQTT 模式有 ac、washer、control、dashboard 四個背景程序；Matter 多一個 Node bridge，Node controller 由 control 管理。

```powershell
.\scripts\stop.ps1
# 同時停止本專案 Broker：
.\scripts\stop.ps1 -StopBroker
```

停止腳本核對 PID、執行檔與啟動時間，清理相符程序及其子程序。設備運轉中被停止，下次啟動會回報故障，需人工查核解除。

## 家庭需求與個別核准

1. 首頁輸入「幫我開冷氣，順便安排洗衣」，或使用快捷按鈕。Main 委派子 Agent 各自多輪工具；沒指定時間時使用已同意的常用偏好，明確條件不會被預設覆蓋。
2. 兩台計畫直接顯示在首頁，同頁各自確認與追蹤，可以同時運轉。一台補問、無解或失敗不撤銷另一台。
3. 模型不可用、超限或條件不足時使用各設備表單。家庭需求、委派識別碼、父子工作、工具結果、模型供應者與預算均保存於 SQLite；重試相同委派不建立第二個任務。
4. 每台設備最多一個未完成任務。未知或故障只佔該設備；另一台可繼續。開始前可取消，開始後需確認中止；收到設備回報才判定成功。故障先查核，再人工解除。

Main 與子 Agent 共用最多 15 次請求、120 個真實秒、單次 20 秒，包含備援嘗試。沿用目前設定：vLLM 主用，失敗才切 Azure，保留上下文且本次工作沿用備援。已完成提案在預算用盡後保留。一般測試不呼叫付費模型，連線測試須明確啟用。

## 冷氣舒適管理

表單可設定起訖、目標、舒適範圍、環境溫度及回溫常數。預設 27°C、26–28°C、環境 32°C、關機回溫常數 30 分鐘。計畫顯示用電、費用與預估達標時間；時段內無法達到舒適範圍時拒絕提案。

到指定時間才開機，不提前預冷。開機採原教學模型的指數降溫，功率 100–1,200 W；以精確積分計算用電。時段結束由設備自主關機，關機功率為零並向環境溫度回復。此模型供展示，不代表真實冷氣效率。

核准保存起訖及允許溫度範圍。擴大時間、放寬舒適範圍或改變模型參數需要重新核准。費用超過預測會產生提醒，不因費用自行關機。持續偏離舒適範圍、需求或設備異常才觸發 Agent；每筆遙測不呼叫模型。已核准設備不依賴模型持續在線。

## 費率與基準用電

在左側「偏好與進階設定」選擇模擬、住宅非時間、簡易二段式或三段式。基準以選月份與輸入度數儲存，例如 2026-10 為 350 度、2026-11 為 320 度；保留其他月份，不必填 JSON，未輸入時不會默認為零。基準須排除本系統任務。正式估算以日曆月拆分流動電費差額，不分攤固定基本費。

住宅使用累進級距；簡易時間電價使用夏月、台電離峰日及時段，另計每月超過 2,000 度的加收。家庭總額以合併負載計算，不能加總每台單獨計算的邊際費用。未完成的核准任務使用預估負載，已結束任務使用設備回報區段的平均功率；未核准提案不算入總額。

每份提案保存方案、基準、已核准負載、來源雜湊與日曆。核准時重算／核對快照；另一台已核准或基準、費率變更時，舊報價可能需重新估算再核准。住宅非時間電價在相同月份、用電下，單純移動開始時間沒有節費效果。

缺少月份基準、日期早於費率實施日，或時間電價缺少年度日曆時，正式估算停止並顯示原因。按「下載並查核台電費率」才更新來源；失敗保留既有快照並顯示原因，不靜默換成模擬費率。

目前內附來源實施日為 2025-10-01，查核日 2026-10-17；台電離峰日覆蓋 2026、2027 年。來源與取得方式保存在 `src/smart_home_energy/data/`。2026 年由官方 PDF 的索引表格查核，原 PDF 下載端點不可用；2027 年下載官方 PDF 並核對紅色離峰標記。日曆不是一般國定假日清單。可用 `scripts/import_tariff_calendar.py` 匯入官方 PDF；需額外安裝 `pip install -e '.[calendar]'`，匯入後仍應核對輸出。

## Matter 路徑與持久化

固定路徑為控制服務 → Node Matter controller → Matter bridge → MQTT 虛擬設備。設備回報先更新 bridge 屬性，再經 Matter 讀回控制服務。controller 不直接連 MQTT，Matter 失聯不會退回直接 MQTT。

鎖定 `@matter/main@0.17.9`。洗衣為 LaundryWasher 的 OperationalState Start/Stop；冷氣為 RoomAirConditioner 的 OnOff 與 Thermostat。專案擴充 HemsTask 群集傳送分段命令上下文、版本、核准與行程 ID；標準操作缺少匹配上下文會被拒絕。橋接結果與設備命令均持久化去重。

`HEMS_MATTER_STORAGE` 保存 controller fabric、bridge 身分、配對及端點對應；相對路徑以專案根目錄解析。洗衣端點 2、冷氣端點 3。`HEMS_MATTER_PORT`、`HEMS_MATTER_DISCRIMINATOR`、`HEMS_MATTER_PASSCODE` 兩個程序須一致。重啟後先查核現況，不重送舊啟動。此為單機展示，未驗證跨主機配對或第三方控制器。

控制資料庫使用版本化遷移至 v2，舊資料指定 `washer-1` 且保留歷史、核准、行程與費率快照。冷氣、洗衣與控制資料庫須分開，備份時停止服務並保留 WAL 與 Matter storage。不要以刪除資料解除故障。

完整帳單、實際抄表週期比例計費、真實家電校正與硬體 Matter 裝置不在本階段交付。
