# 実行runner

`bench/run.py` は ready ケースを 1 回分 agent に解かせ、agent が触れない場所で採点して、実行記録（`run.json`）を残す。
ファイル操作の部品は `bench/workspace.py`、採点は [verify.md](verify.md) の `bench.verify` を使う。

## 流れ

1. 前提を確かめる。いずれかが満たされなければ exit 2 で終わり、run ディレクトリを作らない。
   - catalog の検査を通っていること。
   - ケースが `ready` であること。
   - `runtime.image` が `<name>@sha256:<64桁>` であること。
   - agent 定義が正しいこと。
   - bundle の SHA256 が catalog と一致すること。
   - 採点テストのディレクトリがあること。
   - run ID が未使用であること。
2. bundle を新しい `workspace/` に展開する。全 member を書き込み前に検証し、次のものを拒否する。
   - 絶対パス、`..`、top の外の member。
   - リンク、デバイス。
   - 同じパスの重複。
3. agent を子プロセスとして起動する。作業ディレクトリは `workspace/` にする。
   - 環境変数は許可リスト（`PATH`、`HOME` など）、agent 定義の `env`、`BENCH_*` だけを渡す。
   - 制限時間を超えたら process group に SIGTERM、猶予後に SIGKILL を送る。
4. agent の終了後に、次の 4 点を確かめる。
   - workspace の変更（追加・変更・削除）を洗い出す。
   - `editable_paths` の外の変更、シンボリックリンク、特殊ファイルがあれば `invalid_submission` とする。
   - `usage.json` の出力 tokens と tool 回数を `limits` と照合する。
   - bundle・採点テスト・catalog の hash を実行前と比べる。改変されていれば `invalid_submission` とする。
5. 採点用の `grading/` を bundle から改めて展開し、許可された変更だけを適用して `bench.verify` に渡す。
   agent が `build/` などの無視されるディレクトリに置いたものは採点に入らない。
6. `run.json` を最後に書く。`run.json` のない run ディレクトリは未完了として扱う。

## コマンド

```sh
python3 -m bench.run --catalog cases/catalog.json --case ALG-001 \
    --bundle <bundle.tar> --package autoware_interpolation \
    --grader-tests <採点テストのディレクトリ> --agent <agent.json> \
    --runs-dir <runs> [--run-id ID] [--rerun-of ID --reason TEXT] \
    [--verify-cmd '<JSON配列>'] [--verify-timeout 1800] [--kill-grace 10]
```

bundle・採点テスト・runs はリポジトリの外（非公開の cache）に置く。

agent 定義の例:

```json
{"name": "example-agent", "argv": ["<command>", "<arg>"], "env": {"KEY": "value"}, "info": {"model": "<model id>"}}
```

`env` の値は `run.json` に書かず、キー名だけを残す。

終了コード:

| コード | 意味 |
|---|---|
| 0 | `pass` |
| 1 | `task_fail` または `invalid_submission` |
| 3 | `infra_error` |
| 2 | 使い方の誤り、前提の不成立 |

## 判定

上から順に、最初に当てはまったものを採る。

| 条件 | 状態 | `status_reason` |
|---|---|---|
| agent を起動できない | `infra_error` | `agent_start_failed` |
| bundle・採点テスト・catalog の改変 | `invalid_submission` | `bundle_changed` など |
| 編集範囲の外の変更、リンク、特殊ファイル | `invalid_submission` | `edit_outside_editable_paths` |
| 制限時間の超過 | `task_fail` | `wall_budget_exceeded` |
| 出力 tokens・tool 回数の超過 | `task_fail` | `budget_exceeded:<項目>` |
| 採点結果がある | 採点結果の状態 | `graded` |
| 採点記録が読めない | `infra_error` | `verify_record_invalid` |

## 実行記録

```text
<runs>/<case id>/<run id>/
  workspace/   agent が作業したコピー
  grading/     bundle + 許可された変更（採点に使ったもの）
  agent.stdout  agent.stderr  usage.json
  changes.patch  verify.json  run.json
```

`run.json` には次のものを残す。

- ケース・catalog・bundle・採点テストの hash。
- image、予算、agent の名前・argv・`info`。
- 終了状況、使用量、変更、違反。
- 採点の要約と `verify.json` の hash。
- 時刻、Python の版。

argv にはローカルのパスが入りうるため、`run.json` をそのまま公開しない。

## 限界

このrunnerだけでは、実モデルの比較に使える隔離にならない。
agent は runner と同じ OS ユーザーで、host 上で動く。台本どおりに動く control（参照修正・無変更・範囲外編集）で採点境界を確かめるためのもの。
実モデルを走らせる前に、agent を別ユーザーの container で動かす仕組みを入れる（#12 の次の段階）。

- **workspace の外を読める。** agent は host のファイル、他の run、非公開の cache を読める。
- **記録を後から書き換えられる。** `setsid` などで process group から抜けた子プロセスは、終了時の SIGKILL で止まらない。
  そうした子プロセスは、runner が `run.json` を書いたあとでも run ディレクトリを書き換えうる。
- **使用量は自己申告。** `usage.json` は agent 側（adapter）が書く。runner はモデルを呼ばないため、tokens と tool 回数を独立に測れない。adapter はクライアントの使用量の記録から書く。
- **採点には固有の限界がある。** 採点器の限界は [verify.md](verify.md#限界) を参照。
