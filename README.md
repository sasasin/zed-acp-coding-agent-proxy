# Zed ACP Grok Build Proxy

Zed editor の External Agent / ACP 接続とコーディングエージェントの間に置く stdio プロキシです。現在は Grok Build に対応しています。

このプロキシは Zed と Grok Build の通信を中継しながら、ACP の送受信ログを `logs/` 配下へ保存します。Grok Build は npm パッケージ `@xai-official/grok` を `npx` 経由で起動します。

* https://www.npmjs.com/package/@xai-official/grok

## 必要なアプリケーション

以下が事前にインストールされ、PATH から実行できる必要があります。

- Zed editor
- uv
- Node.js / npx

## Zed settings.json の設定例

Zed の `settings.json` の `agent_servers` に custom agent server を追加します。

```json
{
  "agent_servers": {
    "grok-build": {
      "type": "registry"
    },
    "Grok Build (handmade)": {
      "type": "custom",
      "command": "uv",
      "args": [
        "run",
        "/path/to/zed-acp-coding-agent-proxy/zed-acp-coding-agent-proxy.py"
      ],
      "env": {
        "GROK_BUILD_VERSION": "0.2.72",
        "GROK_BUILD_MODEL": "grok-build"
      }
    }
  }
}
```

`GROK_BUILD_VERSION` に指定できるのは https://www.npmjs.com/package/@xai-official/grok?activeTab=versions にて公開されているバージョンのみです。

`GROK_BUILD_MODEL` を変更すると、Grok Build 起動時に渡すモデルを切り替えられます。

例:

```json
"GROK_BUILD_MODEL": "grok-composer-2.5-fast"
```

## ログ

プロキシを起動すると、以下のようなセッションディレクトリが作成されます。

```text
/path/to/zed-acp-coding-agent-proxy/logs/session-YYYYMMDD-HHMMSS-PID/
```

主なログファイル:

- `events.log`: プロキシ起動情報、Grok 起動コマンド、終了状態
- `frames.log`: Zed と Grok Build の ACP メッセージ
- `zed-to-grok.bin`: Zed から Grok Build への生データ
- `grok-to-zed.bin`: Grok Build から Zed への生データ
- `grok-stderr.log`: Grok Build の stderr

## 通信の加工とフィルタ

このプロキシは基本的に Zed と Grok Build の stdio 通信をそのまま中継します。ただし、Zed と Grok Build の ACP 実装差を吸収するため、Grok Build から Zed へ向かう一部のメッセージだけを Zed へ転送せずに破棄します。破棄したメッセージも `frames.log` には `grok->zed filtered` として記録します。

### ExtNotification とは

ACP は JSON-RPC 風のプロトコルで、通常の request / response のほかに notification を扱います。notification は `method` を持ちますが `id` を持たず、受信側から response を返さない一方通行の通知です。

その中でも `method` が `_` で始まるものは、ACP の標準機能ではなく、各 agent や client が独自に追加する extension method として扱われます。`_x.ai/...` は xAI / Grok Build が使っている private extension method です。

ACP の考え方として、未知の extension notification は受信側が理解できなければ無視してよいものです。たとえば Grok Build が `_x.ai/models/update` や `_x.ai/mcp/servers_updated` を送ってきても、Zed がその意味を知らないなら、Zed の通常動作に必要な ACP メッセージだけを処理して、その通知は捨てて構いません。

このプロキシの主な存在理由は、まさにその「Zed が理解しない Grok Build 独自 notification」を Zed に届く前に無視することです。これにより、Zed が未知の `_x.ai/...` method に対して error response を返し、その error response が Grok Build 側の stderr や接続状態を乱す、という相性問題を避けます。

### Zed から Grok Build への通信

Zed から Grok Build への通信は加工していません。受け取った byte stream をそのまま Grok Build へ転送します。

### Grok Build から Zed への通信

以下のメッセージは Zed へ転送せず、ログにだけ残します。

#### `_x.ai/` で始まる private notification

例:

- `_x.ai/mcp/servers_updated`
- `_x.ai/settings/update`
- `_x.ai/announcements/update`
- `_x.ai/mcp/init_progress`
- `_x.ai/mcp_initialized`
- `_x.ai/mcp/server_status`

これらは Grok Build / xAI 側の ExtNotification です。Zed 側には対応する handler がないため、そのまま転送すると Zed が `Method not found` の JSON-RPC error response を返します。その error response は JSON-RPC notification への応答としては相性が悪く、Grok Build 側で `received message with neither id nor method` のような stderr ログにつながることがありました。

そのため、`method` が `_x.ai/` で始まり、かつ `id` を持たない notification は Zed へ転送せずに破棄します。

#### `id == "skills-reload"` の result response

Grok Build が `id: "skills-reload"` を持つ response を送ることがあります。Zed はこの ID の request を送っていないため、そのまま転送すると Zed 側で `received response for unknown id, no subscriber found id=String("skills-reload")` という warning になります。

この response は Zed の ACP セッション成立や通常の prompt 実行には不要に見えるため、Zed へ転送せずに破棄します。

## 直接起動する場合

Zed からではなく手元で起動確認する場合は、以下のように実行できます。

```powershell
$env:GROK_BUILD_VERSION = "0.2.72"
$env:GROK_BUILD_MODEL = "grok-build"
uv run /path/to/zed-acp-coding-agent-proxy/zed-acp-coding-agent-proxy.py
```

この場合、ACP の stdin/stdout を待ち受けるため、通常は Zed から custom agent server として起動して使います。
