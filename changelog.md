# Changelog

每次 commit 新增一筆，簡述做了什麼、驗證結果及尚未完成的事項；最新紀錄放最上方，標題使用對應的 commit 訊息。

## 2026-10-17 — feat: implement approval-controlled washer agent

- 新增虛擬洗衣機、跨夜排程、安靜時段、模擬電價及 60 倍展示模式。
- 新增 SQLite 任務與核准紀錄、MQTT 控制、命令去重、取消／中止及重啟恢復。
- 新增多輪 Main Agent 工具流程、Azure／vLLM 備援與請求預算；保留無模型的表單排程。
- 擴充 Streamlit 洗衣操作畫面，保留既有冷氣監測。
- 補上設定範本、PowerShell 啟停腳本、實作計畫、操作指南及驗收紀錄。
- 驗證：46 項測試、真實 Mosquitto 復原測試、瀏覽器操作及啟停腳本通過；真實 Azure／vLLM 尚未實測。

詳細內容見 [操作指南](docs/MAIN_AGENT_GUIDE.md) 與 [驗收紀錄](docs/MAIN_AGENT_VALIDATION.md)。
