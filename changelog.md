# Changelog

記錄各版本的重要變更，最新版本放最上方。格式參考 [Sample CHANGELOG](https://gist.github.com/juampynr/4c18214a8eb554084e21d6e288a18a2c)，使用版本號、日期及 Added／Changed／Fixed 分類；版本 commit 以對應的 `v{版本號}:` 開頭。既有未編版紀錄保留原標題。

## [Unreleased]

尚無待發布變更。

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
