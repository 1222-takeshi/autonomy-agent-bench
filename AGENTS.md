# Development rules

- ユーザー向け報告とPR descriptionは日本語。Commit message / PR titleは英語。
- open Issueへ紐付け、origin/main同期後に専用branch/worktreeで変更する。mainへ直接実装しない。
- 最小の一貫した変更とnormal/edge/invalid/regressionを検証する。原則300行程度、500行超は分割を検討。
- read-only reviewer 2〜4名でBLOCKING/IMPORTANTが0件になるまでmergeしない。
- tests/static/validationをPRへ記載し、Quality Gate後にsquash mergeする。task branch/worktreeを片付け、main同期cleanを確認。
- 複数writerは独立Issue/worktreeへ分ける。他者の変更を取り消さない。

## Benchmark invariants

- raw/source/baselineはimmutable。採点・入力の変更はversionを変え、再実行を別記録にする。
- 候補ケースを上流の実在バグと断定しない。未検証はdraft、source.kind=candidateとする。
- ready metadataの静的検査と、実コード・参照修正・実通信の検証を区別する。
- 実行runnerが採点境界と再現性を検証するまでモデル比較を公表しない。
- candidateへ参照解/隠しテスト/他モデルの解答を渡さない。自己申告のpassを採点結果にしない。
- 実車・host network・Docker socket・秘密情報をcandidateへ渡さない。
- 第三者データ、大きな生成物、raw結果、実ホスト名・ユーザー名・絶対パスをcommitしない。
- CIはprivate repo専用の隔離self-hosted runnerで実行する。permissionsはcontents:read、actionはSHA固定、pull_request_target不使用。
- runnerへhost mount・Docker socket・GPU・PATを渡さない。public化前にrunner登録を解除し、GitHub-hostedへ切り替える。
- privateを維持する。公開や第三者データの再配布はユーザーの明示指示を確認してから行う。

## Checks

`python3 -m unittest discover -s tests -v` と `python3 -m bench.catalog cases/catalog.json`、Python構文・JSON・相対リンク・秘密/host/pathの差分検査、`git diff --check`を実行する。
`--require-ready`は現catalogでは失敗するのが正しい。通すためにdraftをreadyへ書き換えない。
完了時はIssue/PR/merge commit、変更と目的、Tests/静的解析/Validation/Review、PRサイズ、mainの状態を日本語で報告する。
