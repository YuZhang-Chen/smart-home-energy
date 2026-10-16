# PoC 驗收紀錄

驗收日期：2026-10-16（Asia/Taipei）。

## 執行環境

- Windows、Python 3.12.13，使用專案既有 `.venv`。
- Paho MQTT 2.1.0、Streamlit 1.66.0、python-dotenv 1.2.4。
- Docker Desktop 4.55.0、Docker Engine 29.1.3，Linux containers。
- 官方 `eclipse-mosquitto:2` 容器，僅綁定 `127.0.0.1:1883`。
- `pip install -e .` 安裝成功；`pip check` 沒有依賴衝突。

## 自動測試

`python -m unittest discover -s tests -v`：**22 項全部通過**（含 `.env` 設定調整後複驗）。

覆蓋冷氣溫度收斂與功率上下限、實際時間積分、不變的原始模型狀態、JSON 與 UTC 時間、錯誤資料與設定、有界歷史、過期判定、重新訂閱及離線不排隊。Streamlit AppTest 驗證等待畫面、數字格式、過期提示，以及重繪後只有一個訂閱端。

設定測試另驗證 `.env` 載入、既有環境變數優先、檔案缺失與必要欄位缺漏時的錯誤訊息。實際專案 `.env` 已成功載入，`pip check` 沒有依賴衝突。

圖表首次載入可能超過 AppTest 預設的 3 秒，畫面測試使用 15 秒上限。AppTest 在獨立執行時可能輸出 `missing ScriptRunContext` 提示，測試結果仍為通過。

## 真實 MQTT 驗收

執行 `python tests/mqtt_smoke.py --recovery`，結果為 **passed**：

| 項目 | 實測結果 |
| --- | --- |
| 連續收集時間 | 30.00 秒 |
| 收到筆數 | 15 筆 |
| 最短資料間隔 | 2.001676 秒 |
| 最長資料間隔 | 2.009399 秒 |
| Broker 延後啟動 | 自動連線並開始接收 |
| 錯誤 JSON | 記錄警告，後續有效資料正常接收 |
| Broker 停止再啟動 | 自動重連並恢復訂閱，累積用電持續增加 |
| 離線資料 | 序號跨越 3，沒有補送舊快照 |
| 模擬器重新啟動 | 新 run_id、用電重新累積、歷史只保留新一輪 |

完整數據由測試產生於 `artifacts/mqtt-smoke.json`，每次執行會更新。

## 實際瀏覽器檢查

已在 Codex 瀏覽器開啟 `http://127.0.0.1:8501`，確認 Broker 已連線、數字持續更新、功率曲線顯示及 Asia/Taipei 時間換算。

核對同一畫面的資料範例：

| 欄位 | 原始 MQTT 值 | 網頁顯示 |
| --- | --- | --- |
| temperature_c | 29.5719232242695 | 29.57 °C |
| power_w | 871.5769672808502 | 871.6 W |
| energy_kwh | 0.024005650779076865 | 0.024006 kWh |
| timestamp | 2026-10-16T11:58:16.747422Z | 2026-10-16 19:58:16 |

窄版畫面已將單位放入指標標籤，避免用電數字遭截斷。驗收截圖位於 `artifacts/dashboard.jpg`。

本次驗收只針對教學用冷氣模型與本機監測資料流，沒有評估真實冷氣效率或節能效果。
