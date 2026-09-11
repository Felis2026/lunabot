# Sekai Deck Recommend Service for LunaBot

The service requires Python 3.10 or later and
`sekai-deck-recommend-cpp`. The repository Dockerfile builds the StarMoe
variant during image creation and verifies its WL3 capability.

## Docker

`start.sh` starts the official service on `127.0.0.1:45556`. The optional CN
future-event preview service uses `127.0.0.1:45557` and remains disabled
unless both of these settings are enabled:

- `DECKREC_PREVIEW_LOCAL_ENABLED=1`
- `deck.cn_jp_preview.enabled: true` in `config/sekai/sekai.yaml`

The local preview process is configured by environment variables and needs no
second YAML file. Its defaults are port `45557`, one worker, and the isolated
data directory `data/sekai/deckrec-preview`; use the
`DECKREC_PREVIEW_PORT`, `DECKREC_PREVIEW_WORKER_NUM`, and
`DECKREC_PREVIEW_DATA_DIR` variables only when those defaults conflict with
the deployment.

The Docker build defaults to the latest commit on the configured StarMoe
branch during an uncached build. Set `DECKREC_NATIVE_REF` to a full commit for
a reproducible build. See the repository
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md) for source and
license details.

Preview activation records can be audited without starting the Bot:

```bash
python src/plugins/sekai/modules/deck_preview/router.py status
```

After correcting CN data or service configuration, explicitly clear one
activity so the complete cutover checks run again:

```bash
python src/plugins/sekai/modules/deck_preview/router.py reset 184 --confirm
```

Setting `deck.cn_jp_preview.enabled: false` bypasses Preview routing and sticky
activation records without deleting them.

## Optional Finale MasterData

The Bot actively refreshes `eventCardBonusLimits`, `eventHonorBonuses`,
`eventMysekaiFixtureGameCharacterPerformanceBonusLimits`, and
`eventSkillScoreUpLimits` from each JP/CN region's own configured source.
They join official synchronization and its file fingerprint. Valid empty
arrays are retained; a failed refresh can use a valid local JSON array.
Other regions do not proactively request these optional tables.

Preview carries only optional files available in both regions and preserves
the CN contents for these tables. This does not enable finale previews.
`eventTotalPowerLimits` is not consumed by the current native kernel; its WL3
336000 cap is implemented in native code.

## Manual run

1. Install the repository requirements.
2. Edit `config.yaml`.
3. Run:

```bash
python serve.py
```

4. Point `deck.servers` in `config/sekai/sekai.yaml` to the configured host
   and port.
