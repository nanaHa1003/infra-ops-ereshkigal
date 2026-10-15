# Cross-host Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在舊主機無法存取時，使用獨立 `recover.yml` 將備份的 DB／設定復原到替代主機，驗證後再由獨立操作正式接管。

**Architecture:** Ansible 負責目標主機預檢、bootstrap、共用 role 的 prepare/start 及接管順序；小型 Python 模組負責來源快照篩選、manifest、檔案映射、匯入、驗證及 run state。既有備份 CLI 保持相容，來源 hostname/path 與目的地設定完全分離。資料層驗證不啟動正式應用或對外排程。

**Tech Stack:** Ansible、community.docker、Python standard library、PyYAML（僅 legacy Compose parser）、restic、Docker Compose v2、Fedora 43 x86_64／systemd／SELinux enforcing VM。資料庫矩陣為 PG16、PG18、Immich PG14 extensions、MariaDB、SQLite。

**Spec:** `docs/superpowers/specs/2026-10-15-cross-host-recovery-design.md`（使用者已確認）。本計畫待審閱及選擇執行方式；尚未開始實作。

## Global Constraints

- `recover.yml` 不 import `site.yml`；`site.yml` 不新增 recovery task。正式 inventory／主機不因開發或演練改動。
- 第一版以 Fedora、systemd、Docker Compose v2、SELinux enforcing 為實測範圍，先驗證 x86_64。先列 Fedora 43；增加其他版本需先新增通過的矩陣。
- 照片、影片、附件、下載資料維持排除；沒有媒體不能宣稱完整服務資料已恢復。OS／磁碟佈建、改網域、版本升級及非空正式資料覆蓋不在範圍。
- 使用 Git repo、匹配的歷史 Vault、B2 憑證及 restic password；不要求舊機在線或保存備份 config。真實 Vault 不改寫，不把 `.vault_pass` 帶入測試 checkout。
- source host/job/staging path 必填；只選 completed tag，`latest` 解析後固定完整 snapshot ID。未知 manifest version 不自動猜測。
- root run directory `0700`、config/state `0600`；secret 任務 `no_log: true`、`diff: false`；argv、stdout、journal、報告不含秘密／SQL 內容。
- restore/verify 不啟動 DDNS、backup timers、Caddy、socket proxies、正式應用 worker／workflow／下載器。驗證 DB 無公開 port／host network／Docker socket，使用 internal network。
- 目的地只接受不存在／空白或相同 run 擁有的完整結果；失敗匯入不自動重灌。清理需明確 run ID 及清單，不提供通用 force。
- 恢復原版本後才另行升級；PG18 mount `/var/lib/postgresql` 與 PG14／16 的 `/var/lib/postgresql/data` 不混用。
- 保留既有 `backup_runner.py` CLI、completed/pending tags、鎖、排程及保留策略。禁止 recovery 呼叫 init/tag/forget/prune。
- activate 需同一份成功驗證報告、舊機已停止／隔離及明確啟動清單；DNS／路由切換人工執行，新機備份設定及 Healthchecks 獨立配置。

## Review Focus

1. **來源／目標混用**：兩台 host、兩個 staging/storage root 時選對舊快照；新備份到達不改變已選 ID。Task 3／9。
2. **版本與 secrets 漂移**：mutable tag、缺 digest、歷史 Vault mismatch、錯 n8n key 不可自動套用最新值。Task 2／4／7。
3. **路徑與既有資料**：missing mount、symlink／重疊、PG18 mount layout、半套 DB 及別的 run 不可覆蓋。Task 4／5／6。
4. **驗證期副作用**：Compose 合併遺留 ports／networks、Docker socket、工作排程或 plaintext credential 不可從測試容器洩出。Task 5／7／9。
5. **中斷與接管**：run/profile 改變、匯入中斷、兩個 DB 同時掛同一 volume、activate 部分失敗不可變成自動重灌／回切。Task 5／8／9。

---

## Execution Environment and File Boundaries

