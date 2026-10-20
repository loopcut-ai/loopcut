# Vendored add-on modules

`bundles.py`, `blobs.py`, `cloud_http.py` and `motion_retarget.py` are byte-for-byte copies of the
Loopcut add-on's modules (`scripts/addons_core/loopcut/` in the Loopcut Blender repository,
GPL-3.0-or-later, the same licence as this package), so the connector packs and uploads scenes exactly
as the add-on does (the same dependency collection, canonical manifest numbers and multipart upload
retries that were fixed live on 2026-10-17), and puts motion results on a rig exactly as the add-on's
`apply_motion` does (`loopcut motion-apply` and the `motion_apply` tool run it in a headless Blender
through `loopcut/motion_blender.py`).

Source: add-on development commit `55b1113c5307` (branch `feat/motion-client`).

Do not edit them here. Change the add-on, then copy the files again:

```sh
python3 scripts/sync_addon.py /path/to/loopcut/blender   # copies from that checkout's working tree
```

`tests/test_vendored.py` fails when the copies differ from an add-on checkout named by
`LOOPCUT_ADDON_DIR`, and is skipped without it.

`__init__.py`, `config.py` and `credentials.py` are local stand-ins for the add-on package,
not copies.
