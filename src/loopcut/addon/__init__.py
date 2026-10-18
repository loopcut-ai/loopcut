"""Verbatim copies of the Loopcut add-on's bundle, blob and HTTP modules; see VENDORED.md.

The copies import bl_info, config and credentials from their package, so this package
supplies small stand-ins. The loopcut package always passes cloud_http.Client an api_key and
client_headers, so cloud_http never reads Blender or the add-on's stored key.
"""

bl_info = {"name": "Loopcut connector", "version": (0, 1, 0)}