執行前使用 using-git-worktrees 技能建立 recovery feature checkout，保存目前兩份規劃文件；不得移除既有工作、覆蓋 Vault 或直接在 production 執行。測試用新 checkout 注入 `vault.ci.yml` 至測試 snapshot，保留真實 repo 的 encrypted Vault 與 `.vault_pass` 原樣。

以下 `python`／Ansible 命令須在已有 ansible、ansible-lint、PyYAML、Molecule 的環境執行，PATH 有 restic 及 Docker Compose。macOS `ANSIBLE_LOCAL_TEMP` 指向允許寫入的 temp；測試 tempfile 先 resolve 為 canonical path，避免 `/var`→`/private/var` 被誤判 symlink。

共用程式放 `roles/backup/files/backup_common.py`，兩個 role 分別安裝到自身 program dir，不要求執行整個 backup role。recovery 用 `python3-pyyaml` 作為 Fedora 套件；原 backup runner 維持 stdlib-only。所有程序走 argv list，秘密經 protected config／stdin／env，錯誤只轉固定類別。

核心分工：

| 檔案 | 責任 |
|---|---|
| `roles/backup/files/backup_common.py` | 共用 BackupError、ResticError、checked_path、safe_id、restic transport |
| `roles/backup/files/backup_manifest.py` | v2 producer、artifact hashes、映像與 DB metadata |
| `roles/recovery/files/recovery_models.py` | 驗證內部資料契約，無 I/O |
| `roles/recovery/files/recovery_manifest.py` | snapshot scope、tree layout、v1/v2 normalization、相容性檢查 |
| `roles/recovery/files/recovery_files.py` | 服務 registry、檔案映射、安全複製與目的地驗證 |
| `roles/recovery/files/recovery_state.py` | 原子 state、主機鎖、run ownership、清理清單 |
| `roles/recovery/files/recovery_databases.py` | PG/MariaDB/SQLite 匯入及 DB-only 容器生命週期 |
| `roles/recovery/files/recovery_verify.py` | 代表性資料、設定與 n8n 解密驗證 |
| `roles/recovery/files/recovery_runner.py` | JSON config CLI 與階段協調，不包入各 DB 細節 |
| `roles/recovery/tasks/*.yml` | 安裝、預檢、prepare、restore/verify、activate 與權限／SELinux |

### Internal contracts（所有 task 使用同一組命名）

以 validated JSON-compatible dict 表示，對應 constructors 在 `recovery_models.py`；禁止把 manifest 原始 dict 直接作為 subprocess／目的地設定。

- `SourceScope`: `repository`, `host`, `job`, `staging_path`, `snapshot`；snapshot 最初為 latest 或十六進位 ID，selection 結果永遠是 64 字元 ID。
- `Selection`: `snapshot_id`, `host`, `job`, `staging_path`, `snapshot_time`, `tree_root`, `manifest_path`；tree_root 由 restic ls 唯一定位，不以 snapshot.paths 推定。
- `RecoveryModel`: `schema_version`, `selection`, `services`, `sources`, `images`, `deployment`, `limitations`；不含任意 target paths。
- `Artifact`: `source_id`, `kind`, `entry_type`（file/directory）, `relative_path`, `size`, `sha256`；directory 的 size=0、sha256=null，必須保留空目錄。legacy file 允許 hash 不存在但需 restic verification 並記錄 hash baseline unavailable。
- `Mapping`: `source_id`, `artifact`, `target`, `owner`, `group`, `mode`, `service`；target 只由 registry＋target profile 建立。
- `RunState`: `run_id`, `target_machine_id`, `snapshot_id`, `profile_sha256`, `phase`, `services`, `owned_paths`, `owned_containers`, `results`, `activation`；activation 記錄 services_started、cutover_confirmed、ddns_enabled。state 不含 secret 或 secret hash。profile hash 只涵蓋公開設定與 Vault 版本參照。
- `VerificationResult`: `service`, `status`（passed/failed/not_applicable/blocked）, `checks`, `counts`, `limitations`；`checks` 只含非敏感名稱及結果。state 的 imported/verified 是每服務進度，不混用 status 值。

## Task 1: Split preparation from start while preserving normal deployment

