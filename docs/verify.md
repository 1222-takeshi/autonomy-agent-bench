# 検証コントローラー

`bench/verify.py` は、候補者の環境の外で ROS 2 パッケージを 1 つだけ構築し、
その gtest スイートと grader 所有のテストを実行して、構造化した結果を書き出す
採点controllerです。モデルや ROS を直接起動せず、結果の生成と集計のみを行います。

## 目的

- 固定された container の中で、入力 workspace の `src/` 配下にある指定パッケージを構築する。
- 構築が通ったあと gtest を実行し、合格/不合格/スキップをテスト単位で記録する。
- 入力のハッシュ、実行したdockerのコマンド、時刻、時刻から算出した所要時間、
  標準出力/標準錯誤の末尾を残し、再現と採点の根拠にする。

## コマンド

```sh
python3 -m bench.verify \
    --image ros:humble@sha256:<digest> \
    --workspace <workspace> \
    --package <package> \
    [--tests <grader_test_dir>] \
    [--require-suite <suite> ...] \
    --out result.json \
    [--timeout 1800] [--memory 8g] [--cpus 4]
```

- `--image` は digest 固定の `ros:humble-ros-base` を渡す。タグだけでは起動しない設計にする。
- `--workspace` は `src/` を含む colcon ワークスペースのルート。入力ディレクトリは変更しない。
- `--tests` を渡すと、その中のファイルをパッケージの `src/**/<package>/test/` へ配置する。
  サブディレクトリ構成を相対パスのまま保ち（`tests/src/a.cpp` → `<package>/test/src/a.cpp`）、
  任意のファイルがシンクリンクの場合、また解決先が `<package>/test/` の外に出る場合に拒否する。
  採点器のスイートはサブディレクトリを glob で読むため、構成を落とさない。
- `--require-suite` を渡すと、そのスイートから少なくとも 1 件のテストが走らなかったとき、
  結果を `invalid_submission`（採点不可能）とする。複数回指定可能。
- `--out` に結果の JSON を書く。

## 隔離の設定

container は以下で起動します。ホストの Docker ソケットやネットワークへは接続しません。

- `--rm --network none --read-only` で、書き込み可能な `/tmp` だけ tmpfs で確保する。
- 各走査にユニークな `--name bench-verify-<uuid4>` を付与し、`--mount type=bind,...` で
  入力ワークスペースを `/ws` に接続する。
- `--user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --pids-limit 1024` で権限とリソースを絞る。
- `--memory` / `--cpus` で資源上限を設ける。
- container 内では `source /opt/ros/humble/setup.bash` のあと `colcon build` →
  `colcon test` (`-R '^test_'`) → `colcon test-result --all` を 1 つの bash で実行する。
- timeout を超えた走査は `docker kill <name>` で名前通りの container を殺す（エラーは無視）。

## 結果のフィールドと状態

JSON は `schema_version: 1` を先頭にし、以下を含みます。

- `status`: `pass` / `task_fail` / `invalid_submission` / `infra_error`
- `phase`: 失敗を区分する `build`（コンパイル）/ `test` / `infra`
- `tests`: 各テストの suite/name/status
- `counts`: `total` / `pass` / `fail` / `skip`
- `image`, `package`
- `required_suites`: `--require-suite` で指定したスイート名（リスト）
- `timed_out`: timeout を超えた走査かどうかの真偽
- `workspace_sha256`, `tests_sha256`: 入力とテストの決定論的ハッシュ
  （`.git/` `build/` `install/` `log/` を除外し、相対パスとファイルバイトで計算）
- `docker_argv`: 実行したdocker.argv（リスト）
- `exit_code`
- `start_utc` / `end_utc` / `duration_seconds`
- `stdout_tail` / `stderr_tail`: 各 4000 文字

状態の定義:

- `pass`: 構築が通り、少なくとも 1 つのテストが走り、全テストが合格。
- `task_fail`: コンパイル失敗、いずれかのテスト不合格、テストが 0 件、
  または構築完了後に timeout を超えた走査。
- `invalid_submission`: 構築は通ったが、`--require-suite` で指定した採点器のスイートから
  1 件のテストも走らなかった。不合格のテストがあればそちらは無視してこちら優先。
- `infra_error`: docker の欠落・image の欠落・OOM による中断、
  構築出力前にcontainerが終了、XMLが読み込めない等、controller/環境の故障。
  構築前に timeout を超えた走査もこちら。

ツール全体の終了コードは `0` が pass、`1` が task_fail と invalid_submission、
`3` が infra_error、`2` が使用誤り（必要な引数の欠落など）です。

## 真正性についての注記

このツールは入力や採点器のハッシュ・実行コマンド・環境を記録しますが、
**入力や結果の真正性は保証しません**。hash は照合・検証の根拠として使い、
候補側へ参照解・隠しテスト・採点出力を渡さない境界は別途実装して使います。

## 限界

- 採点対象のコードは採点器のバイナリと同じ container で動作します。
  したがって候補側が偽造した gtest XML をこのツールが検出することはできず、
  パッチのブラインドレビューで対応します。
- 入力 workspace にシンクリンク（ファイル・ディレクトリ双方）が含まれると、
  ホスト側のコントローラーがその先へ辿るリスクがあるため、container を起動する
  前に `invalid_submission`（phase `build`）で拒否します。実行後の gtest 結果
  収集とハッシュも、シンクリンクを辿らずにスキップします。
- `--tests` で渡した採点器の C++ ソースから期待する `TEST`/`TEST_F`/`TEST_P` の
  ペアを抽出し、結果にそれぞれ出現しなければ `invalid_submission`（phase `test`）
  にします。同じスイートの別の `TEST` では要件を満たしません。
- 早期の `infra_error` / 使用誤りの記録も、通常の記録と同じフィールド
  （`tests: []`、`counts` のすべての 0、`image`、`package`、`required_suites`、
  `timed_out`、`docker_argv: null`、メッセージは `note`）を持ちます。
- このツールは `editable_paths` を強制しません。候補の diff の検証は runner が行います。
- 採点のテストは ctest のテスト名が `test_` で始まるように登録する必要があります
  （`colcon test -R '^test_'`）。
