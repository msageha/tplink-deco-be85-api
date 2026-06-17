# Deco BE85 API

TP-Link Deco BE85 のローカル Web API（Deco アプリが内部で叩いている API）を
FastAPI + Pydantic でラップした REST サーバーです。ステータス取得や Wi-Fi の
ON/OFF などをローカルネットワークから操作できます。

## 仕組み

Deco の管理画面と同じログインフローを再現しています。

1. `POST /cgi-bin/luci/;stok=/login?form=keys` … パスワード暗号化用 RSA 公開鍵を取得
2. `POST /cgi-bin/luci/;stok=/login?form=auth` … 署名用 RSA 公開鍵 + `seq` を取得
3. セッションごとにランダムな AES-128-CBC 鍵/IV を生成
4. リクエスト本文を AES 暗号化し、署名を RSA 暗号化（53byte チャンク）
   - ログイン時の署名: `k=<key>&i=<iv>&h=<hash>&s=<seq+len>`（AES 鍵/IV を同梱）
   - ログイン後の署名: `h=<hash>&s=<seq+len>`
   - `hash = md5("admin" + password)` … 認証後エンドポイントはこの hash を検証する
5. `form=login` で `stok` と `sysauth` Cookie を取得し、以降のリクエストで利用

> 実機 (BE85 / FW 1.2.2) の Web UI を解析して確認した挙動です。署名 hash には
> ローカル管理アカウント名 `admin` を使います（`.env` の `USERNAME`（TP-Link ID の
> メール）は local API では使われません）。

暗号化処理は `src/deco/crypto.py`、ログイン/通信は `src/deco/client.py` にあります。

## セットアップ

`.env` に認証情報を記載します（変数名はそのまま）。

```dotenv
USERNAME=<TP-Link ID（メール）。local API では未使用・参照用>
PASSWORD=<Deco管理パスワード（TP-Link ID のパスワード）>
# 任意（デフォルト値）
DECO_HOST=http://172.16.1.1
ACCOUNT=admin        # 署名 hash に使うローカルアカウント名（通常 admin のまま）
VERIFY_SSL=false
TIMEOUT=30
```

`PASSWORD` がローカル認証の本体です。`USERNAME`（メール）はクラウド用の TP-Link ID で、
ローカル API には送信されません（互換のため `.env` から読み込むだけ）。

依存関係のインストール（`make setup` でも可）:

```bash
uv sync --extra test
```

`.env` は `make setup-env`（`.env.example` をコピー）で雛形を用意できます。

## 構成

`src/` レイアウトの virtual プロジェクト（ビルドなし）。実行・テストは `pythonpath=src`
／`--app-dir src`／`PYTHONPATH=src` でパッケージを解決します。

```
src/
  deco/      Deco ローカル API クライアント（FastAPI 非依存・単体利用可）
             crypto.py / client.py / exceptions.py
  api/       FastAPI Web 層: routes.py / schemas.py / service.py
  config.py  Settings（pydantic-settings）
  main.py    FastAPI アプリ
tests/       オフライン単体テスト（deco.crypto・api.schemas）
scripts/     smoke.py(疎通) / probe.py(読み取り探索) / sniff.py(UI 復号解析)
```

`deco/` はプロトコル層（暗号化・ログイン・通信）、`api/` は Web 層という責務分離です。

## 起動

```bash
make up-dev   # = uv run uvicorn main:app --reload --app-dir src --host 127.0.0.1 --port 8000
```

- Swagger UI: http://127.0.0.1:8000/docs
- ルーターへの接続は遅延ログイン（最初の API 呼び出し時にログイン）。401/403 や
  セッション切れ時は自動で再ログインして 1 回リトライします。

### Docker

```bash
make build-image                       # docker build -t deco-be85-api:latest .
docker run --rm -p 8000:8000 --env-file .env deco-be85-api:latest
```

多段ビルド（`uv` ビルダ → `python:slim` ランナー、非 root 実行）。認証情報は
`--env-file .env` などで実行時に注入します（イメージには含めません）。

## エンドポイント

