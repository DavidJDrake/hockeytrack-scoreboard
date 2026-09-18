// The download page: shows the current scoreboard image from the mirror's
// latest.json, and the commands that verify it before it is flashed. Nothing
// here builds markup from strings; every value is set as text or an
// attribute, and the manifest is used only in the exact shape a release
// writes, because the mirror is a copy that could be tampered with.
export const MANIFEST_URL = "https://images.scoreboard.davidjdrake.com/latest.json";
const IMAGES_BASE = "https://images.scoreboard.davidjdrake.com/images/";
const REPO = "DavidJDrake/hockeytrack-scoreboard";
const VERSION = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
const HEX64 = /^[0-9a-f]{64}$/;

export function validManifest(m) {
  return m !== null && typeof m === "object"
    && typeof m.version === "string" && VERSION.test(m.version)
    && m.file === `scoreboard-${m.version}.img.xz`
    && typeof m.sha256 === "string" && HEX64.test(m.sha256)
    && Number.isSafeInteger(m.size) && m.size > 0;
}

export function imageUrl(m) {
  return `${IMAGES_BASE}${m.version}/${m.file}`;
}

export function formatSize(bytes) {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

// Every command below is built only from validManifest's fields: a version
// matching VERSION, a file name derived from it, and a 64-hex checksum.

// Against the mirror's own latest.json: proves the download is intact, not
// where it came from, since the checksum and the image share a host.
export function verifyChecksumCommand(m) {
  return `echo "${m.sha256}  ${m.file}" | sha256sum -c -`;
}

// Against the checksum published with the GitHub Release, fetched from GitHub.
export function verifyReleaseChecksumCommand(m) {
  return `gh release download ${m.version} --repo ${REPO} --pattern '${m.file}.sha256'`
    + ` && sha256sum -c ${m.file}.sha256`;
}

// Origin: signed by this repository's release workflow, for this version's tag.
// --repo alone would accept an attestation from any workflow or ref in it.
export function verifyAttestationCommand(m) {
  return `gh attestation verify ${m.file} --repo ${REPO}`
    + ` --signer-workflow ${REPO}/.github/workflows/image.yml`
    + ` --source-ref refs/tags/${m.version}`;
}

export async function loadManifest(fetchImpl = fetch) {
  const resp = await fetchImpl(MANIFEST_URL, { cache: "no-store", credentials: "omit" });
  if (!resp.ok) throw new Error(`manifest: status ${resp.status}`);
  const m = await resp.json();
  if (!validManifest(m)) throw new Error("manifest: unexpected shape");
  return m;
}

export function render(doc, m) {
  doc.getElementById("image-version").textContent = m.version;
  doc.getElementById("image-size").textContent = formatSize(m.size);
  doc.getElementById("image-sha256").textContent = m.sha256;
  const link = doc.getElementById("image-link");
  link.href = imageUrl(m);
  link.textContent = `Download ${m.file}`;
  doc.getElementById("verify-sha").textContent = verifyChecksumCommand(m);
  doc.getElementById("verify-release-sha").textContent = verifyReleaseChecksumCommand(m);
  doc.getElementById("verify-attest").textContent = verifyAttestationCommand(m);
  doc.getElementById("image-details").hidden = false;
  doc.getElementById("image-download").hidden = false;
  doc.getElementById("image-status").textContent = `The current image is ${m.version}.`;
}

export function renderUnavailable(doc) {
  doc.getElementById("image-status").textContent =
    "The current image could not be loaded. Every release is also listed on GitHub, linked below.";
}

if (typeof document !== "undefined") {
  loadManifest().then((m) => render(document, m), () => renderUnavailable(document));
}
