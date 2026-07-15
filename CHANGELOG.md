# Changelog

## [0.5.0](https://github.com/maps90/jean/compare/v0.4.0...v0.5.0) (2026-07-15)


### Features

* **approval,mcp:** enforce approvals in the SDK, and run MCP servers once per worker ([dfd6a10](https://github.com/maps90/jean/commit/dfd6a1012f052633480ca0130cd4d3cd7e7e324f))
* **approval:** add an Always-allow button to the gate ([6532088](https://github.com/maps90/jean/commit/6532088d87fbc9f878615d8efbd5aecb080e130f))
* **approval:** carry once/always scope through the coordinator ([752c894](https://github.com/maps90/jean/commit/752c89461c394fa232548e392bdf73e7b9f07dca))
* **approval:** deterministic risk classifier for tool calls ([dbc99e9](https://github.com/maps90/jean/commit/dbc99e99620a6e4459fb0fd9fb36dc6b078fd6f2))
* **approval:** gate only risky tool calls via the classifier ([22c56c0](https://github.com/maps90/jean/commit/22c56c0d329a6df67e894c1b182a9627d719f3a6))
* **approval:** gate only risky tool calls via the classifier (cont.) ([f674987](https://github.com/maps90/jean/commit/f674987a7bf54a749ecfdd54bbbd0e21f1a87c52))
* **approval:** intercept ExitPlanMode as the single plan approval ([af20055](https://github.com/maps90/jean/commit/af200551d2d15dd95ae46753729cf4a003092d1f))
* **config:** default permission_mode to plan (plan-then-approve) ([5f748f3](https://github.com/maps90/jean/commit/5f748f320dcdbbde477ab2326fc03285c7d2c2a2))
* **config:** gate risky tools by default; drop plan re-arm ([bb2286a](https://github.com/maps90/jean/commit/bb2286a88a828ff3f438c9d97dad84567441d181))
* **db:** store thread transcripts as gzipped blobs, cascading with the session ([b8ac4c0](https://github.com/maps90/jean/commit/b8ac4c03264636df976f3b8814b44ddb849b32e6))
* **gateway:** reply only to the thread's conversation partner ([9ee2364](https://github.com/maps90/jean/commit/9ee236447999d37609a8a91782b97eebfca7529e))
* **maintenance:** expire sessions at 3 days and approvals at 30, swept daily ([d11c00d](https://github.com/maps90/jean/commit/d11c00d81e50dd8ade8d0f10ec1f1ca1cef4b1d8))
* **mcp:** expand ${VAR} in remote server configs, failing at boot on an unset one ([12029a2](https://github.com/maps90/jean/commit/12029a2537a4c3a2eedd8b1b8cdc9e8ffe4a6dc3))
* **persona:** describe mention-gated engagement in the prompt ([9730c5a](https://github.com/maps90/jean/commit/9730c5a20201bc94c53aa0a35c4865fba4e172e8))
* **persona:** name the agent from the soul, not the project ([b17470f](https://github.com/maps90/jean/commit/b17470f3e5465ddc7a2471790fc1d6b1461401de))
* **ports:** add TranscriptStore, turn_seq, and separate prune windows ([e6d4bd8](https://github.com/maps90/jean/commit/e6d4bd8b105d7b0a9462d9dee24d90f7b812384c))
* **server:** wire transcript persistence and the new retention windows ([35aab9a](https://github.com/maps90/jean/commit/35aab9a115cac5b3452a76b3274035a11db53477))
* **session:** hydrate and archive transcripts; drop a client another worker advanced ([9190ec8](https://github.com/maps90/jean/commit/9190ec853d5f9e5ce5ae1ecc9a7a9aae1a652824))
* **session:** locate the CLI's on-disk transcript for a session id ([74a0a55](https://github.com/maps90/jean/commit/74a0a5508c3f610ee8acb4118311ec8d97c6bfd9))
* **session:** re-arm plan mode each turn so approval binds to one plan ([fe1ccd5](https://github.com/maps90/jean/commit/fe1ccd5789894427575078810c202238c4e43627))
* **store:** store a thread's conversation partner (engaged_with) ([1267976](https://github.com/maps90/jean/commit/126797681bb8f8e2f89240804444c45158ce6e50))


### Bug Fixes

* **approval:** close classifier gaps (Read secrets, multi-flag rm, scp, env print) ([29d600c](https://github.com/maps90/jean/commit/29d600cdc3eae5c0fe22f2d6a1466515675de172))
* **approval:** harden risk classifier patterns ([40102e1](https://github.com/maps90/jean/commit/40102e1b25db8bc731ae9689194a985d1c9c3e13))
* **approval:** honor the SDK's suggested narrow pattern for Always allow ([309da32](https://github.com/maps90/jean/commit/309da3231a2b898e79fa0fe4220db9cecb77fe66))
* **approval:** retire the buttons once a request is decided ([8e12c20](https://github.com/maps90/jean/commit/8e12c201fd3ccdf8a68e6cd05c8aa386727b0573))
* **approval:** route plugin MCP tools through the risk classifier ([3397121](https://github.com/maps90/jean/commit/33971210ef9067c4396e8402d7c255b7aa4ec9d8))
* **db:** configurable asyncpg pool size + openssh-client in image ([#19](https://github.com/maps90/jean/issues/19)) ([a722475](https://github.com/maps90/jean/commit/a722475f46988b2b88c0d25d5bfb7ddc3a11ddb6))
* **db:** keep the dead engaged column so a rollback stays safe ([95a5820](https://github.com/maps90/jean/commit/95a58207c32ce5e1c83db854f4f6545a7ce81924))
* **db:** make MemoryStore.save() require an existing session like Postgres ([5eff269](https://github.com/maps90/jean/commit/5eff2694ca2a9cbdb0bc30424c0e93b3b7a0e5a3))
* **docker:** run as non-root so the CLI will start ([#22](https://github.com/maps90/jean/issues/22)) ([b2c0591](https://github.com/maps90/jean/commit/b2c05917f9b1d1ab96b3b1aaf2b5b3d9849f698c))
* **gateway:** only the partner may disengage jean from a thread ([e57ece8](https://github.com/maps90/jean/commit/e57ece8df523fc948fefab56b6c460432818d3c4))
* **mcp:** fail fast on a missing command, and log the preflight score ([bcf7f6c](https://github.com/maps90/jean/commit/bcf7f6cdd03a790217d902f3c271e10214af69cf))
* **mcp:** load plugin MCP servers at boot and allow their tools per server ([3c105a6](https://github.com/maps90/jean/commit/3c105a62a377572afc814367043f883ce355b33d))
* **mcp:** register a plugin's http MCP servers instead of dropping them ([a555329](https://github.com/maps90/jean/commit/a5553291b2bc2ebe2b42dd5cdea12b193203bf99))
* **mcp:** stop the probe reporting a healthy server as down ([832df68](https://github.com/maps90/jean/commit/832df6870f6b4cca3902701addf7be43e7c90b27))
* **session:** bind the Slack MCP server per thread, not process-wide ([e3c7aea](https://github.com/maps90/jean/commit/e3c7aeabc9916d0c26c06e5b9f17ea188ce97c88))
* **session:** hydrate when another worker advanced past our un-archived turn ([a8307ca](https://github.com/maps90/jean/commit/a8307ca5d73a88fef0d2610cfa614fc6385ee724))
* **session:** never destroy or abandon a transcript a turn is still writing ([8f6aa37](https://github.com/maps90/jean/commit/8f6aa37e1c5323042f5b83f9ea7a9df341a3092c))
* **session:** never let a DB failure destroy the only copy of a transcript ([3f18aae](https://github.com/maps90/jean/commit/3f18aaeca12c7d080ddf7343633d1bb09078ac84))
* **session:** settle on the exact record count, not "the count went up" ([a524f6a](https://github.com/maps90/jean/commit/a524f6ab2fc5d6998b998aeb59b8bd622a1af8e9))
* **session:** stop trusting a silently-broken assistant-message count ([00f6b5e](https://github.com/maps90/jean/commit/00f6b5e24edbe5e8ca2e78b4209ba9f2542f063a))
* **session:** survive a resume whose CLI transcript is gone ([#21](https://github.com/maps90/jean/issues/21)) ([5ce7e4c](https://github.com/maps90/jean/commit/5ce7e4c1adce5f23c90ece23512e47f03933d844))
* **session:** sweep under the thread lock, and stop shipping a dead setting ([a9943b3](https://github.com/maps90/jean/commit/a9943b3ff7e0b86f0522e108c125f5c4e54a0748))
* **session:** wait for the CLI to flush a turn before archiving it ([19a6de4](https://github.com/maps90/jean/commit/19a6de45c5a9743e4c16b87359c390a96367a92e))


### Performance Improvements

* **mcp:** run each MCP server once per worker, not once per session ([09c17b3](https://github.com/maps90/jean/commit/09c17b3887eec3f3748e236b7ee986255afafd8a))


### Documentation

* **approval:** fix stale plan-mode comment; document known limitations ([a7d6aa1](https://github.com/maps90/jean/commit/a7d6aa13ec37ed67a43f4c965d6756b8a010a4f0))
* **claude:** worktrees live under .claude/worktrees/, not beside the repo ([e8ddc52](https://github.com/maps90/jean/commit/e8ddc52e8f700bafbb7033f541bc65ce8198cb94))
* how to register a remote MCP server, and why an unset var is fatal ([3bc38a8](https://github.com/maps90/jean/commit/3bc38a8819c5791f4ef0cc4389c7d581c3dd53f5))
* implementation plan for mention-gated engagement ([810a213](https://github.com/maps90/jean/commit/810a2131ac4c1688c9539a1fbf3e118f41444e6d))
* implementation plan for risk-classified approval gate ([ea4063a](https://github.com/maps90/jean/commit/ea4063ad66402e1f78ec61aadf7fcf4c0ff9cad9))
* implementation plan for session transcript persistence ([9f19f43](https://github.com/maps90/jean/commit/9f19f4334915538f6ebb5a159a9056ff677e76be))
* one feature at a time, in its own worktree ([90a64b3](https://github.com/maps90/jean/commit/90a64b3ed542f8da9834336fcad69f78c056d1e4))
* one feature at a time, in its own worktree ([125c9da](https://github.com/maps90/jean/commit/125c9da993d043a1a51efa9e56547ef78e03dbe0))
* pin down Decision.partner as a resulting value, not a sentinel ([4a65108](https://github.com/maps90/jean/commit/4a651087f589f65b7e9375a517600f65b767523d))
* plan remote MCP env expansion ([cd54d02](https://github.com/maps90/jean/commit/cd54d025e61552a3affde0544478c3e3f184bbcb))
* record the two amendments made during implementation ([72f7f86](https://github.com/maps90/jean/commit/72f7f86ab5e39785183f4cea43bf26a316bc1c85))
* spec for mention-gated engagement (one partner per thread) ([0f2e329](https://github.com/maps90/jean/commit/0f2e3294d7103ace45f1180d531197f1908c2a68))
* spec for risk-classified approval gate (slaude model) ([b2ad8c8](https://github.com/maps90/jean/commit/b2ad8c857b2b4f37434774b7fd7153b9e1481683))
* spec for session transcript persistence + retention ([fa09370](https://github.com/maps90/jean/commit/fa093704865e91018c5da17d6629ee754b3c2d28))
* spec matches the shipped app_mention filter and the shared _engage path ([189dda2](https://github.com/maps90/jean/commit/189dda20587f8d076676599d8a4c408d9a5e1921))
* spec remote MCP env expansion so Portico stops arriving as curl ([49b22c4](https://github.com/maps90/jean/commit/49b22c4d361634d9b1a28cc1e2adfdc5ba17f6a6))
* transcripts live in Postgres, not just the pod that wrote them ([8cda3d8](https://github.com/maps90/jean/commit/8cda3d814eb190d362d4e79aa37ec251549490f4))

## [0.4.0](https://github.com/maps90/jean/compare/v0.3.0...v0.4.0) (2026-07-12)


### Features

* **plugins:** resolve SSH marketplace URLs over SSH transport ([#15](https://github.com/maps90/jean/issues/15)) ([9587b82](https://github.com/maps90/jean/commit/9587b82c40f05b851e7c5b10604a2b08bcf1bc11))

## [0.3.0](https://github.com/maps90/jean/compare/v0.2.0...v0.3.0) (2026-07-11)


### Features

* report running version on /healthz ([#7](https://github.com/maps90/jean/issues/7)) ([d5511fa](https://github.com/maps90/jean/commit/d5511fa9c1a55db746888a005c4e03be42d8fe13))

## [0.2.0](https://github.com/maps90/jean/compare/v0.1.0...v0.2.0) (2026-07-11)


### Features

* add release-please + GHCR publish pipeline ([e60086e](https://github.com/maps90/jean/commit/e60086e5640803c1ac16b0720ef9cfe452161e87))
* add release-please + GHCR publish pipeline ([fe44d29](https://github.com/maps90/jean/commit/fe44d29dc1e0bc0ab863b2cd4a1d1c7885873d66))
