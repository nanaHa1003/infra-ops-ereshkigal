# Homelab 資料庫與設定備份設計

狀態：已正式部署並啟用排程，B2 首次備份、檔案還原、SQLite 及 n8n／Authentik 資料庫匯入已通過。其餘驗收項目見 docs/backup.md。

## 已確認的需求

- 第一階段只備份資料庫與設定，不包含照片、影片、下載內容。
- 每 6 小時執行，資料庫依序匯出，不平行增加負載。
- 保留最近 48 小時全部版本，以及每日 14 份、每週 8 份、每月 12 份。
- 加密上傳 Backblaze B2，備份失敗告警，首次完成後測試還原。
- Healthchecks.io 帳號及備份 Check 已建立，使用 UUID Ping URL。
- B2 專用 private bucket 已建立，Object Lock 不啟用；Application Key 限該 bucket 與 `ereshkigal/` prefix。
- 使用者已將 UUID Ping URL 存入 `vault_secrets.backup.jobs.database.healthchecks_ping_url`。
- bucket 為 `homelab-db-snapshots`，S3 endpoint 為 `s3.us-west-004.backblazeb2.com`。
- 使用者已授權將本文件與既有修正 commit 並 push 目前分支；實際部署由使用者互動輸入 sudo 密碼。

## 方法與取捨

建議使用 restic，透過 B2 S3-compatible API 儲存加密、去重的快照。
相較直接上傳完整壓縮檔，restic 統一管理版本、保留及完整性檢查。
相較 PostgreSQL WAL/PITR，定期邏輯匯出較符合目前約 6 小時的資料損失容忍度，
並能共用於 MariaDB、SQLite 與設定檔。第一階段不建置 PITR。

## 備份清單

| 服務 | 納入 | 排除及限制 |
|---|---|---|
| Authentik | PostgreSQL 邏輯匯出、templates、Compose 所需設定與 secrets | Redis、media 圖片；圖片須另行備份才能完整還原 |
| n8n | PostgreSQL 邏輯匯出、持久化 encryption key 與設定 | 執行紀錄的外部 binary data、快取與日誌；資料庫備份不能取代外部附件 |
| Immich | PostgreSQL 邏輯匯出、版本及擴充套件資訊、部署設定 | 整個媒體 library、ML cache、Redis；無法救回遺失的原始照片 |
| WordPress | MariaDB 邏輯匯出、wp-config.php、自訂程式與 theme/plugin 設定檔 | uploads 媒體、快取及可重建 core；完整網站還原需另有媒體備份 |
| Mealie | SQLite 一致性備份、應用設定、自訂 prompts | 圖片、附件、快取；需先確認實際資料庫位置與非媒體檔案清單 |
| Pinchflat | SQLite 一致性備份、非媒體設定 | downloads、日誌、快取；需先確認實際資料庫位置 |
| Homepage | config | 快取、日誌 |
| Aria2 | config 與 session 狀態 | downloads 與未完成檔案；動態 session 以應用安全匯出方式取得 |
| Caddy／socket proxy／DDNS | Compose、Caddyfile、DDNS 腳本、Caddy 持久化憑證及設定 | Docker socket、容器日誌 |

採明確白名單，不直接遞迴備份整個 storage_root 或所有 stack 目錄。
備份實作前先唯讀確認 SQLite 路徑、WordPress 自訂檔案、n8n key 與 binary data 配置。
未辨識的必要檔案須明確報錯，不能靜默宣告成功。
New-API 已退休，不重啟、不讀取其資料；LANraragi 與 Home Assistant 不在本 repo 範圍。

Ansible repo（含尚未提交的變更）及加密 Vault 位於控制端，不假設它們存在伺服器。
需另提供控制端加密備份／離線保存步驟；伺服器備份不能自動涵蓋控制端檔案。

## 執行與一致性

- 新增獨立 backup role 與 backup.yml；一般 site.yml 不自動啟用備份或拉取更新。
- 初始化可由使用者明確指定 `backup_initialize_repositories=true` 交給 Ansible 執行；預設關閉。
  先驗證 repository 是否可讀，只有 restic 明確回覆不存在（exit 10）才建立；權限或密碼錯誤不得當成不存在。
- 使用 root systemd service/timer，依 Asia/Taipei 每日 00、06、12、18 時執行。
- root-only staging 目錄使用固定來源路徑，以免每次時間戳路徑破壞保留分組。
- 使用互斥鎖避免備份、維護同時執行；逾時或重疊不能回報成功。
- PostgreSQL 使用各容器內相符版本的 pg_dump；記錄版本、擴充套件及還原角色需求。
- MariaDB 使用一致性邏輯匯出，包含還原需要的 triggers/routines/events；檢查資料表引擎，
  不把 single-transaction 對非交易型資料表視為一致性保證。
