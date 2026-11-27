# Domain checkpoint 2026-11-27

80 host tests passed. DNS resolves correctly. Server-side named TLS passes with project CA, but PC public named handshake is reset. Public certificate still quota-limited. User confirms no ICP/access filing; provider confirmation pending. A/B origin was NOT migrated; existing IP+CA kept. See validation_domain_20261127.md.

# Current PC A / cloud C checkpoint

PC A is implemented in desktop_a. Real local HTTP and browser tests passed before deploying C. C runs independently at https://60.204.129.7 with verified project CA TLS. Real PC A -> public C -> local B worker, permission denials, restart persistence, private restore and failing-upgrade rollback passed. 78 host tests passed.

Current local A/B are running on 8766/8765 with previously enrolled ordinary node credentials; no C admin token is saved locally. Original ESP32 remains untouched. See validation_pc_20261126.md and acceptance_pc.md.

Source-only portable package and remote installer are available. Actual MQTT broker, trained SOC/SOH, second server/computer and long-duration acceptance remain pending.
