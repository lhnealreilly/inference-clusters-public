Golden renders taken from the pre-Auto-Mode base (`hyperpod-inference-gateway` 2134097 merged
with `gpu-spot-nodepool` 4055d39, commit 4292ed2). `test_auto_mode.py` asserts that with Auto
Mode off the charts and destroy scripts still render byte-identically to these files. Do not
regenerate them for an Auto-Mode change; regenerate only for an intended non-Auto-Mode change.
