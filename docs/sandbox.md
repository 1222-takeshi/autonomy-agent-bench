# agent の隔離実行

`bench/sandbox.py` は、[runner](runner.md) から agent として起動される launcher。
実モデル（Claude Code のクライアント）を container の中で動かし、外へはモデルの endpoint にしか出られないようにする。
`bench/egress.py` はその唯一の出口になる中継で、別の container で動く。

```text
host ─ bench.run ─ bench.sandbox
                      │ docker
      ┌───────────────┴──────────────────────────────┐
      │ network aab-net-<run>（--internal：外への経路なし） │
      │   agent container ──http──> egress container ─┼─bridge─> モデルの endpoint
      │   （/work = workspace だけ）  （key を差し込む）   │
      └──────────────────────────────────────────────┘
```

## 使い方

agent 定義の `argv` を launcher にする。

```json
{"name": "claude-code-<model>",
 "argv": ["python3", "<repo>/bench/sandbox.py",
          "--agent-image", "aab/agent-claude:{case}", "--egress-image", "<採点 image のタグ>",
          "--endpoint", "http://<endpoint-host>:<port>", "--key-file", "<key file（権限 600）>",
          "--model", "<model id>", "--max-turns", "200"],
 "info": {"model": "<model id>", "client": "claude-code <版>"}}
```

- `--agent-image` の `{case}` は、実行するケースの ID を小文字にしたものに置き換わる（例: `aab/agent-claude:feat-001`）。ケースごとに採点 image が違うため、agent の image もケースごとに作る。
- agent の image は `cases/env/agent-claude/Dockerfile` から、そのケースの採点 image を `BASE_IMAGE` にして作る。
  - 土台は採点 image で、ROS 2 と colcon を含む。
  - Node と Claude Code の版を固定し、Node の tarball は SHA256 で検証する。
- ビルドの前に、`BASE_IMAGE` に渡すタグが catalog の image ID を指すことを確かめる。

### endpoint と認証の組み合わせ

| 受験するモデル | `--endpoint` | `--auth-style` | `--client-auth` | `--key-file` の中身 |
|---|---|---|---|---|
| Spark のローカルモデル（vLLM のゲート） | `http://<endpoint-host>:<port>` | `both`（既定） | `api-key`（既定） | ゲートの key |
| Anthropic API（API key） | `https://api.anthropic.com` | `x-api-key` | `api-key` | API key |
| Anthropic（サブスクリプション） | `https://api.anthropic.com` | `bearer` | `oauth` | `claude setup-token` で発行したトークン |

- `https` の endpoint では、egress が TLS で接続する。証明書とホスト名は image の CA で検証する。
- agent の container が持つ認証情報は、どの組み合わせでもダミーの値だけ。
  - `api-key` では `ANTHROPIC_API_KEY`、`oauth` では `CLAUDE_CODE_OAUTH_TOKEN` にダミーを入れる。
  - egress が、受け取った `Authorization` と `x-api-key` を捨て、`--auth-style` の形で本物を差し込む。

## 隔離の内容

| 対象 | 設定 |
|---|---|
| 通信 | agent は `--internal` のネットワークだけに属する。名前解決もインターネットも Tailscale も届かない。出口は `egress:18888` だけで、その先は `--endpoint` の 1 か所に固定 |
| key | 本物の key は egress の container だけが読み取り専用で持つ。agent はダミーの key を送り、egress が差し替える。ログにも agent にも key は出ない |
| 中継の制限 | origin-form の HTTP/1.x だけを通す。1 接続 1 リクエストで、`TRACE`・`CONNECT` などは拒否する。同時接続は 64 まで |
| ファイル | agent に mount するのは workspace（`/work`）と、読み取り専用のプロンプトだけ。host のホーム、非公開の cache、他の run は見えない |
| 権限 | uid 1000、`--cap-drop ALL`、`no-new-privileges`、pids・メモリ・CPU に上限を設ける。`/tmp` と `/home/bench` は tmpfs |
| ツール | `WebFetch`・`WebSearch` は無効にする（上流の修正は公開されている）。MCP は読まない |
| 片付け | agent・egress・ネットワークは、正常終了でも失敗でも SIGTERM でも消す |

## プロンプト

agent に渡すのは、bundle の `TASK.md` に、catalog から作った共通の規則（harness rules v1）を足したもの。
規則の内容は、編集してよいパス、`/tmp` を作業場所に使うこと、制限時間と tool 回数、通信がないことの 4 つ。
どのモデルにも同じ文面を渡す。実際に渡したものは run ディレクトリの `prompt.md` に残る。

`TASK.md` は hash で固定しているため変えない。規則を足したのは、最初の実モデルの run（Ornith、ALG-001）が修正自体は正しかった（参考採点 41/41）のに、再現用のフォルダを workspace に作って `invalid_submission` になったため。

## 使用量

`usage.json` は、agent の container の stdout（Claude Code の stream-json）を host 側で受け取って作る。
container の中のファイルからは書き換えられない。
tool 回数は transcript の `tool_use` を数え、tokens は最後の `result` 行の `usage` を使う。

## 残る限界

- **tokens は申告値。** tokens はクライアントが報告する値で、モデルの endpoint 側では測っていない。
- **endpoint は信頼が前提。** egress は応答をそのまま返す。endpoint はリクエストのヘッダーを応答に含めないもの（自前の vLLM のゲート）を前提にする。
- **採点の限界は残る。** agent はビルドとテストを `/work` で自由に実行できる。採点は bundle を展開し直した workspace で別に行うため、`/work` の生成物は採点に入らない。採点器そのものの限界は [verify.md](verify.md#限界) を参照。
