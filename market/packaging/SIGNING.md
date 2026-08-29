# App Package Signing (release authenticity — TODO #4)

The App Center installer already gives **integrity** (sha256) and **safe unpack**
(zip-slip / tar-bomb vetting). Signing adds **authenticity**: proof a package was
published by the holder of the release private key. Without it, anyone who can
serve a `catalog.json` to the browser could deliver arbitrary root code to the
device. With it, the device refuses any package it cannot trace to the release key.

## Scheme

- **Algorithm:** ECDSA over **P-256 (prime256v1)**, digest **SHA-256**.
- **What is signed:** the raw `<pkg>.tar.gz` bytes (detached signature).
- **Distribution:** base64 of the DER signature, as a `<pkg>.tar.gz.sig` sidecar
  and embedded in `catalog.json` as `package.signature` (+ `signature_alg`).
- **Verification tool:** `openssl dgst -sha256 -verify` — works on the device's
  OpenSSL 1.1.1 **and** the build host's 3.x. **No Python crypto dependency.**

> Why not Ed25519: the device ships OpenSSL 1.1.1, whose `pkeyutl` has no
> `-rawin` (a 3.0 flag), so Ed25519 one-shot CLI verify is unreachable there.
> `openssl dgst` + an EC key is portable across both.

## Keys

| Key | Location | In repo? | On device? |
|-----|----------|----------|------------|
| **Private** `release_priv.pem` | `~/.recamera_release_key/` (chmod 600) | **NO — never** | **NO — never** |
| **Vendor public** `release_pub.pem` | `market/appmgr/keys/release_pub.pem` | yes (committed) | yes (immutable firmware → `/usr/lib/recamera/appmgr/keys/`) |
| **Owner public** `*.pem` | owner provisioning | no | `/userdata/local/appmgr/keys/owners/` |

The private key is the crown jewel. Anyone holding it can publish packages the
whole fleet trusts. Treat it accordingly:

- keep it only under `~/.recamera_release_key/` (or an offline / hardware-backed
  store — override with `RELEASE_KEY_DIR`);
- **never** commit it, scp it to a device, paste it in chat, or bake it into an
  image;
- for production, generate it on an offline machine and consider encrypting the
  key file (`openssl ecparam ... | openssl ec -aes256`);
- if it leaks: generate a new keypair, re-sign every package, and redeploy
  `release_pub.pem` to every device (key rotation = one public-key file swap).

`keygen.sh` refuses to overwrite an existing private key unless you pass
`--force`, precisely because rotation is a fleet-wide event.

## Publisher workflow

```sh
cd market/packaging

# 1. one-time: create the keypair (private stays local, public lands in the repo)
./keygen.sh

# 2. build packages as usual
python3 build.py apps/<id>

# 3. sign every package in dist/  ->  writes dist/<pkg>.tar.gz.sig
python3 sign.py

#    (optional) re-verify the sidecars against the public key
python3 sign.py --verify

# 4. regenerate the catalog; it embeds package.signature from the .sig sidecars
python3 ../catalog/gen_catalog.py
```

`gen_catalog.py` prints a loud `WARN … UNSIGNED` for any package missing a
`.sig`. With the default device policy those packages are **refused**.

## Device policy (`APPMGR_REQUIRE_SIGNATURE`)

Set on the appmgr process (env), default **on**:

- **`1` (default):** an **unsigned** package is refused; a package with a **bad**
  signature is refused.
- **`0`:** unsigned packages are **allowed** (audited as a warning) — a bad
  signature is **still** refused. This is the migration/escape hatch.

The authenticated same-origin local Web v1 upload route has a narrower product
exception that does not require changing this global switch. Its server-minted
preflight returns a critical root-code warning and finalize requires both
the independent permission confirmation and `unsigned_risk_confirmed: true`;
`developer_mode` is an obsolete compatibility field and is not a gate. The
resulting app is installed stopped and needs a separate Start action. Direct/API, cloud and
legacy routes do not inherit that exception. nginx overwrites the internal
route stamp after JWT authentication, so client-provided `source`/`channel`
fields cannot opt into it.

A **present-but-bad** signature is *always* refused, regardless of the switch.
Already-installed apps are never re-verified, so flipping this never bricks a
running device — it only governs new installs.

The immutable vendor-anchor path is overridable via `APPMGR_RELEASE_PUBKEY`
(default `/usr/lib/recamera/appmgr/keys/release_pub.pem`). Device-owner anchors
are direct `*.pem` children of `APPMGR_OWNER_KEYS_DIR` (default
`/userdata/local/appmgr/keys/owners/`) and extend rather than replace the vendor
anchor. Both key files and the owner directory must be regular/no-follow,
owned by appmgr's effective uid, and non-group/world-writable objects. The
verifier caps owner key count and key size, parses every `*.pem` before trying
the signature (one malformed key fails the whole store closed), snapshots each
safely opened key, and reports `signer_kind` plus the
SHA-256 fingerprint of canonical public-key DER. Missing/invalid vendor trust,
unsafe owner-store content, or a signature matching no trusted key fails
closed. App packages may not contain key/trust-store material.