- SQLite 使用 backup API，再檢查產物；不得直接複製運行中的 db/WAL 檔。
- 不依賴複製原始 PostgreSQL/MariaDB data 目錄；預設不停止服務。
- 每個資料庫各自一致，整批不是跨服務同一時間點快照。
- 完整匯出成功、產物驗證、restic 上傳全部成功後才送成功 ping。
  任一來源失敗即視為整批失敗，不上傳不完整批次為成功快照。
- 保護暫存檔，完成後清除本次產物；失敗後下次執行不得沿用過期產物。

## B2 與秘密管理

- 使用專用 private bucket 與限該 bucket 的 Application Key，不使用 master key。
- repository URL 使用 `s3:https://s3.us-west-004.backblazeb2.com/homelab-db-snapshots/ereshkigal`，所有物件位於
  `ereshkigal/` 下，與 Application Key 的 prefix 限制一致。
- Vault 將秘密分成 repositories 與 jobs；目前 repository 與 job 識別碼皆為 `database`，
  工作範圍仍包含 DB 與 config。repository 憑證使用
  `vault_secrets.backup.repositories.database.access_key_id`、
  `vault_secrets.backup.repositories.database.secret_access_key`、
  `vault_secrets.backup.repositories.database.restic_password`；工作監看使用
  `vault_secrets.backup.jobs.database.healthchecks_ping_url`。B2 keyID 對應 AWS_ACCESS_KEY_ID，
  applicationKey 對應 AWS_SECRET_ACCESS_KEY；repository 密碼獨立產生並保存在主機以外。
- 非秘密設定放在 inventory 的 `backup_repositories.database.url` 與
  `backup_jobs.database`（repository 引用、來源、排程、保留政策）。新增媒體工作或其他
  S3 provider 時建立新的 job／repository，不把全域單一目的地寫死在備份程式中。
- 權限涵蓋 repository 所需列舉、讀取、寫入、刪除；建置時依官方文件驗證。
- 先不啟用 Object Lock 或自行清除 restic 物件的 lifecycle rule，以免與保留政策衝突。
  B2 非目前版本／隱藏物件的計費與清理另外確認，不能只以 restic 顯示容量估算帳單。
- B2 key、restic repository 密碼、Healthchecks Ping URL 存入 Ansible Vault；
  目標主機的秘密檔 root:root 0600，秘密目錄 0700，Ansible no_log 且禁止 diff。
- 備份密碼、B2 存取方式、Vault 解密方式須保存在主機以外的密碼管理器／離線位置；
  避免把唯一還原金鑰只存於它所加密的備份內。
- 不將秘密值、完整 SQL 或原始錯誤輸出送到 Healthchecks／公開日誌。

## 告警與維護

- Healthchecks Simple period 6h、grace 2h；Email 為初版通知管道。
- 回報 start、success、fail；start 後 2h 未成功也會被視為逾時。
- 主機完全不回報時，由外部 Healthchecks 告警；不依賴同機 Home Assistant／ntfy。
- Healthchecks 暫時不可達不得刪除已完成的備份；回報失敗另記錄，不能偽裝監看正常。
- 每週維護套用 restic 保留與 prune，與備份共用鎖；先驗證 forget dry-run。
  保留選項取聯集，固定 host/tag/source 分組，維護只處理本備份集合。
- 每週做 repository metadata check，每月輪替讀取部分備份資料，首次還原另做完整驗證。
- 未來 ntfy／Home Assistant 串接獨立處理，不阻擋第一階段備份。

## 驗收與還原

1. 測試模擬 dump 失敗、上傳失敗、鎖競爭、回報失敗，確認沒有錯誤成功訊號或過期資料混入。
2. 使用假秘密測試模板、秘密權限、排除清單、保留分組、timer 與 Ansible 冪等性。
3. 首次手動執行，確認 B2 snapshot、Healthchecks 成功回報，測試通知與逾時告警後恢復。
4. 從 B2 還原到隔離環境，以相符資料庫映像／Immich 擴充套件匯入；
   檢查 SQLite integrity、PostgreSQL/MariaDB 資料及 n8n key 配對，不覆寫正式資料。
5. 記錄還原步驟與耗時；暫不承諾尚未測量的恢復時間。
6. 觀察一週的 dump 耗時、實際上傳量、B2 用量，確認頻率與保留容量適合。

資料損失目標約 6h，另加備份完成所需時間，且以持續成功為前提；
8h 是監看告警門檻，不是資料損失保證。未備份媒體的完整應用還原不在本階段驗收範圍。

## 參考

- [restic B2／S3 repository](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [restic 備份與錯誤處理](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic 保留政策](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Immich 備份還原](https://docs.immich.app/administration/backup-and-restore/)
- [Healthchecks 排程與寬限](https://healthchecks.io/docs/configuring_checks/)
