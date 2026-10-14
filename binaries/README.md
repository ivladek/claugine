# Prebuilt binaries

The files are too big for git (GitHub limit - 100 MB per file); they are kept on Google Drive,
readable by anyone with the link:

**https://drive.google.com/drive/folders/1zob0ymE0H3qIcJWZi0cEyRH8h62q3lm3?usp=sharing**

Download them into this directory - git ignores everything here except this file.

| file | what | sha256 |
|---|---|---|
| `vyos-2026.03-generic-amd64-claugine.iso` | VyOS Stream 2026.03 ISO with the claugine scripts in `/claugine/` - the ISO of `vyos_image_vm_create` | `88321e6d346fa2e12bbb5c921919128fde4a98d9e98abbf09e896b674104b106` |
| `vyos-2026.03.qcow2` | the VyOS image `VyOS Router 2026.03` - the local copy of `vyos_image_to_platforms`, configured by `vyos-image-prepare` | `04f7c624940c59fef8366bbcfb522e5b98a6aa6c1c7433b3bd14ce8c1e3177b9` |

Check a downloaded file:

```bash
sha256sum binaries/<file>          # macOS: shasum -a 256 binaries/<file>
```

Put the image on a platform:

```bash
image_upload platform=platforms.dc1.payload1 file=binaries/vyos-2026.03.qcow2 name="VyOS Router 2026.03" type=OS
```

See [VyOS image](../docs/network/vyos-image.md#prebuilt-files).
