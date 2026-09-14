# Phase 3d — FTS5調査・検知修正報告

調査日: 2026-09-15 JST。base: `be877379`。対象: このworktree。runtime、稼働checkout、coreリポは変更していない。commit / push / 再起動 / 修復CLIは実行していない。

## 結論と受入判定

**確定したのは検査側の誤検知経路と修復状態の取り残し。数週間周期でFTS5を物理破損させる書込操作は確定していない。** 稼働DBを `immutable=1` で検査する既存実装は、書込中の一時Chroma DBに対してSQLite破損エラーを返した。同じDBは通常の読取で正常だった。検査をWAL対応の読取へ修正し、FTS MATCHを追加した。

| 受入 | 判定 | 結果 |
|---|---|---|
| D1 3体×3回の時系列 | 判定不能を含む | 下表・CSVに9件。8月workerログに時刻/animaがなく、隔離時刻と初回検知時刻を同一視できない。9回の独立したFTS5破損を立証した表ではない |
| D2 同一FTS5エラーの意図的再現 | NG（部分再現） | 3仮説を検証。immutable読取だけ `database disk image is malformed` とquick_check不整合を再現。本番の `malformed inverted index for FTS5 table ...` 文言は未再現 |
| D3 再現経路の予防 | 部分OK | WALを無視する検査を修正。WAL未checkpointの正常DBを誤検知しない回帰fixtureが緑。物理破損の書込予防は仮説・設計のみ |
| D4 夜間・1時間継続・1回修復・rollback | 未実装（停止条件b） | supervisorのkill/rollback欠落と対象範囲の衝突。安全設計を下記に記載 |
| D5 検知 | OK | FTSの読取プローブ実装、core healthcheckの実ファイルに対するdiff案と9ケース検証 |
| 指定pytest | OK（baseline除外） | 618 passed / 6 skipped / 12632 deselected。変更前から失敗した4件を個別除外 |
| git diff --check | OK | 対象コード・tests・memory docs・本報告のみ |

## D1 — 実測の区別と時系列

### 観測条件

- runtime JSONは `Path.read_text()` で直接読んだ。`repair_state.read_state()` は状態を書き換える副作用があるため、runtime調査では呼んでいない。runtimeのSQLiteを再検査する処理も実行していない。
- `vector-worker.log` と `.1` のJSONを `ts` で集計。観測範囲は2026-09-02 21:00:19〜09-15 04:00:07 JST。`.1.gz` はJSONが0件で、時刻のない旧形式。これを特定日の10分窓に配賦しない。
- `animaworks.log*` のJSON timestampの保持範囲は09-02 10:13:12〜09-15 05:14:59 JST。animaログは行頭のローカル時刻をJSTとして読む。ローテート名の日付と実際の行の日付は一致しない場合があるため、ファイル名で日を決めない。
- 下表の10分窓は `[基準時刻−10分, 基準時刻)`。隔離基準の場合、初回破損の直前10分を意味しない。記録なしは処理不在の証明ではない。
- kaedeのarchiveは08-17に2個、09-03に1個。senaは08-23 JSTと09-03。mioは08-23 JST・08-30 JST・09-02の3個。09-02のmioは修復成功であり、新しい破損検知として数えない。