| Method | Path                       | 説明                                   |
| ------ | -------------------------- | -------------------------------------- |
| GET    | `/api/health`              | サーバー状態とログイン状況             |
| POST   | `/api/login`               | 明示ログイン                           |
| POST   | `/api/logout`              | ログアウト                             |
| GET    | `/api/dashboard`           | 概況（回線/CPU/メモリ/Deco台数/接続数）|
| GET    | `/api/devices`             | Deco ユニット（メッシュノード）一覧    |
| GET    | `/api/clients`             | 接続クライアント一覧（`?online_only=true`）|
| GET    | `/api/network/wan`         | WAN IPv4 ステータス                    |
| GET    | `/api/network/internet`    | インターネット接続情報（IPv4/IPv6）    |
| GET    | `/api/network/lan`         | LAN / DHCP DNS / WAN IP                |
| GET    | `/api/network/ipv6`        | IPv6 有効状態                          |
| GET    | `/api/network/performance` | CPU / メモリ使用率                     |
| GET    | `/api/network/mac-clone`   | MAC クローン設定                       |
| GET    | `/api/wireless`            | Wi-Fi 設定の取得                       |
| POST   | `/api/wireless`            | バンド別 Wi-Fi の ON/OFF               |
| POST   | `/api/wireless/config`     | Wi-Fi 設定変更（SSID/パスワード/enable 等）|
| GET    | `/api/wireless/power`      | 電波（DFS サポート等）                 |
| GET    | `/api/device/mode`         | 動作モード（region/workmode/sysmode）  |
| GET    | `/api/device/time`         | 時刻・タイムゾーン設定                  |
| GET    | `/api/clients/blocked`     | ブロック中クライアント一覧             |
| GET    | `/api/cloud/device-info`   | クラウド連携情報（model/role 等）      |
| GET    | `/api/system/component-info` | ERP/省電力等のコンポーネント情報     |
| GET    | `/api/system/switch-list`  | UI 機能スイッチ                        |
| GET    | `/api/system/log-types`    | エクスポート可能なログ種別             |
| POST   | `/api/reboot`              | Deco の再起動（`confirm=true` 必須）   |
| POST   | `/api/raw`                 | 任意エンドポイントへの汎用パススルー   |

### Wi-Fi トグル例

```bash
curl -X POST http://127.0.0.1:8000/api/wireless \
  -H 'Content-Type: application/json' \
  -d '{"band":"band5_1","network":"host","enable":false}'
```

`band` は `band2_4` / `band5_1` / `band6`、`network` は `host` / `guest`（enum で検証）。

### Wi-Fi 詳細設定の変更 `/api/wireless/config`

`admin/wireless?form=wlan` への `operation:write`。`band`×`network`(host/guest) ごとに
`enable` / `ssid` / `password` / `enable_hide_ssid` / `channel` / `channel_width` / `mode`
を指定できます（指定したフィールドのみ送信）。`ssid`/`password` は**平文で渡すと内部で
base64 エンコード**します（ルーターは base64 で保持するため）。

```bash
# ゲスト Wi-Fi の SSID とパスワードを変更
curl -X POST http://127.0.0.1:8000/api/wireless/config \
  -H 'Content-Type: application/json' \
  -d '{"band":"band5_1","network":"guest","settings":{"enable":true,"ssid":"My Guest","password":"secretpass"}}'
```

> Wi-Fi 設定の変更は接続中クライアントに一時的な影響があります。`settings` は未知フィールドを
> 拒否（`extra=forbid`）し、最低 1 項目の指定が必須です。設定値は `GET /api/wireless` の構造
> （`band.host` / `band.guest`）に対応します。ゲスト Wi-Fi の現在値もこの GET で取得できます。

### 汎用パススルー `/api/raw`

任意の Deco エンドポイントを直接叩けます。レスポンスは復号済みのエンベロープ全体
（`result`/`error_code` または `success`/`data`）をそのまま返します。

