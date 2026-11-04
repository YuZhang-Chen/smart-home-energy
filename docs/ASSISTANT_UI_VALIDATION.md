# 家庭助理介面驗收

目前助理介面由 React／CopilotKit 提供；本檔記錄遷移後驗收。部署與操作方式見 [ASSISTANT_UI.md](ASSISTANT_UI.md)，完整測試範圍見 [VALIDATION.md](VALIDATION.md)。

- CopilotKit React 與 Runtime 固定為 1.79.1，Python `ag-ui-protocol` 固定為 1.0.0，Runtime 端 `@ag-ui/client` 固定為 1.0.2。
- 對話歷史、工作狀態與 AG-UI 事件由 Python／SQLite 保存；Runtime 重連會讀取持久化狀態。
- 右側家庭助理顯示進度與已保存的設備提案；左側設備抽屜、用電、偏好／費率／時鐘及診斷頁由同一 React SPA 提供。
- 冷氣及洗衣使用個別核准卡。核准與修改攜帶任務版本；燈泡命令依明確需求入列，完成狀態依設備回報更新。
- Python 測試 83 項通過、3 項略過；React 測試 21 項通過；正式建置通過。
- 真實 vLLM 經 Copilot Runtime／FastAPI／AG-UI 委派冷氣與洗衣 Agent，建立兩張待核准卡。版本化修改及逐項核准後，隔離 Matter bridge 虛擬設備皆完成，回報同步至 `/api/home`。
- MQTT API 流程、Matter bridge 重啟、設備離線偵測與復原測試均通過。完整操作命令與報告位置見 [VALIDATION.md](VALIDATION.md)。

通訊測試使用隔離 MQTT Broker 及 Matter bridge 模擬設備，未連接實體家電。遷移前的瀏覽器與實體流程紀錄可在 Git 歷史及 [PHASE2_VALIDATION.md](PHASE2_VALIDATION.md) 查閱。
