# ADR-0001: 配布名と namespace の確認

- 状態: 製品・CLI 名は正本どおり。公開 namespace は未取得・未確定
- 確認日: 2026-10-03 UTC（公開ページ実見 02:05–02:07、公式仕様確認 02:04–02:07）
- 正本: `MANY_HUB_MASTER_PLAN.md` §13, §15, §20, §21
- 制約: 読み取りのみ。登録、予約、publish、push、OAuth、credential 作成を行っていない

## 判断

1. 製品名 `MANY Hub`、repository 候補 `ishizakahiroshi/many-hub`、CLI 名 `manyhub` を維持する。CLI 名は registry の package 名と独立してよい。
2. 下表は公開ページの観測であり、「取得済み」「登録可能」「所有者本人」「商標上問題なし」を意味しない。404、検索 0 件、取得ツールのエラーを区別する。
3. npm はローカル側の確認結果を待つ。npm が未確定でもローカル実行、ネイティブ配布、Docker 配布の設計を止めない。実装用 package metadata と公開先を混同しない。
4. image 名候補は `ghcr.io/ishizakahiroshi/many-hub`。GitHub owner と package 書込権限の確認、既存 private package との照合、公開の許可が必要。Docker Hub は所有を確認した namespace の下の `many-hub` とし、owner 名を GitHub と同一と仮定しない。

## 観測結果

| 対象 | 公式 URL / 情報源 | UTC | 確認結果と限界 |
|---|---|---|---|
| npm `many-hub` | https://www.npmjs.com/package/many-hub / https://registry.npmjs.org/many-hub | 02:03–02:06 | Web 取得は利用不能／cache miss。cloud browser は bot/security verification が継続し package 本文に到達しなかった。**未検証**。404 と判断しない。 |
| npm `manyhub` | https://www.npmjs.com/package/manyhub / https://registry.npmjs.org/manyhub | 02:03–02:04 | Web 取得は利用不能／cache miss。**未検証**。npm の browser block 後に別経路で同サイトを再試行していない。 |
| npm scoped package | [npm scopes](https://docs.npmjs.com/about-scopes/) | 02:04 | `@<verified-npm-owner>/many-hub` を代替候補にできる。npm owner は未確認。GitHub `ishizakahiroshi` という名前だけでは同名 npm scope の所有は証明できない。 |
| PyPI `many-hub` | https://pypi.org/project/many-hub/ | 02:05:33 | cloud browser が PyPI の **Error code 404** を表示。公開 project ページは見つからなかった。登録可能性は未確認。 |
| PyPI `many_hub`, `many.hub` | [PyPA name normalization](https://packaging.python.org/en/latest/specifications/name-normalization/) | 02:04 | いずれも `many-hub` と同一の正規化名。別々の空き namespace と数えない。 |
| PyPI `manyhub` | https://pypi.org/search/?q=manyhub | 02:05:41 | PyPI 自身の検索 UI が **0 projects** と表示。検索から除外される archived project 等はあり得るため、直接 project API の不存在や登録可能性までは確認していない。 |
| Docker Hub repository 候補 | https://hub.docker.com/r/ishizakahiroshi/many-hub | 02:06:30 | cloud browser が **404 Route Not Found** を表示。公開 repository は見つからなかった。private repository や書込権限は未確認。 |
| Docker Hub owner 候補 | https://hub.docker.com/u/ishizakahiroshi | 02:07:10 | 上記 repository の owner リンクを開くと **Page Not Found**。この Docker ID の登録可能性・所有者は未確認。 |
| Docker Hub product-named owner 候補 | https://hub.docker.com/u/manyhub | 02:07:11 | cloud browser が **404 Route Not Found** を表示。登録可能性・所有者は未確認。 |
| GHCR `ishizakahiroshi/many-hub` | https://github.com/users/ishizakahiroshi/packages/container/package/many-hub / [GHCR 公式仕様](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) | 02:07 | 公開 package ページの Web 取得は cache miss。**未検証**。GitHub repository の有無、GHCR package の有無、package publish 権限は別々に確認する。 |
| GitHub repository `ishizakahiroshi/many-hub` | https://github.com/ishizakahiroshi/many-hub | — | 2026-10-03 02:16 UTC に認証済み GitHub connector で **公開 repository の存在**、owner `ishizakahiroshi`、push/admin 権限を確認。ユーザー自身が作成。これは npm/PyPI/Docker/GHCR の登録や公開の許可・取得を意味しない。 |

検索エンジンの registry 限定検索でも該当 package の一次情報は得られなかったが、これは不在の証明として採用していない。補助的な無認証 HTTP probe は結果取得前に実行環境の承認処理が取消となり、証拠に使っていない。

## Registry ごとの制約

- **npm:** scope は npm user / organization に紐づく。unscoped 名は公開用で、類似名・policy の検査もある。単なる名前確保を目的とする空 package は作らない。[名前の条件](https://docs.npmjs.com/package-name-guidelines/)、[scope](https://docs.npmjs.com/about-scopes/)、[名前の予約に関する policy](https://docs.npmjs.com/policies/disputes/)
- **PyPI:** 大文字小文字を区別せず、`.` / `_` / `-` の連続を `-` へ正規化する。`manyhub` は `many-hub` と別名。ページがなくても、類似名、管理者の禁止、release のない登録済み project 等により登録不可の場合がある。[正規化](https://packaging.python.org/en/latest/specifications/name-normalization/)、[PyPI FAQ](https://pypi.org/help/#project-name)
- **Docker Hub:** `namespace/repository:tag` の namespace は個人 Docker ID または organization。`many-hub` は repository 名として条件を満たすが、個人 Docker ID は 4–30 字の小文字英数字のみなので `many-hub` は個人 ID にできない。`manyhub` と `ishizakahiroshi` は文字形式を満たすだけで、取得可能という意味ではない。private repository は検索に出ない。[repository 作成](https://docs.docker.com/docker-hub/repos/create/)、[Docker ID と namespace](https://docs.docker.com/faqs/accounts/)
- **GHCR:** image は GitHub personal account / organization に scope される。repository 名が同じだけでは package が自動的にリンクされず、初回 publish 時は private が既定。公開 repository の作成が package 公開や権限設定を兼ねると考えない。[Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

## 公開前の残作業

- npm ローカル確認: registry の exact-name metadata の結果、時刻、public package の有無、owner を記録。存在する場合は URL・version・repository・README・LICENSE を照合し、同一製品と推測しない。
- PyPI: `many-hub` と `manyhub` の exact-name project / JSON / Simple API の読取確認を、公開作業の直前にも行う。404 でも取得保証としない。
- GitHub/GHCR: 認証済み owner と対象 repository / package の存在・権限を読み取り確認。Docker Hub を選ぶ場合は Docker owner を別途確認する。
- 既存同名 package の metadata / README / license は今回取得できていない。同名という理由で依存採用・コード流用しない。MANY Hub の採用 license は正本の **Apache-2.0** を維持する。
- 公開 package や repository の作成、名前の登録、公開設定変更、push/release は、この調査とは別の明示的な許可を得て行う。商標調査・法的クリアランスは本調査の対象外。