| anima / 対象回 | 検知・基準時刻（JST） | 直前10分のvector-worker operation / collection | 同時刻のjob・状況 | 当日server再起動 | 根拠と限界 |
|---|---|---|---|---|---|
| kaede 08-17 | 検知不明。隔離12:10:13、12:13:56 | 判定不能（旧workerログは無時刻） | 不明 | 不明（serverログ保持外） | archive名。stateに残る直近signalは08-14 10:50:22の `delete_documents / kaede_knowledge / sqlite_malformed`。08-17の新規検知とは断定しない |
| kaede 09-03 | 検知不明。隔離13:53:16 | 記録0件（全anima） | 13:53:01全process停止、13:53:22〜CLI reindex | あり、10:25:53 / 10:59:52 / 13:56:22起動 | `animaworks.log.4:16988,17012,17626`、state.last_attempt_at。隔離を初回検知としない |
| kaede 09-11 | **13:50:43.600838 detect** | 記録0件（全anima） | ±1分にも対象job記録なし。02:31頃のkaede単体SIGKILLは別時刻 | server再起動記録なし（単体再起動とは区別） | state.updated_at / last_error。FTS5 malformed、reasonはmanual CLIのまま |
| sena 08-23（UTC08-22） | **08:00:07.569496 signal** | 窓は判定不能。検知時 `query / sena_knowledge`、08:00:08にも同signal | 不明 | 不明（保持外） | state.recent_signals、reason=`chroma_error_finding_id`。隔離08:00:17。FTS5だった証拠なし |
| sena 09-03 | 検知不明。隔離10:17:36 | senaは0件。他anima=kaede: query、get、update-metadata、create-collection、upsert（詳細下記） | 10:17:29全process停止、10:17:43〜sena CLI reindex | あり、上記09-03と同じ | `animaworks.log.4:2599,2621,4454`、state.last_attempt_at |
| sena 09-09 | **13:22:01.676215 detect** | 記録0件（全anima） | ±1分に対象job記録なし。11:14:54にserver起動済み | あり、11:14:54 | state.updated_at / last_error、`animaworks.log.2:25594,25647`。FTS5 malformed、reasonはmanual CLIのまま |
| mio 08-14 | 日のみ判明、検知時刻不明 | 判定不能 | server startup preflightでkaede/mio/noa/renを再構築 | あり（時刻不明） | core `handoffs/2026-08_archive.md` §28のlive runtime反映。個別のFTS5エラー証拠なし |
| mio 08-23（UTC08-22） | 検知不明。隔離03:11:04 | workerは判定不能。animaログで03:10:50〜51にprocedures 6 chunksをindex | 03:10:46 weekly consolidation開始、03:10:53停止要求、03:10:58 SIGTERM | 不明（mio停止は確認、server全体は保持外） | `logs/animas/mio/20260822.log:761,767,794`付近、archive名 |
| mio 08-30 | **02:16:00.236648 requested**、隔離02:16:11 | workerは判定不能。anima側02:12:15〜16にprocedures 6 chunksをindex | daily consolidation Phase Bが02:12:08開始、heartbeat cronも同時期 | 不明（server保持外） | `mio/20260829.log:575`付近、state.requested_at。02:17:26〜58 shared_common_knowledge upsertのhnsw_corruptionは隔離後で、087a8ab9の既知lifecycle不整合に対応 |

sena 09-03窓の全worker操作はすべてkaede: `list-collections / null ×17`; `query / kaede_knowledge ×2, shared_common_knowledge ×2, kaede_episodes ×24, kaede_facts ×3`; `get-by-metadata / kaede_knowledge, shared_common_knowledge, kaede_episodes 各1`; `get-by-ids / kaede_knowledge, shared_common_knowledge, kaede_episodes 各2`; `update-metadata / 同3collection 各2`; `create-collection / kaede_episodes ×1`; `upsert / kaede_episodes ×1`。これは別animaであり、sena DBとの同時writerの証拠にはならない。

現在state（直接読取）:

| anima | status/stage | updated_at UTC | last_success_at UTC | failures |
|---|---|---|---|---:|
| kaede | corrupt/detect | 09-11 04:50:43.600838 | 09-03 04:55:48.585897 | 0 |
| sena | corrupt/detect | 09-09 04:22:01.676215 | 09-03 01:22:29.920640 | 0 |
| mio | success/complete | 09-02 12:25:39.876941 | 09-02 12:25:39.876938 | 0 |

kaede/senaの `last_error` はともに `malformed inverted index for FTS5 table main.embedding_fulltext_search; collections`。`; collections` は `_run_quick_check()` の返却形式と一致し、Chromaのupsert例外だけでなく**状態読取時の検査結果**であることを支持する。最新MATCH成功の値（kaede106/sena108/ren100）はブリーフ記載の09-15 05:10観測を引用したもので、本レーンの再測定値ではない。

