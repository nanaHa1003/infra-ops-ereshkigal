# 替代主機復原

此功能從固定 Restic 快照還原資料庫與設定到**另一台具備必要容器執行能力的 Linux 主機**，不需要來源主機 SSH。`recover.yml` 是獨立入口，`site.yml` 不包含任何復原操作。照片、影片、下載、WordPress uploads、Mealie 附件不在目前備份範圍。

要讓後續快照自動記錄 image digests、資料庫 preload／DEFINER 與網域，需另外由操作者部署新版 `backup.yml` 並確認新快照成功；目前開發未變更正式機的備份程式。既有舊快照使用明確 legacy profile，缺版本資訊時不能保證無人介入復原。

依使用者於2026-10-16確認，本次以 Container 測試作為驗收標準，不再以空白 Fedora VM 演練作為完成條件。已驗證範圍與未涵蓋的主機行為列於文件末尾。

## 事前準備

- Git checkout 與快照對應的 role/template 版本；控制端 Python/YAML、Ansible collections、Restic。
- 歷史 Ansible Vault、Vault 解密方式、Restic 密碼、來源 B2 限定 prefix 的憑證（讀取時也需允許 Restic 建立／移除 lock）。這些不能只放在故障伺服器裡。不要把秘密放進 `-e`、命令列、Git 或報告。
- 替代主機、sudo、已可使用的 Docker Engine（Linux containers）及 Compose v2、Restic、Python3/PyYAML、穩定的 machine-id、明確儲存掛載點與足夠暫存空間。復原不安裝或替換容器引擎、不移除 Podman/Buildah；主機 provisioning 是獨立作業。SELinux 啟用時需準備 semanage/restorecon 及 Ansible SELinux Python bindings，未啟用則跳過標籤設定。
- 來源 snapshot 的完整 image digest／Linux OS／目標 CPU 架構資料。無 digest 或架構不符時先解決版本缺口；不使用最新 image 猜測。
- 設定只存於本機、受 Git ignore 的 inventory/profile。從 `inventory/recovery.ini.example` 與 `inventory/recovery-profile.yml.example` 複製。替代機先保留在 `recovery_targets`，不要加入正式 `homelab` 群組。

`recover_source.host/job/staging_path` 描述**原始備份**，不改成新主機。`storage_root`、`stack_root`、`recover_storage_mount` 描述**新機**，可使用完全不同路徑。`domain` 必須沿用來源網域；改網域是另外的遷移作業。`recover_vault_revision` 記錄你選擇的歷史 Vault 版本。

舊版 manifest 必須用 `recover_legacy_profile` 補足 source kind、原檔案位置與有 service label 的 image digest。資料庫可明確補 `version/database/role/extensions/preload_libraries/definers/verification`，網域放在 `recover_legacy_profile.domain`。不要把現在線上的數量填成歷史快照 baseline。不知道的數量保持 unavailable；不知道的 image 不能略過。

## 規劃、還原與驗證

所有操作都必須指定單一 `--limit`。下列範例的 inventory/profile/plan 都是控制端檔案。第一次 `plan` 不變更替代主機的服務或資料；`--check` 一律只執行 plan。

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=plan --ask-become-pass
```

保留輸出的 `.recovery/<run_id>.plan.json`；它固定完整 snapshot ID、來源 scope、目標 machine-id、services、路徑、image digests 與 profile hash。後續操作在 profile 增加 `recover_plan_file: /absolute/path/.recovery/<run_id>.plan.json`。不要修改產生的 plan。改目標、服務或歷史 Vault 版本時重新規劃，不能拿不同 profile 接續既有 run。重新 plan 時移除 recover_plan_file 或另指定不存在的新檔，避免覆蓋舊計畫。

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=restore --ask-become-pass
```

Restore 安裝專用復原腳本（不安裝系統套件）、下載固定快照、驗證 artifacts／歷史 secrets、產生固定版本 Compose、還原檔案，再匯入資料庫並自動 verify。沒有啟動應用 server、workflow、排程、SMTP、DDNS、Healthchecks 或備份 timer。PG16/18、Immich PG14 與 MariaDB 只啟動 run-owned DB 容器，無 published ports、無 Docker socket、只有 internal network；結束或失敗時停止 DB，保留資料。SQLite 使用匯出的完整資料庫，拒絕殘留 WAL/SHM。

報告在新機 `/var/lib/homelab-recovery/runs/<run_id>/report.json`，root-only。n8n credential 明文只在 CLI 容器 tmpfs，輸出只有數量。PG/MariaDB 沒有歷史數量 baseline 時顯示有限驗證，並非與來源所有內容相同。版本不支援、解密失敗或缺媒體會有明確限制／啟動阻擋。