**Files:** Modify `roles/common/tasks/main.yml`、9 個 active role 的 `tasks/main.yml`；create `roles/common/tasks/{bootstrap,storage,ddns}.yml`、各 active role 的 `tasks/{prepare,start}.yml`；modify `tests/test_lifecycle.py`、`molecule/default/verify.yml`。

**Interfaces:** 日常 main 保持相同順序、tags、pull/recreate 行為。prepare 只建立目錄／render／複製基礎檔；start 負責 Compose reconcile。common bootstrap 接受 `common_remove_conflicts`，日常維持既有行為，recovery 固定 false 並在衝突時停止；ddns 只能明確呼叫。proxy prepare 可準備網路，start 分清 Caddy／socket services。

- [x] **1. Write failing regression tests**：`test_prepare_never_starts_containers_or_schedules_ddns`、`test_normal_main_preserves_prepare_then_start_and_tags`、`test_recovery_bootstrap_rejects_conflicting_podman_without_removal`。既有 lifecycle helper 改為展開本 role 的 static import_tasks，仍對實際 leaf task 斷言，不能把新 task 排除而讓測試空過。
- [x] **2. Run RED**：`python -m unittest discover -s tests -p test_lifecycle.py -v`；應因 prepare/start 尚不存在而失敗。
- [x] **3. Implement task split**：用 static import_tasks 保留 main contract；Mealie repo prompts 必須先 prepare，還原檔案後不得再次 prepare 覆蓋。不改 templates 的版本／密鑰／port defaults；site/down 不加入 recovery。
- [x] **4. Verify GREEN**：同一測試、全套 `python -m unittest discover -s tests -v`、dummy-Vault checkout 的 `ansible-lint` 及 `molecule test`。第二次 converge changed=0；正常 role tags 仍可獨立執行。
- [x] **5. Commit**：`refactor: separate homelab preparation from service startup`；只 stage 本 task 檔案。

## Task 2: Publish versioned backup metadata and share transport

**Files:** Create `roles/backup/files/{backup_common,backup_manifest}.py`；modify `backup_runner.py`、`backup_sources.py`、`roles/backup/tasks/main.yml`、`templates/config.json.j2`；create `tests/test_backup_manifest.py`；modify existing backup tests。

**Interfaces:** `backup_common.restic(repo, args, *, cwd=None, timeout=3600, capture=False) -> bytes` 等同現有行為；原模組保留 import/re-export 以相容呼叫者及 monkeypatch boundary。`backup_manifest.build_manifest(stage: Path, config: dict, exports: dict) -> dict` 產生 schema_version=2，保留原 sources metadata。config 新增 `deployment`（revision/dirty/vault_revision）及 job 的 `service_projects`（service→Compose project 名稱 map）；Ansible vars 對應 `backup_deployment_metadata` 及 job.service_projects。`capture_image_metadata(container: str) -> dict` 只 inspect Image/RepoDigests/architecture，絕不輸出 Config.Env。依所選 project labels 找出 container，不掃描與此 job 無關的容器；producer 不依賴 PyYAML。

- [x] **1. Write failing tests**：`test_v2_manifest_records_artifacts_and_omits_secrets`、`test_manifest_tracks_all_service_images_not_only_database`、`test_missing_registry_digest_is_reported_not_replaced_by_latest`、`test_backup_does_not_require_server_git_checkout`。fixtures 為假 PG metadata、假 app Compose 與普通設定檔；stage 中 artifacts 的實際 hashes 必須與 manifest 一致。
- [x] **2. Run RED**：`python -m unittest discover -s tests -p test_backup_manifest.py -v`。
- [x] **3. Implement producer and transport extraction**：manifest 寫出前收集 stage artifacts，manifest 本身不計入自身 hash。部署 revision／dirty／Vault revision 由 Ansible 控制端提供，不能用伺服器 git。PG/MariaDB 記錄 engine/version/roles/extensions、可取得的大小與一致性限制；查詢代表性 counts 綁定版本化服務 registry，無法取得時明記 unavailable，不編造快照時精確筆數。SQLite count 應從匯出 DB 查詢；其他獨立查詢記錄時間，不能假設與 dump 同一交易。
- [x] **4. Verify GREEN**：全套 backup tests、真實 restic round trip、`molecule test -s backup`。原 init/ensure-init、鎖、pending→completed、失敗清理與 Healthchecks 行為不變；新腳本一併以 root0700 安裝。
- [x] **5. Commit**：`feat: record versioned recovery metadata in backups`。本 task 完成不代表可部署到正式主機；部署另列驗收。

