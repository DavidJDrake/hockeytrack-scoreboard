# Release-signing public keys

Each `<key id>.pem` here is the PUBLIC half of an AWS KMS asymmetric key
(`ECC_NIST_P256`, `alias/scoreboard-release-signing`, `terraform/release-signing.tf`)
in SubjectPublicKeyInfo PEM form, exported once by the owner with a read-only
call after the key exists:

    aws kms get-public-key --region us-east-1 \
      --key-id alias/scoreboard-release-signing \
      --output text --query PublicKey | base64 -d \
      | openssl pkey -pubin -inform DER -outform PEM \
      > device/certs/release-signing/release-2026-1.pem

The private half never exists outside KMS: not on the owner's machine, not in
a GitHub secret, not in this repository. `tools/pi-setup.sh --appliance`
installs this directory to `/opt/scoreboard/certs/release-signing/` and the
image gate asserts the installed directory holds exactly these files, byte
for byte, and that none of them is a private key.

The panel (SCO-68) verifies a release manifest's exact bytes against EVERY
key here before parsing it, and checks the manifest's `keyId` afterwards for
consistency. Rotation is therefore a keyring, not a swap: add the new key's
`.pem` beside the old one, ship a release signed by the old key that carries
both, sign the next with the new key, then drop the old file. The publish job
checks KMS's signature against the copy of this directory carried in the
build artifact, so a swapped file here fails the release, never the fleet.

Until the first key is exported this directory holds only this file, and the
publish job refuses to release: a manifest no panel could verify is not a
release.
