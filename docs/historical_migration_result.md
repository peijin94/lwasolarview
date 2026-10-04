# Historical SFU metadata migration: executed result

Completed 2026-10-04 on `pz47@inti`. Canonical archive `/nas8/lwa/spec_v2/fits`, also exposed as `/common/lwa/spec_v2/fits`.

## Result and verification

- 1072 exact daily files, 20230719–20261003.
- All 1072 now have primary `BUNIT='sfu'`.
- 1009 existing Jy-labelled cards replaced (80 bytes per file).
- 63 missing BUNIT cards inserted in existing padding by replacing END+blank with BUNIT+END (160 bytes per file).
- No spectral arithmetic, no header growth, no payload movement, no size changes; original time/frequency/calibration tables left untouched.
- Apply: candidates=1072, processed=1072, changed=1072, verified=1072, rejected=0, errors=0.
- Independent fresh `--verify`: verified=1072, rejected=0, errors=0.
- Separate Astropy inventory readback: `{'sfu': 1072}`, no missing or Jy primary units, all original file sizes preserved.
- Tool verification compares full primary header outside edited span and five deterministic post-header sample windows. This is **not a full numerical-payload checksum**. Only the 80/160-byte header spans were written.
- Service tests: 43/43 pass, including the Codex migration tests, reviewed-anomaly guards, malformed-manifest reporting and inserted-legacy-header idempotence. Tests demonstrate byte-exact payload preservation and insertion rollback on complete small fixtures.

Generator uses explicit `tofits(spec_unit='sfu')` in original/open/background modes, already deployed. Reader's physical Jy->SFU division remains unchanged. Model/correction internal values use Jy; correctly labelled SFU input is converted once. `FREQ_CAL` quantities were not relabelled.

## Maintenance exclusion and rollback

Applied while holding the launcher's `/tmp/lwa_spec_daily.lock` using `flock --nonblock`, so the production cron could not concurrently regenerate files through that launcher.

Durable exact-original-span journal:
`/data1/users/pz47/sfu_migration/bunit-sfu-20261004.jsonl`

Logs and final inventory:
`/data1/users/pz47/sfu_migration/{dryrun-final.log,apply.log,verify.log,final-summary.json}`

Fresh read-only verification:
```
python3 /data1/users/pz47/fix_spec_v2_bunit.py --verify \
  /data1/users/pz47/sfu_migration/bunit-sfu-20261004.jsonl
```
Rollback is a PREVIEW unless --apply is explicitly supplied. Do not rollback as normal operation: it would restore wrong/missing labels. If emergency restoration is required, retain originals, acquire the same launcher lock, and use a new rollback journal:
```
python3 /data1/users/pz47/fix_spec_v2_bunit.py --rollback \
  /data1/users/pz47/sfu_migration/bunit-sfu-20261004.jsonl
```

## Separate pre-existing timestamp anomalies

Six files have a corrupt last timestamp and a time-axis reversal:
`20231108.fits`, `20231219.fits`, `20231220.fits`, `20231229.fits`, `20231230.fits`, `20260219.fits`.
Their DATE_END points into 2010 or 2012 instead of their observation year. This was found in both the header and actual UT extension; it is not just a display typo. The cause was not fixed or definitively diagnosed in this unit-only task. These recordings remain unsuitable for monotonic-time processing until separately repaired/filtered from raw data.

Their unit repair used an explicit reviewed manifest bound to the canonical directory, exact filenames and SHA256 of each complete primary header outside BUNIT. The metadata exception relaxes only recording-date consistency for those exact reviewed headers, not format/unit/checksum/corrected-product/identity or payload checks. Hashes are stable across the permitted BUNIT-only replacement. The approval is stored in each journal record so fresh verify/rollback need no general date-bypass option. Tests reject modified headers and foreign/corrected products.

Reviewed manifest: `/data1/users/pz47/sfu_migration/reviewed-date-anomalies-20261004.json`, also included in docs for audit. No timestamps, data samples, filenames or calibration arrays were changed to conceal the problem.

## Scope left outside this migration

Historical PNGs and previously published S3/web copies are not automatically regenerated or rewritten by a local FITS-header repair. Future daily exports are correctly labelled; replication of historical external copies is a separate operation. Absolute calibration, full-day model coverage and K_FAST/MASTER pairing remain separate science gates.