## Task 3: Select cross-host snapshots and normalize legacy/v2 manifests

**Files:** Create `roles/recovery/files/{recovery_models,recovery_manifest}.py`、`tests/test_recovery_manifest.py`、`tests/test_recovery_restic.py`、`tests/fixtures/recovery/`（只含假資料）。

**Interfaces:** `select_snapshot(repo: dict, scope: dict) -> Selection`；`normalize_manifest(raw: dict, selection: dict, legacy_profile: dict) -> RecoveryModel`；`validate_compatibility(model: dict, profile: dict) -> list[dict]`。阻擋項有固定 code＋service，不含原始內容。image overrides 只允許選定服務的已審閱版本／digest，不從 target defaults 自動推斷。

- [ ] **1. Write failing tests**：`test_old_host_selected_on_different_target`、`test_explicit_id_cannot_bypass_completed_scope`、`test_latest_is_pinned_despite_newer_snapshot`、`test_tree_root_is_not_assumed_from_snapshot_paths`、`test_two_manifests_are_rejected`、`test_v1_is_normalized_without_executing_compose`、`test_unknown_version_and_incomplete_legacy_profile_block_service`。v1 fixture 必須符合目前 sources 與 rendered Compose 格式，含 mutable image tag 缺口。
- [ ] **2. Run RED**：`python -m unittest discover -s tests -p test_recovery_manifest.py -v`。
- [ ] **3. Implement contracts and parser**：validate IDs/keys/types，safe_load 白名單 image/environment scalar；不執行快照 Compose。明確分開 restic selection path 與 tree_root；artifact 相對路徑不接受 absolute/..。來源操作白名單限 snapshots/ls/dump/restore，保留必要 restic lock，不允許 init/forget/prune/tag。
- [ ] **4. Verify GREEN**：manifest tests 與真實 restic test；建立包含另一 host/job/pending 的本機 repo，停用來源資料後仍可固定 ID 還原；assert 舊 host/舊 path 與新 target 不同。來源 snapshot 集合前後不變。
- [ ] **5. Commit**：`feat: resolve cross-host recovery snapshots and legacy manifests`。

## Task 4: Map artifacts to empty target paths and validate secrets

**Files:** Create `roles/recovery/files/recovery_files.py`、`roles/recovery/vars/services.yml`、`tests/test_recovery_files.py`、`tests/test_recovery_secrets.py`。

**Interfaces:** `build_mappings(model: dict, target: dict, registry: dict) -> list[Mapping]`；`validate_targets(mappings: list, state: dict | None) -> None`；`install_artifacts(mappings: list, scratch: Path, state: dict) -> list[str]` 回傳新建 owned paths；`compare_recovery_secrets(model: dict, scratch: Path, secrets: dict) -> list[dict]` 只回傳欄位名與 matched boolean。

