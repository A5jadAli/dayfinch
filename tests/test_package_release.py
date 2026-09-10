import hashlib
import subprocess
import sys
from pathlib import Path


def test_package_release_renames_and_hashes_binary(tmp_path):
    source = tmp_path / "Dayfinch-Agent"
    source.write_bytes(b"portable-agent-binary")
    target = tmp_path / "release" / "dayfinch-agent-linux-x64"

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "package_release.py"),
            str(source),
            str(target),
        ],
        check=True,
    )

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert target.read_bytes() == source.read_bytes()
    assert target.with_name(f"{target.name}.sha256").read_text() == (
        f"{expected}  {target.name}\n"
    )
