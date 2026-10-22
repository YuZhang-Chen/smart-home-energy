# 智慧燈泡測試與驗收

首版測試以「使用者送出明確指令 → 控制服務派送 → 燈泡受理 → 新鮮狀態回報 → 畫面與 Agent 確認」為主線。展示時間只是場景光照，不會自動送出燈泡命令；日夜自動規則另階段測試。

| 層次 | 主要案例 | 成功條件 | 目前證據 |
| --- | --- | --- | --- |
| 領域／設備 | 預設關閉；40% 約 Level 102、2700K 約 370 mired；關燈保留設定；非法值、重複 ID、過期或舊版本；漸變與重啟 | 設備狀態與持久化一致，錯誤命令不改狀態 | `tests/test_bulb.py` |
| 控制／HTTP | request_id 重送；accepted 尚未完成；最終回報後 confirmed；狀態過期時拒絕派送 | 一次指令、設備回報前請求保持 pending | `tests/test_bulb.py`、`tests/test_api.py` |
| Agent 權責 | Main 查詢與委派；Light 只操作 bulb-1；原始需求 40% 不可改成 60%；假模型送出後不宣稱已亮燈 | 越權不新增控制請求，結果等待設備回報 | `tests/test_light_agent.py` |
| 前端 | 提交 40% 暖光後仍顯示舊回報；新回報到達才顯示 40%；過期時停用控制；切換 12:00／21:00 不送命令 | 畫面、3D 與回報同步，展示時間不控制設備 | `web/src/controls.test.tsx`、`web/src/dayNight.test.ts` |
| MQTT 整合 | 隔離 Broker 啟動燈泡與控制服務；開／關／調光／調色溫；斷線、重連、重送、重啟 | 三個主題的命令／結果／狀態與資料庫一致 | 已通過；`artifacts/phase2-mqtt-smoke.json` |
| Matter 整合 | 配對 endpoint 4；OnOff、LevelControl、ColorControl 命令及標準屬性讀回；關燈調色溫；bridge 中斷 | Matter 標準屬性、MQTT 狀態、HTTP 狀態一致；不迴圈發命令 | 已通過；`artifacts/phase2-matter-smoke.json` |

本機回歸指令（PowerShell，於專案根目錄）：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm.cmd test --prefix web
npm.cmd run build --prefix web
node --check matter/bridge.mjs
node --check matter/controller.mjs
```

`test_api.py` 與 Agent 測試使用 Windows asyncio 本機 loopback；若執行環境禁止 loopback，測試可能停在事件迴圈建立階段，需在允許本機 loopback 的環境執行。真實模型測試另以環境變數啟用；一般回歸使用假模型，不需要付費 API。Matter／MQTT 整合使用隔離資料庫、`127.0.0.1:18884` 測試 Broker 與獨立 Matter storage。2026-10-20 的 Matter 驗收已確認獨立 On、Off、40%（Level 102）、2700K（370 mired）、關燈時設定 6500K（154 mired）、再次開燈保留設定、Broker 復原、controller 重啟、bridge 中斷不繞過 MQTT、bridge 復原，以及標準屬性與設備 snapshot 一致。命令 ID、bridge journal、程序日誌及設備資料庫保存在報告所指向的隔離 artifacts 目錄。

手動視覺驗收固定鏡頭與曝光，比較 12:00 燈關、21:00 燈關、21:00 暖色 40% 三張畫面。前兩張不得因切換展示時間改變燈泡 power；第三張必須在回報 power=true、level≈102、mired≈370 後才點亮。重新整理或第二個頁面應得到同一狀態。