- [ ] **1. Write failing tests**：`test_new_storage_root_replaces_old_absolute_paths`、`test_missing_mount_or_nonempty_target_blocks_install`、`test_symlink_traversal_overlap_and_scratch_recursion_rejected`、`test_same_run_owned_paths_allowed_but_other_run_rejected`、`test_historical_vault_mismatch_reports_no_values`、`test_wordpress_core_precedes_restored_custom_files`、`test_mealie_prompts_not_overwritten_after_restore`、`test_empty_required_directory_is_preserved`。
- [ ] **2. Run RED**：分別執行 `python -m unittest discover -s tests -p test_recovery_files.py -v` 及 `test_recovery_secrets.py`。
- [ ] **3. Implement registry and file install**：照 spec §7 列完全部來源 ID。file 與 directory artifacts 分開處理，必需／optional_absent 來自 registry；未知 source 只留 scratch。測試 registry 目的地與現有 storage_permissions／Compose volume 定義相容，避免另維護一份會漂移的硬編碼路徑。Compose/Caddy/DDNS 不整份照搬；比對白名單 secrets 後以 target templates render。n8n key 取同一快照檔案。WordPress core 從匹配 image 的 `/usr/src/wordpress` 取到 run-owned scratch，再 overlay 自訂內容；不執行 WordPress entrypoint／下載最新 core。
- [ ] **4. Verify GREEN**：檔案／secret 測試均通過，錯誤訊息、captured subprocess argv、report 不含 fixture secrets。以兩個不同 UID/GID profiles 檢查只處理選定服務路徑；SELinux 行為留 Task 9 真實 VM 驗證。
- [ ] **5. Commit**：`feat: map recovery artifacts and validate historical secrets`。

## Task 5: Add standalone Ansible entry, preflight and resumable run state

**Files:** Create `recover.yml`、`inventory/recovery.ini.example`、`inventory/recovery-profile.yml.example`、`roles/recovery/defaults/main.yml`、`tasks/{main,plan,install,restore,verify,cleanup}.yml`、`templates/config.json.j2`、`files/{recovery_state,recovery_runner}.py`、`tests/{test_recovery_state,test_recovery_ansible}.py`；modify `.gitignore`。

**Interfaces:** CLI `recovery_runner.py --config <root0600.json> {plan,restore,verify,cleanup-preview,cleanup} --run <id>`；plan 在 controller 尚無 config 檔時另支援 `--config-stdin`，秘密只經 stdin。Ansible `recover_plan_file` 指定控制端的公開計畫 JSON；plan 預設產生 `.recovery/<run_id>.plan.json` 並輸出該路徑，後續階段必須指定此檔，從中讀取唯一 run_id／完整 snapshot ID。`open_run(plan: dict, state_root: Path) -> RunState`、`advance_service(state: dict, service: str, phase: str, result: dict) -> None`、`cleanup_preview(state: dict) -> list[dict]`。activate 在 Task 8 加入，不先留可無檢查通過的 stub。

- [ ] **1. Write failing tests**：`test_only_one_explicit_recovery_target_allowed`、`test_recover_never_imports_site_or_backup_main`、`test_check_mode_does_not_import_or_activate`、`test_plan_without_remote_restic_uses_controller_or_reports_incomplete`、`test_profile_snapshot_or_machine_change_rejects_resume`、`test_atomic_state_survives_interruption`、`test_cleanup_refuses_unknown_paths_or_active_production_containers`。
- [ ] **2. Run RED**：`python -m unittest discover -s tests -p test_recovery_state.py -v` 及 `test_recovery_ansible.py`。
- [ ] **3. Implement entry and state**：僅 `[recovery_targets]`、必填 limit/phase/services、預設 phase=plan。profile 使用獨立 repository map＋Vault 備份憑證，不繼承舊 host_vars；同網域及目標最小空間／mount 必填。控制端保存不含秘密的 plan JSON 及完整 ID（`.recovery/` gitignored），restore 必須引用該計畫；target root-only run state 含 `/etc/machine-id` identity。寫 state 使用同目錄暫存＋fsync＋atomic replace，持有整段主機鎖。cleanup 只能指定同 run，拒絕 activated state。
- [ ] **4. Implement Ansible phase wiring**：plan 只做唯讀 facts／快照查詢，不安裝依賴；必要 sudo 明確交互輸入。restore 安裝 Python／PyYAML／restic、共用程式與 task1 bootstrap，下載驗證後才 prepare/install。掛載檢查使用實際 mount facts，不能只測 directory exists。render 所選版本的 target Compose，但不 start 應用。restore 自動呼叫 Task 6/7 adapters；在兩者完成前 CLI 對 restore 回傳 unavailable，不宣稱成功。
- [ ] **5. Verify GREEN**：state/Ansible 測試、dummy-Vault 的 recover syntax及 lint。測試缺掛載、空間不足、架構不支援、Podman 衝突、第二個 lock；全部在正式資料寫入前失敗。plan 的 unknown 不能被當作 passed。
- [ ] **6. Commit**：`feat: add isolated recovery preflight and run state`。