`last_daily_consolidation` は09-15 02:39:48 JSTのみを保持する。過去日のjob履歴としては使えない。09-08のserverログには04:00:00 daily RAG indexing開始〜04:00:23完了があり、9月の13時台detectを定時indexing直後とは言えない。月次forgettingとdetectの同時実行を示す記録は得られなかった。

ブリーフの「失敗810/60/24」はそのまま独立したFTS故障回数として使わない。UTC日で再集計すると09-09は `Vector operation failed` 268行、HTTP500 541行、ERROR 273行、09-11はERROR 5行、09-14はERROR 2行。例外trace/HTTP/operation記録を合算すると重複する。9月後半のERRORがFTS5由来かは、この集計では確定していない。

### CSV（データ同梱）

空欄のdetected_atは未確定。`basis_at`は隔離等の代理時刻で、検知時刻の推定値ではない。

```csv
anima,episode,detected_at_jst,basis_at_jst,basis,worker_prev_10m,jobs,server_restart,evidence
kaede,2026-08-17,,2026-08-17T12:10:13+09:00,archive,unknown_unstamped,unknown,unknown,archive/vectordb-corrupt-20260817_031013
kaede,2026-09-03,,2026-09-03T13:53:16+09:00,archive,no_records,manual_repair_reindex,yes,animaworks.log.4:16988/17012/17626
kaede,2026-09-11,2026-09-11T13:50:43.600838+09:00,,state_detect,no_records,no_job_records,no_restart_record,state/rag_repair.json.updated_at
sena,2026-08-23,2026-08-23T08:00:07.569496+09:00,,query_signal,unknown_unstamped,unknown,unknown,state/rag_repair.json.recent_signals
sena,2026-09-03,,2026-09-03T10:17:36+09:00,archive,sena_none_kaede_operations,manual_repair_reindex,yes,animaworks.log.4:2599/2621/4454
sena,2026-09-09,2026-09-09T13:22:01.676215+09:00,,state_detect,no_records,no_job_records,yes_11:14:54,state/rag_repair.json.updated_at
mio,2026-08-14,,,handoff_day_only,unknown_unstamped,startup_preflight,yes_time_unknown,core/handoffs/2026-08_archive.md_section28
mio,2026-08-23,,2026-08-23T03:11:04+09:00,archive,unknown_unstamped,weekly_consolidation_and_procedure_index,unknown,logs/animas/mio/20260822.log:761
mio,2026-08-30,2026-08-30T02:16:00.236648+09:00,,state_requested,unknown_unstamped,daily_consolidation_and_procedure_index_and_heartbeat,unknown,state/rag_repair.json.requested_at
```

## D2 / D3 — 一時DB実験と予防

環境: runtime venv CPython 3.12.11、ChromaDB 1.5.2、Python sqlite3 3.49.1。Chroma Rust内部のSQLiteバージョンまでは確認していない。

再実行可能fixture: `tests/diagnostics/phase3d_fts5_probe.py`。`TemporaryDirectory` に本物のChroma DBを作成し、64 documents × 80 upsert rounds、明示3次元embeddingを使用する。モデルdownloadやruntimeへのアクセスはしない。各writerはspawnした別process。時間・対象processを限定して終了する。

```sh
../animaworks/.venv/bin/python tests/diagnostics/phase3d_fts5_probe.py
```

