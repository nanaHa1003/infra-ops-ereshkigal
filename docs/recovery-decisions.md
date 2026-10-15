# 實作裁定與驗收界線

以下依執行 ledger 的裁定順序，保留本次代為決定的事項及其代價。正式操作以 [recovery.md](recovery.md) 為準；完整 Fedora43 x86_64／SELinux VM 驗收仍未完成。

| 次序 | 裁定 | 若假設不成立的代價 |
|---|---|---|
| 1 | 使用 Native 管理的獨立 worktree，原 checkout 的規劃檔保留；不複製秘密。 | 工作位於另一 checkout，需選正確分支／路徑才能看到變更。 |
| 2 | 將已核准 spec/plan 一併提交；新功能不自動 push、merge 或部署。 | 使用者還需明確決定整合／部署時機。 |
| 3 | 共用 bootstrap 增加唯讀套件衝突檢查；日常預設移除衝突套件的行為保留，recovery 則拒絕。 | 新機若已裝 Podman 等套件，必須先另行處理衝突，復原會停止。 |
| 4 | Task2 可在 Task1 凍結測試 checkout 跑 Molecule 時獨立寫 metadata；各自測試完成才記完成。 | 單一 task BASE 可能包含前項提交，需以整分支 review 確認。 |
| 5 | PG/MariaDB dump 沒有一致交易內的精確數量 baseline 時明列 unavailable；SQLite 從匯出檔計數。Git/Vault deployment revision 由控制端明確供應，不猜測。 | 無 baseline 或 revision 時需要人工核對，不能承諾資料與歷史來源完全一致。 |
| 6 | Task3 可在 Task2 的獨立 runtime 驗證期間依已通過的 v2 contract 開發。 | producer 的 runtime 問題若出現，consumer 也需重新驗證。 |
| 7 | 小型 v1/v2 假 fixture 放測試內；不取真實備份資料做公開 fixture。 | fixture 增大時需再整理，真實應用完整 schema 仍由 VM 演練驗收。 |
| 8 | YAML registry 使用 recovery_service_registry wrapper；Python 接受解開後的 map，mapping 帶 run/root/mount/tree context。 | 呼叫者若用錯層級會被拒絕，新增服務必須同步 registry 與測試。 |
| 9 | download 為內部階段，先驗證 artifacts／secrets 再 prepare；控制端只檢查目標路徑語法，目標機檢查實際 mount/symlink/platform/machine-id。 | 控制端可規劃的路徑，仍可能在目標端被拒絕，需先修掛載再還原。 |
| 10 | prepare 會產生的額外 storage/stack 目錄也列為 approved roots。 | cleanup 會移除此 run 擁有的那些目錄；啟動應用後禁止 cleanup。 |
| 11 | Docker/systemd probe 失敗視為未知；只連目標本機 daemon，清除繼承的遠端 Docker context。 | 沒有足夠 probe 權限時會中止，不把未知當成空白主機。 |
| 12 | validate-plan/download 是 backend 內部操作，對外仍用 plan/restore/verify/activate/cleanup。 | 手動跳內部階段不受操作手冊支援，必須沿用固定 plan。 |
| 13 | 驗證資料庫直接用受控 Docker argv 建立，不使用可合併 production ports/networks 的 DB-only Compose；正式啟動沿用 role start。 | 需維護兩個生命週期介面，因此真實 DB restart/handoff 有獨立測試。 |
| 14 | 本機真實 DB matrix 是 Linux arm64／Podman component test，不當成 Fedora43 x86_64 SELinux 證據。 | 不得以本次結果宣稱整台新機 DR 已驗收；需要空白 VM。 |
| 15 | n8n 私有 tmpfs 使用 mode1777，容器內 node UID 寫入，移除容器即消失；fixture 使用官方 Docker Hub 固定版本，正式使用來源 digest。未知 schema 阻擋啟動。 | registry 下載、CLI 或新版本 schema 不相容時會失敗；不能自動換新版。 |
| 16 | 分開 socket/Caddy start，保留日常順序；recovery 共用網路先建、MariaDB auto-upgrade 關閉、網域沿用、有限驗證逐服務審閱、媒體非空且人工確認、先記 start intent。 | 人工 fencing／媒體完整性確認若錯誤可能造成雙寫或缺檔；應用啟動後沒有自動 cleanup／回切。 |
| 17 | 無可用測試 VM時，實際應用／SELinux／重開機接管驗收保持未完成，不用正式機替代。 | 下一步需要準備測試 VM；本分支不能直接視為 production DR 保證。 |
| 18 | SQL engines 用獨立五項真實 matrix，Molecule 驗證 file/SQLite backend 與 UID1007/GID1008；容器銷毀清理全部假資料。 | Molecule 不涵蓋完整 Fedora43 recover.yml；完整 orchestration 仍需要另外的整合測試與 VM 驗收。 |

獨立程式碼審查發現與修正紀錄將一併附在交接中；不能以測試數量取代實際操作路徑驗證。

獨立審查另明確保留以下範圍判定（依審查列出的順序）：

| 次序 | 保留的範圍判定 | 代價 |
|---|---|---|
| 19 | Fedora43 x86_64／SELinux enforcing 沒有可用 VM，不把它假定通過。 | 正式 DR 與重開機行為仍未驗收。 |
| 20 | 完整登入／OIDC 及其他 image 的實際 app readiness 留給 VM 演練；已知 FPM 問題則必須現在修正。 | 個別 app 的接管仍可能被 readiness 阻擋，不能直接用正式機驗收。 |
| 21 | DNS／VPN 切換維持人工，不提供自動回切。 | 操作者需要核對路由與登入，接受新寫入後自行處理回退資料。 |
| 22 | 媒體重建與完整性排除於本次備份；外部媒體 reference 只是人工確認。 | 不保證照片／uploads／下載／附件完整，也不能以目錄非空取代內容驗證。 |
| 23 | 其他 OS、ARM64 production、版本升級不納入第一版。 | 那些平台／遷移需要另加矩陣，不能直接啟用。 |
| 24 | 沒有 PG/MariaDB 歷史精確數量時維持 limited/manual。 | 成功匯入與 FK／權限檢查不代表每筆歷史資料完全一致。 |

本次獨立審查沒有列出延期的小問題。四項需修正的具體問題是 prepare 誤接 start、跨 run 的空目錄認領、FPM HTTP probe、續跑跳過 Vault 比對；修正各有 RED→GREEN 測試，最終結果見操作手冊的驗證紀錄。

後續修正補充：目錄認領改為固定主機層的持久 index，不能因空目錄、不同 state_root 或中斷而重複認領；index 遺失或損壞時會拒絕續跑，必須先人工查明，不自動接管既有資料。Native worktree 與本計畫 ledger 保留至 VM 驗收，尚未執行整合／推送決策；代價是保留少量本機工作檔，避免下一輪遺失待驗收事項。