## Task 6: Restore databases into new persistent directories

**Files:** Create `roles/recovery/files/recovery_databases.py`、`templates/databases.yml.j2`、`tests/test_recovery_databases.py`、`tests/test_recovery_database_integration.py`、`tests/recovery_db_harness.py`。

**Interfaces:** `restore_database(service: str, model: dict, target: dict, state: dict) -> VerificationResult`；`stop_recovery_databases(state: dict) -> None`；owned containers 帶 run/service labels。DB-only template 只允許 internal network、target-owned DB volume，無 public ports／host network／socket。integration harness 以 `HOMELAB_RECOVERY_INTEGRATION=1` 明確啟用；CI 必跑，未設旗標的 local skip 要列入報告。

- [ ] **1. Write failing tests**：PG角色與 ownership、PG18 mount、Immich vector/vectors/vchord 真實 extensions／索引、MariaDB routines/events/triggers/DEFINER、SQLite WAL 殘留、SQL error、empty dump、timeout；`test_interrupted_import_does_not_reimport_automatically` 與 `test_database_volume_has_only_one_running_owner`。
- [ ] **2. Run RED**：`python -m unittest discover -s tests -p test_recovery_databases.py -v`；Docker harness 啟用後執行 integration 檔，預期尚未實作 adapter 而失敗，不以缺 Docker 的 skip 當 RED。
- [ ] **3. Implement PG/MariaDB/SQLite adapters**：PG 建正確 app role／DB後用 matching pg_restore `--exit-on-error --no-owner --no-acl`；Immich image/preload 按 snapshot extensions，缺庫直接拒絕。MariaDB bootstrap account 與 app grants 分開，DEFINER 只接受 profile 確認的白名單；client error 不忽略。SQLite integrity 通過後安裝至停用 app 的目錄。每次只標記完整成功的匯入；錯誤只記固定類別，不印 raw stderr。
- [ ] **4. Verify GREEN**：假資料在 PG16、PG18、Immich PG14、MariaDB 及 SQLite 完整 round trip，檢查 FK／索引／代表性內容及非 superuser app 權限。MariaDB／WordPress tag 先解析 fixture 版本及 digest，不測「目前 latest」。輸出無 secrets，容器停止後 DB 目錄仍存在，stop 不刪 persistent data。
- [ ] **5. Commit**：`feat: restore PostgreSQL MariaDB and SQLite recovery data`。

## Task 7: Verify application data and n8n key without running workloads

**Files:** Create `roles/recovery/files/recovery_verify.py`、`tests/test_recovery_verify.py`、`tests/test_recovery_n8n_integration.py`；modify runner verify/restore wiring、service registry。

**Interfaces:** `verify_service(service: str, model: dict, target: dict, state: dict) -> VerificationResult`；`verify_n8n_credentials(model: dict, state: dict) -> VerificationResult`；`write_report(state: dict, results: list[dict], destination: Path) -> None`。report 對每服務區分 data_restored、checks、limitations、activation_blockers；未知 baseline 不判定全部內容一致。