| 仮説 | 操作 | 最終の観測 | 評価 |
|---|---|---|---|
| H1 別process upsert/delete | writer A upsert、writer B delete半数→upsert、並行mode=ro検査 | 719 samples、両writer exit0、検査errorなし、最終quick_check=ok/MATCH=64 | 再現不能。この有限試験で並行Chromaが常に安全と証明したわけではない |
| H2 immutable読取 | upsert writer稼働中にmode=ro&immutable=1でquick_check/MATCH | 563 samples、`database disk image is malformed`、freelist/page参照不整合。writer exit0、最終mode=roはok/MATCH=64 | 誤検知を再現。永続的なDB破損は再現していない |
| H3 checkpoint中断 | WAL checkpoint(TRUNCATE) loopの別processにSIGKILL、writerは継続 | 497 samples、writer exit0、checkpoint exit−9、最終ok/MATCH=64 | 再現不能。killがfsync内部に一致した保証なし。power-lossの再現でもない |

最初のharness実行はSQLite URI指定漏れとspawn Event寿命の不備で無効だったため修正し、証拠から除外した。修正後、例外だけでなくPRAGMAの非ok行も保存するよう採取を補正した上表を最終結果とする。スケジューリング依存なのでサンプル数・誤検知の頻度は再実行で変わる。

**同一FTS文言は3仮説とも未再現**として停止条件(a)を適用。writer経路変更、consolidation後の強制checkpoint/integrity_check等は実装しない。D5の検査を正しくするために `immutable=1` を外す修正は行った。

変更:

- `_connect_readonly`: URIを `Path.resolve().as_uri() + '?mode=ro'` に変更。`?/#/日本語`のpathも正しく扱う。
- `_run_quick_check`: `BEGIN`でquick_check/collections/FTSを同じ読取snapshotに固定。`closing()` で接続を確実に閉じ、読取snapshotを解放する。
- FTS tableが存在すれば `MATCH 'test'` のcountを1回。既存progress deadline内で実施。special INSERTによるFTS integrity-checkは行わない。
- `MATCH 'test'` は索引の一部分の検査で、全行の健全性保証ではない。SQLite版によってはquick_check自体もFTS異常を報告するため、既存検査が必ずFTSを素通りするという前提は採用しない。
- 0-byte、archive mtime、worker quiesce/move/resume、busy/timeout扱い、既存修復・consolidationの動作は変更しない。

回帰テストは、未checkpointのWALにcollectionsとFTSがcommitされている正常DBをfixture化し、別接続の未commit書込中も検査が正常になることを検証する（旧immutable読取ではcollectionsを見落とす）。FTS shadow segmentを**fixture内だけ**意図的に壊してcorruptになることも確認した。後者は検知テストであり、実際の破損原因の再現とは別。

未実装の予防設計: H1は既存worker-only guardを維持し、必要ならDBごとの所有processを観測する。H2は今回の検査修正。H3は異常終了の時刻・checkpoint進行を記録してからSQLite/Chromaの更新やdurability設定を検討する。通常の別接続書込や正常なWAL checkpointだけでSQLite破損が必然的に起きる、とは結論しない。

