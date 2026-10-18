# Vendored add-on modules

`bundles.py`, `blobs.py` and `cloud_http.py` are byte-for-byte copies of the Loopcut add-on's
modules (`scripts/addons_core/loopcut/` in the Loopcut Blender repository, GPL-3.0-or-later), so the
connector packs and uploads scenes exactly as the add-on does: the same dependency
collection, canonical manifest numbers and multipart upload retries that were fixed live on
2026-10-17.

Source: add-on development commit `fc92f9a4b7c9`.

Do not edit them here. Change the add-on, then copy the files again:

```sh
python3 scripts/sync_addon.py /path/to/loopcut/blender   # copies from that checkout's working tree
```

`tests/test_vendored.py` fails when the copies differ from an add-on checkout named by
`LOOPCUT_ADDON_DIR`, and is skipped without it.

`__init__.py`, `config.py` and `credentials.py` are local stand-ins for the add-on package,
not copies.