- [ ] **1. Write failing tests**：`test_authentik_and_n8n_counts_use_snapshot_baseline`、`test_changed_live_counts_do_not_invalidate_snapshot`、`test_n8n_wrong_key_fails_without_plaintext_output`、`test_zero_credentials_is_not_applicable`、`test_missing_media_blocks_immich_and_aria2_start`、`test_caddy_validation_does_not_start_tls_or_ddns`。
- [ ] **2. Run RED**：verify unit tests；啟用 DB integration 旗標執行 n8n fixture，正確 key／錯 key 兩種 case 都有行為斷言。
- [ ] **3. Implement versioned service checks**：registry 明列每個 image version 的表名／檢查 SQL；不硬套不同版本 schema。Auth users/apps/providers/flows；n8n workflows/credentials；Immich assets/users/albums及 extension；WP posts/options/prefix；Mealie recipes/users；Pinchflat sources/profiles；其餘 YAML/config/permissions。沒有版本化檢查支援時回 blocked 或明確 limited，不以 container running 冒充。
- [ ] **4. Implement isolated n8n CLI check**：匹配 image 以 CLI command 連到還原 DB，不啟動 n8n server。必要解密 export 只寫 container tmpfs，stdout/stderr capture 並丟棄敏感內容，驗證 exit code／輸出數量與資料格式後清除。fixture 含 synthetic active workflow，assert 假 webhook/SMTP 接收器零請求；無 Docker socket，容器移除時 tmpfs 消失。
- [ ] **5. Verify GREEN**：unit/integration tests 與 restore 自動驗證串接；verify 可重跑，不重新匯入 DB、不更新來源快照、不 ping Healthchecks。所有完整性／數量／解密結果列明範圍；錯 key 絕不標 verified。
- [ ] **6. Commit**：`feat: verify recovered data and n8n encryption keys`。

## Task 8: Add explicit activation and handoff controls

**Files:** Create `roles/recovery/tasks/activate.yml`、`tests/test_recovery_activate.py`；modify runner CLI、state、Ansible main；create `docs/recovery.md` 的接管／回退部分。

**Interfaces:** `build_activation_plan(state: dict, services: list[str], old_host_fenced: bool) -> dict`；Ansible vars `recover_activate_services` 必填且是已還原服務的子集，`recover_old_host_fenced: true` 必填，`recover_fencing_reference` 為人工紀錄文字，不含秘密。`recover_cutover_complete: false`、`recover_enable_ddns: false` 預設關閉；DNS 人工切換後可用相同 activate 入口再次核對及完成 handoff。activate 使用相同 run ID/profile，沒有 init／upgrade。media readiness 為特定服務外部路徑檢查，不以通用 ignore flag 跳過。

- [ ] **1. Write failing tests**：`test_activate_requires_matching_verified_run_and_fencing`、`test_missing_dependencies_or_media_block_requested_service`、`test_recovery_db_stops_before_production_db_starts`、`test_activation_stops_after_first_failed_ready_check`、`test_no_automatic_dns_rollback_or_old_host_restart`、`test_new_backup_and_healthchecks_not_inherited_from_old_host`。
- [ ] **2. Run RED**：`python -m unittest discover -s tests -p test_recovery_activate.py -v`。
- [ ] **3. Implement activation**：檢查 recovery DB 已停止、依賴 closure 完整，按 spec 順序調用 Task1 start tasks。用 include_role 的 public role interface 傳相符 image／路徑及 recreate 設定，不偷跑整個 main／Mealie prompts prepare。DB ready check 與 app ready check分開；proxy 是最後公開入口，socket proxies 只在 fencing 後啟動。已 activated run 的重跑只檢查／reconcile，不灌資料。
- [ ] **4. Implement handoff report**：明列人工 DNS/路由切換與登入檢查、新 inventory、新 B2 prefix/key及獨立 Healthchecks Check。應用啟動後尚未確認切換時，results 顯示 awaiting_cutover，run phase 保持 verified，不標 activated。收到 recover_cutover_complete=true 後重新檢查 readiness，才記錄 activated；recover_enable_ddns=true 同時要求 cutover_complete=true，透過 common 的 ddns task 安裝新機排程。完整 backup.yml 仍由操作者在新 inventory／新憑證及新 Check 配好後明確執行。工具不替控制端改 inventory，不假裝 DNS已切換。
- [ ] **5. Verify GREEN**：模擬停止／ready check 邊界及真實 fixture接管。被擋媒體服務保持停止，其餘明選服務可繼續；報告保留部分成功。新機已接受寫入後無自動回切路徑。
- [ ] **6. Commit**：`feat: gate recovery activation and production handoff`。

## Task 9: Exercise replacement-host recovery and document acceptance

**Files:** Create `molecule/recovery/{molecule,prepare,converge,verify,cleanup}.yml`、`tests/recovery_fixtures.py`、`tests/recovery_vm/{README.md,inventory.ini.example,verify.yml}`；modify `.github/workflows/ci.yml`、`docs/recovery.md`、README。