根拠: SQLite公式はimmutable指定時にロック/変更検知を省略し、内容が変わると誤った結果やSQLITE_CORRUPTを返し得るとしている（[URI filenames](https://www.sqlite.org/uri.html#uriimmutable)）。FTSのintegrity-checkはspecial INSERTであり読取プローブと同等ではない（[FTS5 integrity-check](https://www.sqlite.org/fts5.html#the_integrity_check_command)）。一般的な破損経路の区別には [How To Corrupt An SQLite Database](https://www.sqlite.org/howtocorrupt.html) も参照した。

## D4 — 自動修復が走らない理由と停止条件(b)

### 確認した既存コード

1. `repair_state.read_state()` はhealthy/successのときquick_checkし、異常ならstatus=corrupt/stage=detect/updated_at/last_errorだけを上書きする。`reason=manual_repair_rag_cli`、古いrecent_signalsは残る。修復requestは作成しない。
2. supervisor `_poll_requested_rag_repairs()` は **status=requestedだけ**を起動対象とする。corruptは取り残される。
3. `RAGRepairService._state_is_suspect()` も既知reasonの確認とstatus集合でフィルタする。その集合にcorruptがなく、manual reasonも既知集合にない。古いsignalはwindow外になる。stateと起動時quick_checkの両方で拾えなければ再起動しても復旧しない。
4. `sqlite_malformed` は既にSINGLE_SHOT_REASONSにある。裸のFTS malformedは一般の `chroma_corruption` に分類され、これもsingle shot。`error executing plan ... error finding id` が前段にある例外は先に `chroma_error_finding_id` と分類され、同collectionでthreshold（既定2回/5分）が必要。分類の違いはあるが、**今回のstate読取経路はrecord_chroma_error自体を通らない**のでsingle-shotを増やしても直らない。
5. cooldownは既定failures>=2かつlast_failureから60分以内に効く。観測したkaede/senaはfailures=0で、cooldownが今回の停止主因ではない。

### 安全に実装できない衝突点

- 定期poll入口は `core/supervisor/_mgr_rag_repair.py`（ブリーフの実装対象外）。`read_state()`に時刻判定と修復起動を混ぜると、CLI/healthcheck/更新ヘルパーのreadから処理が発火し、既存lock取得中の再入や多process競合を招く。
- `repair_service.repair_anima()` はquarantine→full_reindexの順で、失敗時はreset + failed記録のみ。旧DBを戻さない。`full_reindex()`はindex_meta/BM25も更新するためDBディレクトリだけ戻しても状態整合性を保証できない。
- supervisor `_run_rag_repair_cli_process()` はtimeout（既定1800秒）で **proc.kill()**。子processのexcept/finallyにrollbackを追加するだけではこの経路を救えない。外部workerで続く処理の停止・drainも必要。
- 現在のquarantine helperはmove後すぐworkerをresumeする。再index先がlive DBなので、失敗時rollbackの競合排除や06:00を越えた検索影響を、night-windowのif文だけでは保証できない。

以上から停止条件(b)に従い、**夜間自動修復は有効化せず、repair_service/state/supervisorは無変更**とした。

### 次レーン向け設計（未実装）

1. supervisor pollに専用maintenance入口を追加。JST `02:00 <= now < 06:00`、`corrupt_since`から1時間以上、enabled、非active、既存cooldown/lockを満たす場合だけ対象にする。legacy corruptはupdated_atを初回時刻として移行し、manual reasonを障害種別として使わず、元のrepair履歴として保持する。
2. `corrupt_since` / episode ID / `auto_repair_attempted_at`を永続化。同一episodeのattemptを**既存per-anima repair lock取得後**にclaimし、失敗・supervisor再起動でも二度目を予約しない。成功後の新しいcorruptionだけ新episodeにする。state更新は別の短いstate lockとatomic replaceで保護し、signal追加によるrequested/repairing上書きを防ぐ。
3. claim前のWAL-aware再検査は運用上有益だが、単発MATCH成功だけでcorrupt履歴を消さない。誤検知解除の方針と夜間修復の方針を明示する。
4. workerに一時build用path/namespaceを用意し、元memoryとindex_meta/BM25の整合したsnapshotから別ディレクトリへ構築・検証する。live検索を継続し、切替時だけquiesce→drain→DBと対応metadata切替→resume。06:00までに切替準備ができなければ中断・旧DBを保持する。
5. supervisor側にtransaction manifestを保存し、CLI例外/timeout/SIGKILL/server再起動からもworker-aware rollbackできるようにする。失敗DBを別名で保存、旧DBとmetadataを戻し、failed記録。archive mtime検査は保持し、rollback後のarchive path消費も明記する。
6. 親supervisorの主ログにsuccessを1行、failure/rollback failureをWARNINGで1行。時刻境界01:59/02:00/05:59/06:00、59/60分、再起動後の一度限り、2process claim、lock/cooldown、timeout中worker操作、swap失敗/restore失敗をfixtureで検証する。

## D5 — core healthcheck差分案

以下はcoreリポの実ファイルから生成した **未適用diff**。statusがsuccess/healthy以外ならWARN、継続24時間以上ならRED。最初は `corrupt_since`、なければ `updated_at` を採用し、既存cursorに連続異常の開始時刻を保持するのでheartbeat等がupdated_atを更新しても年齢をリセットしない。healthy/successを観測したときcursorを消す。missing/不正JSONもWARNとして観測開始する。

cursorを削除した場合や観測間に回復→再発した場合は継続期間を完全には復元できない。厳密なepisode追跡はD4のstate schemaが必要。初回に時刻のない異常は判定不能をWARNとし、観測開始から24時間でRED。

同じdiffでcore checkerにも残る `immutable=1` を外す。FTS MATCH追加は今回のAnimaWorksコードに実装済みで、coreのSQLite checkerに同様の検査を重ねる案はここでは含めていない。diffを一時コピーへ適用したPythonをimportし、healthy/success、23h WARN、status変更後24h RED、初回24h RED、不正時刻、不正JSON、status欠損/配列の計9ケースを検証済み。

```diff
--- a/scripts/check_animaworks_runtime_deltas.py
+++ b/scripts/check_animaworks_runtime_deltas.py
@@ -274,8 +274,57 @@
             result.add(1, f"{agent} RAG C absence proxy: {missing}/{samples} ({ratio:.1%})")
 
 
+def evaluate_repair_states(
+    animas_dir: Path,
+    now: datetime,
+    result: Result,
+    cursor: dict[str, dict[str, int]],
+    next_cursor: dict[str, dict[str, int]],
+) -> None:
+    """Track continuous unhealthy time across status/updated_at changes."""
+    for agent in AGENTS:
+        key = f"@rag_repair/{agent}"
+        path = animas_dir / agent / "state" / "rag_repair.json"
+        try:
+            state = json.loads(path.read_text(encoding="utf-8"))
+            if not isinstance(state, dict):
+                raise ValueError("state is not an object")
+        except (OSError, ValueError) as exc:
+            state = {"status": "unreadable", "last_error": str(exc)}
+        status = state.get("status")
+        if isinstance(status, str) and status in {"success", "healthy"}:
+            # Dropping the cursor entry starts a new interval after recovery.
+            continue
+        since = now
+        for field in ("corrupt_since", "updated_at"):
+            raw = state.get(field)
+            if isinstance(raw, str):
+                try:
+                    candidate = parse_time(raw)
+                except ValueError:
+                    continue
+                if candidate <= now:
+                    since = candidate
+                    break
+        previous = cursor.get(key, {}).get("since")
+        if isinstance(previous, int):
+            try:
+                since = min(since, datetime.fromtimestamp(previous, timezone.utc))
+            except (ValueError, OverflowError, OSError):
+                pass
+        next_cursor[key] = {"since": int(since.timestamp())}
+        age = now - since
+        severity = 2 if age >= timedelta(hours=24) else 1
+        result.add(
+            severity,
+            f"{agent} rag_repair status={status!r} stage={state.get('stage')!r} "
+            f"unhealthy_for={age.total_seconds() / 3600:.1f}h "
+            f"error={state.get('last_error')!r}",
+        )
+
+
 def sqlite_uri(path: Path) -> str:
-    return f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
+    return f"{path.resolve().as_uri()}?mode=ro"
 
 
 def evaluate_sqlite(animas_dir: Path, result: Result) -> None:
@@ -318,6 +367,7 @@
         )
         evaluate_logs(lines, now, args.window_minutes, result)
         evaluate_sqlite(args.animas_dir, result)
+        evaluate_repair_states(args.animas_dir, now, result, cursor, next_cursor)
         atomic_json(args.cursor, next_cursor)
     except (OSError, ValueError, sqlite3.Error) as exc:
         print(f"runtime_deltas: ERROR: {exc}", file=sys.stderr)
```

## 検証・変更ファイル・反映手順

変更前baseline:

```text
../animaworks/.venv/bin/python -m pytest tests -q -k "rag or repair or sqlite or consolidation or fts" --ignore=tests/e2e
4 failed, 614 passed, 6 skipped, 12628 deselected in 89.98s
```

baseline失敗4件はいずれも `VectorWorkerUnavailable: vector worker exited early: 1`（CLIが一時vector workerを起動する箇所）。OS/sandbox等のどの要因でworkerがexit1になったかまでは断定していない。今回の変更とは独立した既存失敗として個別除外した。

```sh
../animaworks/.venv/bin/python -m pytest tests -q \
  -k "rag or repair or sqlite or consolidation or fts" --ignore=tests/e2e \
  --deselect=tests/unit/cli/test_index_shared.py::test_index_command_skips_repair_locked_anima \
  --deselect=tests/unit/cli/test_repair_rag_cmd.py::test_repair_rag_success \
  --deselect=tests/unit/cli/test_repair_rag_cmd.py::test_repair_rag_failure_exits_one \
  --deselect=tests/unit/cli/test_repair_rag_cmd.py::test_repair_rag_suspect_only_runs_bulk_repair
# 618 passed, 6 skipped, 12632 deselected in 80.20s
```

対象テストは21 passed。追加4件はWAL未checkpoint/未commit writer共存、FTS読取のみ・接続close、FTS segment破損検知、URI特殊文字。指定の広い受入にはrepair/quarantine/archive検査・consolidation系も含まれ、baseline除外以外は緑。ruffはruntime venvにもPATHにもなく実行できなかった（追加インストールしていない）。`git diff --check` は緑。

変更ファイル:

- `core/memory/rag/sqlite_health.py` — WAL対応読取、同一snapshot、接続close、FTS MATCH。
- `tests/unit/core/memory/test_rag_sqlite_health.py` — 上記4回帰fixture。
- `tests/diagnostics/phase3d_fts5_probe.py` — 3仮説の一時Chroma DB実験。pytestの自動収集対象ではなく明示実行。
- `docs/memory.md` / `docs/memory.ja.md` — 検査の挙動・限界と状態保持を記載。
- `PHASE3D-report.md` — 本報告・CSV・core差分案。

委譲元が反映する手順（本レーンでは未実行）:

1. この差分をreview・commitし、稼働checkoutへ反映する。依存package追加・DB migrationは不要。workerとsupervisorの両方がsqlite_health/repair_state経由で旧コードを保持し得るため、運用の既存手順でserver/vector workerを再起動する。worker単体だけの再読込では不十分。
2. core healthcheckには上記diffを別途review・適用する。定期実行スクリプトなら次回実行から反映され、server再起動は不要。legacyのkaede/senaはupdated_atが24時間超のためREDとなる見込み。
3. 現在corruptのkaede/senaは、今回の反映だけではsuccessに戻らず、夜間修復も予約されない。状態JSONとDB/WALを運用手順で保全し、反映後のworker経由・record_repair=FalseのWAL-aware検査と検索を、書込負荷時を含めて確認する。単発MATCH成功を根拠に状態を手編集して消さない。
4. 継続して異常が出る場合は、停止時間とrestore手順を用意した上で既存のworker-aware repairを運用側で行う。本レーンは現在DBの再構築を実行していない。既存repair CLIにはrollback保証がないため、D4の自動実行は上記設計・supervisor範囲を含む別実装を先に完了する。
5. 次の再発では、検知器のsource、UTC時刻、WAL-aware検査結果、operation/anima/collection、worker PID/起動・終了理由を同じepisodeへ記録する。現在のworker失敗行の件数だけでFTS破損数やwriter競合を判断しない。

未完了はD1の欠損履歴、D2の同一FTS文言・物理破損の再現、D3のwriter予防、D4自動修復。本報告ではこれらを完了扱いにしない。ブラウザUI/Chrome laneは使用していない（SQLite公式文書の取得はweb検索ツールのみ）。

旧HEADの検査コードとの同一fixture比較も実施: WAL未checkpointの正常DBは旧コードで `corrupt (ok, missing collections table)`、修正コードで `ok (ok, collections)`。
