# Domain checkpoint 2026-11-27

80 host tests passed. DNS resolves correctly. Server-side named TLS passes with project CA, but PC public named handshake is reset. Public certificate still quota-limited. User confirms no ICP/access filing; provider confirmation pending. A/B origin was NOT migrated; existing IP+CA kept. See validation_domain_20261127.md.

# Current test report

[PC/cloud report](validation_pc_20261126.md) and [raw sanitized records](test_records/pc_20261126/unit-tests.txt) are authoritative for this extension.

78 host tests passed in 37.215 s; actual Edge, public C inference, private export, restore and failed upgrade rollback passed. No ESP32 flash or trained-model acceptance claimed.