```bash
curl -X POST http://127.0.0.1:8000/api/raw \
  -H 'Content-Type: application/json' \
  -d '{"path":"admin/client?form=client_list","operation":"read","params":{"device_mac":"default"}}'
```

- `path`: `admin/<module>?form=<form>` 形式の相対パス（`://` や `..` は拒否）
- `operation`: `read`/`write`/`load`/`list`/`get`/`set`/`add`/`edit`/`remove`/`operate`（enum）
- `params`: 任意の追加パラメータ（省略可）

> `operation: write` と適切な `params` を渡すと設定を変更できる強力な口です。
> 未知エンドポイントの探索には `uv run python -m scripts.probe`（読み取り専用）が使えます。

### 再起動例（破壊的操作）

```bash
curl -X POST http://127.0.0.1:8000/api/reboot \
  -H 'Content-Type: application/json' -d '{"confirm":true}'
```

## 開発ツール

ruff（lint + formatter）、ty（型チェック）、pre-commit を使用します。

```bash
make format   # ruff format .
make lint     # ty check . + ruff check .
make check    # ruff format --check + ruff check + ty check（CI 相当）

# pre-commit（標準フック + ruff-format + ruff + ty / Dockerfile は hadolint を manual stage）
make precommit-install   # フックを git に登録
make precommit           # 全ファイルに対して実行
```

ruff 設定は `pyproject.toml`（line-length 88 / lint: C,E,F,I,N,NPY,PD,UP,W）。
ty 設定も `pyproject.toml`（`[tool.ty]`、`unresolved-import = "ignore"`、scripts/tests を除外）。

## テスト / 検証

```bash
make test                  # uv run pytest
make cov                   # coverage run -m pytest + report
make smoke                 # 実機への疎通スモーク（読み取りのみ・設定変更なし）
```

`.claude/verify.sh` は Stop hook 用で、オフラインで `ruff format --check` + `ruff check`
+ `ty check` + `pytest` を実行します（実機には触れません）。

## CI（GitHub Actions）

`.github/workflows/` に 2 つのワークフロー（PR・main push・手動で起動）:

- `ci.yaml` … `lint`ジョブ（`ruff format --check` + `ruff check` + `ty check`）と
  `test`ジョブ（`uv sync --locked --all-extras --dev` → `pytest`）
- `pre-commit.yaml` … `pre-commit run --all-files`（標準フック + ruff/ruff-format。
  ty は CI lint と重複するため `SKIP=ty`）

uv は `astral-sh/setup-uv` で導入、Action は SHA ピン留め。依存更新は
`.github/dependabot.yml`（github-actions / uv を weekly）。

## 注意

- ローカルネットワーク内（同一サブネット）からの利用を想定しています。
- Deco のファームウェアによってレスポンスのフィールドが異なる場合があります。
  主要モデルは未知フィールドも保持する（`extra="allow"`）ため、`/docs` の生 JSON も
  確認してください。

## 免責 / Disclaimer

本プロジェクトは **TP-Link 非公式**であり、TP-Link 社とは一切関係ありません。
Deco の公開されていないローカル API をリバースエンジニアリングして利用しています。

- **自分が管理権限を持つ機器に対してのみ**使用してください。
- ファームウェア更新で API が予告なく変わる可能性があります。
- 本ソフトウェアは現状有姿（AS IS）で提供され、いかなる保証もありません。
  利用によって生じた損害について作者は責任を負いません。

## 謝辞 / Acknowledgements

ログインの暗号化フロー（RSA + AES-CBC + 署名）は、以下の公開リバースエンジニアリング
成果を参考に再実装しています。

- [AlexandrErohin/TP-Link-Archer-C6U](https://github.com/AlexandrErohin/TP-Link-Archer-C6U)
  （GPL-3.0）— TP-Link/Deco の暗号化ログイン手順
- BE85 本体の Web UI（`tpEncrypt.js` 等）を解析し、`md5("admin"+password)` 署名や
  `device_mac` パラメータ等の機種固有の挙動を確認

## ライセンス / License

[GPL-3.0-or-later](LICENSE)。上記の参考実装が GPL-3.0 であることに合わせています。
