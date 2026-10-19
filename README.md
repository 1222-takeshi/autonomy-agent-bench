# autonomy-agent-bench

Autoware等の自動運転ソフトウェアを調査・修正・拡張するLLM agentの評価基盤。
単発の競技問題に加えて、コード・設定・ログ・通信をまたぐ開発作業を評価する。

**現在は設計基盤。実行可能ケースは0件、モデル評価は未実行。**
下記3件は課題案であり、特定の上流バグや機能不足を確認済みという意味ではない。

| 系統 | 最初の候補 | 実ケース化 |
|---|---|---|
| アルゴリズムのバグ解析 | 軌跡補間の角度境界・重複点 | [ALG-001 / #2](https://github.com/1222-takeshi/autonomy-agent-bench/issues/2) |
| システム統合のバグ解析 | topicは見えるが受信できないROS 2通信 | [INT-001 / #3](https://github.com/1222-takeshi/autonomy-agent-bench/issues/3) |
| アルゴリズムの機能実装 | 停止点を保持する軌跡再サンプリング | [FEAT-001 / #4](https://github.com/1222-takeshi/autonomy-agent-bench/issues/4) |

統合系はQoS・DDS・namespace/remapping・TF・stamp/clockを対象に含める。
原因の説明、機能の合否、回帰、作業時間、tokens、ツール操作を別々に記録する。
旧LiveCodeBenchの点数とは別の評価軸とし、初期の少数例で総合順位を出さない。

## 使い方

Python 3.10以上、標準ライブラリのみ。現時点のコマンドは静的な台帳検査であり、モデル・ROS・ケースを実行しない。

```sh
python3 -m bench.catalog cases/catalog.json
python3 -m unittest discover -s tests -v
python3 -m bench.catalog cases/catalog.json --require-ready
```

最後のコマンドは現在exit 2になる。全件draftのためで、現段階では期待どおり。
`--require-ready`が通っても実行・証拠の真正性を検証したことにはならず、管理者の再現確認が別途必要。

## 構成と次の段階

- [cases/catalog.json](cases/catalog.json): 候補と再現・昇格条件。revision等の未決値はnull。
- [docs/protocol.md](docs/protocol.md): 入出力、採点、状態遷移、データ境界。
- `bench/catalog.py` / `tests/`: metadata検査とnormal/edge/invalid/regressionのテスト。
- GitHub-hosted CI: テスト・catalog・Python構文・差分空白を検査。ROSやモデルは起動しない。[CI運用](docs/ci.md)。

#2〜#4で固定source・環境・隔離実行・参照修正・誤修正・回帰を用意してからreadyへ進める。
その後にagent実行runnerと比較対象モデルのrun設定を固定する。現時点でrunner・実ケースbundle・採点器は未実装。

## データと出典

このrepoはpublic。第三者source・rosbag・地図・大きな生成物・raw結果・参照解はcommitしない。
取得元、revision、ライセンス、artifactのSHA256を記録し、候補側には問題に必要なsnapshotだけを渡す。
実車・実ネットワークの設定変更や走行試験を評価に含めない。LICENSEは未設定。第三者データを追加する場合は出典と利用条件を確認する。

- [Autoware公式インストール手順](https://docs.autoware.org/main/installation/autoware/source-installation/): 対象revisionに合うROS環境を選び、ケースごとに固定する。
- [ROS 2公式QoS仕様](https://github.com/ros2/ros2_documentation/blob/humble/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst): 提供/要求の互換性と遅延参加の契約を参照する。