**Interfaces:** Docker integration CI job 執行 `HOMELAB_RECOVERY_INTEGRATION=1 python -m unittest discover -s tests -p 'test_recovery_*integration.py' -v`，必須 assert 期望 case 數且無 dependency skip。Molecule recovery 驗證 Ansible包裝／隔離；Fedora VM 另驗證 SELinux enforcing，兩者不互相冒充。

- [ ] **1. Write failing end-to-end fixture**：source-host＋target-host 使用不同 names、帳號與 storage paths，來源備份含假 Authentik/n8n/Immich/WP/SQLite/config資料、假網域與假外部 endpoint。匯出後停止並移除來源 containers／原資料存取，剩 local restic repo＋fake Vault，完成 plan→restore→verify。assert legacy v1 與 v2 各一輪。
- [ ] **2. Run RED**：`molecule test -s recovery` 及 DB integration 矩陣；首次失敗原因必須是未完成的復原行為，不能是預期依賴缺失。
- [ ] **3. Complete scenario and CI**：所有網路／ports/socket restrictions 檢查實際 Docker inspect 的選定非秘密欄位；假 SMTP/webhook/DNS server 的計數保持零。測試 restore 中斷、missing mount、低空間、profile mismatch、cleanup ownership、activate失敗。既有 CI dummy Vault步驟只在 disposable checkout執行；不輸出真實 Vault／原始 dump。
- [ ] **4. Run Fedora 43 x86_64 VM rehearsal**：外部準備空白 VM及專用已掛載測試磁碟，enforcing 必須是實測值；完成不同目標路徑/UID與重啟後的 restore/verify/fixture activate。證明沒有來源 SSH、正式磁碟、production DNS或 B2寫入。無可用VM時明確標記 blocked acceptance，不自行使用正式機充當測試VM。
- [ ] **5. Run final checks**：全套 unittest、明確開啟且無 skip 的 recovery DB integration、production lint、site/down/backup/recover syntax、`molecule test`、`molecule test -s backup`、`molecule test -s recovery`、gitleaks、`git diff --check`。執行於測試 checkout；保留既有真實 B2／服務不受影響。
- [ ] **6. Write complete runbook and results**：提供 plan/profile/Vault 選取、兩次主要sudo操作、版本缺口、媒體限制、清理、最後快照、人工切換與回退界線。記錄下載/匯入/驗證耗時、磁碟高峰及尚未驗收項；只有量測後才提出 RTO。預期操作使用完整 `ansible-playbook recover.yml -i ... --limit ...`，範例秘密不放argv。
- [ ] **7. Commit**：`test: exercise replacement-host recovery and document handoff`；依 verification-before-completion 技能取得新證據後才宣稱完成，推送／正式部署依使用者授權範圍執行。

## Coverage and Handoff

| 規格 | 實作 task |
|---|---|
| §1–4 目標／scope／獨立入口與 profile | 1、3、5、9 |
| §5 plan/restore/verify/activate | 3–8 |
| §6 新舊快照與版本metadata | 2、3 |
| §7 路徑／權限／SELinux | 1、4、5、9 |
| §8 DB與應用驗證 | 6、7、9 |
| §9 秘密／版本／歷史Vault | 2–4、7 |
| §10 重跑／清理／回退／最後快照 | 5、6、8、9 |
| §11–12 分工／整合與最終演練 | 1–9 |

自我審閱：五項 Review Focus 均有負責 task／測試；核心 contracts 的命名與 Task Interfaces 一致；尚無程式、依賴安裝、真實主機動作或 production DR 保證。實作需要的 mapping/table-name fixtures 必須在各 task RED 階段建立，不從真實備份抽取使用者資料做公開 fixture。

建議採 **Native**：依序由目前 agent 實作，完成後由獨立 reviewer 審查整個分支。Tasks 的共享契約與角色拆分關聯密切，先完成端到端串接較容易維持一致；若使用者選擇每 task 獨立 agent／review，改用 subagent-driven-development。兩種方式都要先審閱本計畫，再開始實作。
