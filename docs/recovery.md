# 替代主機復原

此功能從固定 Restic 快照還原資料庫與設定到**另一台空白 Fedora 43 x86_64 主機**，不需要來源主機 SSH。`recover.yml` 是獨立入口，`site.yml` 不包含任何復原操作。照片、影片、下載、WordPress uploads、Mealie 附件不在目前備份範圍。

目前開發驗證通過的範圍及尚待驗收項目列於文件末尾；正式災難復原前，必須先完成空白 Fedora VM 演練。

## 事前準備

- Git checkout 與快照對應的 role/template 版本；控制端 Python/YAML、Ansible collections、Restic。
- 歷史 Ansible Vault、Vault 解密方式、Restic 密碼、來源 B2 限定 prefix 的讀取憑證。這些不能只放在故障伺服器裡。不要把秘密放進 `-e`、命令列、Git 或報告。
- 空白替代主機、sudo、Docker 可安裝、明確的實體儲存掛載點與足夠暫存空間。既有 Podman/Buildah 等衝突套件會被拒絕，不會由復原流程移除。
- 來源 snapshot 的完整 image digest／Linux OS／amd64 架構資料。無 digest 或架構不符時先解決版本缺口；不使用最新 image 猜測。
- 設定只存於本機、受 Git ignore 的 inventory/profile。從 `inventory/recovery.ini.example` 與 `inventory/recovery-profile.yml.example` 複製。替代機先保留在 `recovery_targets`，不要加入正式 `homelab` 群組。

`recover_source.host/job/staging_path` 描述**原始備份**，不改成新主機。`storage_root`、`stack_root`、`recover_storage_mount` 描述**新機**，可使用完全不同路徑。`domain` 必須沿用來源網域；改網域是另外的遷移作業。`recover_vault_revision` 記錄你選擇的歷史 Vault 版本。

舊版 manifest 必須用 `recover_legacy_profile` 補足 source kind、原檔案位置與有 service label 的 image digest。資料庫可明確補 `version/database/role/extensions/preload_libraries/definers/verification`，網域放在 `recover_legacy_profile.domain`。不要把現在線上的數量填成歷史快照 baseline。不知道的數量保持 unavailable；不知道的 image 不能略過。

## 規劃、還原與驗證

所有操作都必須指定單一 `--limit`。下列範例的 inventory/profile/plan 都是控制端檔案。第一次 `plan` 不變更替代主機的服務或資料；`--check` 一律只執行 plan。

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=plan --ask-become-pass
```

保留輸出的 `.recovery/<run_id>.plan.json`；它固定完整 snapshot ID、來源 scope、目標 machine-id、services、路徑、image digests 與 profile hash。後續操作在 profile 增加 `recover_plan_file: /absolute/path/.recovery/<run_id>.plan.json`。不要修改產生的 plan。改目標、服務或歷史 Vault 版本時重新規劃，不能拿不同 profile 接續既有 run。

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=restore --ask-become-pass
```

Restore 安裝專用工具、下載固定快照、驗證 artifacts／歷史 secrets、產生固定版本 Compose、還原檔案，再匯入資料庫並自動 verify。沒有啟動應用 server、workflow、排程、SMTP、DDNS、Healthchecks 或備份 timer。PG16/18、Immich PG14 與 MariaDB 只啟動 run-owned DB 容器，無 published ports、無 Docker socket、只有 internal network；結束或失敗時停止 DB，保留資料。SQLite 使用匯出的完整資料庫，拒絕殘留 WAL/SHM。

報告在新機 `/var/lib/homelab-recovery/runs/<run_id>/report.json`，root-only。n8n credential 明文只在 CLI 容器 tmpfs，輸出只有數量。PG/MariaDB 沒有歷史數量 baseline 時顯示有限驗證，並非與來源所有內容相同。版本不支援、解密失敗或缺媒體會有明確限制／啟動阻擋。

可重跑 verify，不會重匯入資料庫：

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=verify --ask-become-pass
```

資料庫匯入中斷會記 failed，不自動覆寫再匯入。先 preview 再用相符 run ID 清理，只限此 run 記錄的目錄、容器與 network，不能清正式服務或任意目錄。應用開始啟動後即禁止 cleanup。

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=cleanup-preview \
  -e recover_run_id=RUN_ID --ask-become-pass
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=cleanup \
  -e recover_run_id=RUN_ID --ask-become-pass
```

## 正式接管與回退界線

1. 若舊機可用：先停止所有會寫入的應用，做最後一次備份並確認成功，以該 snapshot **重新** plan/restore/verify。演練中途的舊快照不能代表最後狀態。
2. 關閉／隔離舊主機的服務、排程、外部路由與 DDNS，確保它無法重新寫入或接流量，留下 fencing 紀錄。
3. 審閱 protected report；對沒有 baseline 的有限驗證，設定 `recover_accept_limited` 每個服務的具體審閱紀錄。這不會略過失敗、未知 schema 或媒體限制。
4. 明確選 `recover_activate_services`（已還原服務子集）及依賴。Proxy/Homepage 需要 Authentik；首頁與 Proxy 一起啟用。缺照片／uploads／下載／附件的服務保持停止；其他已明選服務可單獨接管。完整媒體已由外部恢復時，必須位於服務預期目錄且非空，再提供 `recover_media_evidence.<service>.reference` 作完整性確認，工具只檢查路徑存在，不代替媒體內容驗證。
5. 設定 `recover_old_host_fenced: true`、`recover_fencing_reference`，`recover_cutover_complete: false`、`recover_enable_ddns: false`，執行 activate：

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=activate --ask-become-pass
```

工具先記錄 start intent，停止 recovery DB，再經正常 role 的 start 入口啟動固定 image，依序檢查 DB／應用 readiness，失敗即停止後續步驟。Socket proxy 在 fencing 之後才開，Caddy 最後開。此時狀態為 `awaiting_cutover`，不是已完成 DNS 接管。應用可能開始背景工作與接受寫入。

手動切換 DNS／路由，檢查登入、SSO、資料及外部連線，再將 `recover_cutover_complete: true` 設定後重跑 activate；必須包含所有已開始啟動的服務。只有明確另設 `recover_enable_ddns: true` 才安裝新機 DDNS。

**開始啟動應用或新機接受寫入後，不提供自動回退。** 如需回切，先停止新機、保留新資料、人工處理資料合併與路由；工具不會自行重啟舊機或還原 DNS。

接管後另建正式 inventory、B2 prefix／Application key、獨立 Healthchecks Check，確認新 job/source host，再明確執行 `backup.yml`。復原流程不繼承舊機 timers 或 ping URL，也不修改控制端正式 inventory。`site.yml` 只在接管與版本核對後另外執行。

## 驗證紀錄與限制

本機隔離 Linux/arm64 component tests：PG16.10、PG18.0、Immich PG14（vector/vectors/vchord、向量索引）、MariaDB11.4.5（routine/event/trigger/DEFINER）、SQLite integrity/WAL；移除來源 DB 後仍可匯入，FK、ownership、app 非 superuser、重跑不重灌均已檢查。四組 DB matrix 約76秒（images cached）；n8n2.28.0 正／錯金鑰與 active workflow 不執行約29秒。這些數字只描述小型假資料測試，不能據此承諾正式 RTO。

**尚待驗收：空白 Fedora43 x86_64 VM、SELinux enforcing、不同 UID／儲存掛載路徑、重開機後完整 Ansible 還原與正式啟動。** 本機容器測試不等同此驗收；尚未在新機部署本功能，也未對正式 B2 repository 做復原寫入。照片／媒體備份及完整性驗證另行處理。