續跑 restore、verify 及 activate 都會重新檢查歷史 Compose/Vault 欄位；沿用同一個 Vault revision 字串不能略過實際比對。報告不保存秘密或秘密雜湊。

可重跑 verify，不會重匯入資料庫：

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=verify --ask-become-pass
```

資料庫匯入中斷會記 failed，不自動覆寫再匯入。先 preview 再用相符 run ID 清理，只限此 run 記錄的目錄、容器與 network，不能清正式服務或任意目錄。主機層 `/var/lib/homelab-recovery/ownership.json` 持久保留目錄認領，獨立於 profile 的 state_root；空目錄也不能由另一 run 認領。不要手動刪除這份 index。清理也會拒絕 foreign running container 掛載中的目錄。應用開始啟動後即禁止 cleanup。

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

**本次 Container 驗收已通過。** 未涵蓋空白 Fedora43 x86_64 主機安裝、SELinux enforcing、實體儲存掛載及整機重開機後的完整接管；這些是已知測試界線，不是本次完成條件。不同 UID／資料路徑已有容器測試。尚未在新機部署本功能，也未對正式 B2 repository 做復原寫入。照片／媒體備份及完整性驗證另行處理。

開發回歸另包含：145項 unittest／本機 Restic 測試通過（五項需 Docker 的測試在一般 suite 明確停用，另以強制 gate 全部實跑；需要 root 的動態 Ansible prepare 測試另在 Molecule 實跑通過，九個服務產生固定 digest Compose 且沒有 startup task）；v1/v2 在來源刪除後 plan→download→restore→verify 的 SQLite/config round trip；Molecule recovery 以 UID1007/GID1008 驗證實際 owner，第二次 converge 零變更。Molecule 僅驗證 component 包裝與資料層，不宣稱已跑完整 Fedora43 `recover.yml` 正式接管。

完整實作裁定、代價及 review 保留的驗收界線見 [recovery-decisions.md](recovery-decisions.md)。

獨立 review 提出的四項問題已補對應回歸：prepare 只呼叫 prepare 入口；持久 host-wide reservation 保護跨 run 空目錄與 foreign mount；WordPress 使用 FPM9000＋bootstrap/DB readiness；resume/activate 重新比對實際歷史 Vault 欄位。WordPress 固定6.8.3 FPM fixture 使用合成 bootstrap 檢查成功與提早退出，完整 WordPress 登入未納入本次 Container 驗收。五項真實 matrix 另直接使用正式 backup_sources 產生 dump 與 metadata，匯出後移除來源再還原；最後一輪約128秒（含 FPM fixture，images cached），不代表正式 RTO。

## 主機可攜性（2026-10-16修訂）

復原依能力檢查，不使用 Fedora／版本號白名單；distribution/release 只保留為 plan 的環境紀錄。執行時仍核對 machine-id、Linux 與目標 CPU 架構。只要前述能力與 POSIX 權限／Docker bind-mount 語意成立，Debian、Ubuntu 或 WSL2 不因名稱被拒絕；這不等於已在所有平台完成實機驗證。WSL2 優先使用 Linux 檔案系統；使用 Docker Desktop 時必須確認 daemon 可存取相同目標路徑，不能只憑 CLI 能連線判定相容。WSL2 沒有 systemd 時不要求 systemd；有運作中的 systemd 時，timer 查詢失敗仍會阻擋。備份排程與選用 DDNS 是另外的主機整合，不由這項可攜性修訂保證。跨架構的替代 image 對應尚未實作，來源 image 與目標架構仍須符合；同架構 ARM image 不再被 x86 白名單拒絕。

## RustFS full-service snapshots (manifest v3)

The source repository/job can be `rustfs_files` on Hecate instead of B2. Use the
matching source host, job and `/var/lib/homelab-backup/staging/rustfs_files` scope
in the explicit recovery profile. Keep credentials under the matching repository
ID in Vault. `recover.yml` stays separate from normal deployment.

v1/v2 database/configuration snapshots remain supported. v3 snapshots additionally
contain direct media roots at their original snapshot paths. The recovery model
maps these bytes into approved replacement storage roots; original source paths
never select replacement destinations. The manifest is selected at the scoped
staging path so an application's own `manifest.json` cannot be mistaken for it.

Staged DB/config artifacts retain explicit SHA-256 hashes. Direct content is
verified by Restic during scratch restore; installation requires a successful
`restore --verify` for the exact pinned snapshot. Full WordPress HTML replaces
narrower WordPress file mappings. Approved directory ancestors can coexist with
separately exported SQLite databases, but file collisions, traversal and symlink
escapes remain forbidden. Aria2 session state is optional for historical backups
and absent from newly produced jobs.

Data restoration and application acceptance remain separate. Media-reference
checks and explicit activation evidence are still required before starting an
application; having files in a v3 snapshot does not by itself prove Immich or
another service has a complete, coherent library.
