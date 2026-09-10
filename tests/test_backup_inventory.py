from scripts.backup_inventory import _payload


class Storage:
    def __init__(self):
        self.calls = []

    def read(self, key, version_id):
        self.calls.append(("screenshot", key, version_id))
        return type("Content", (), {"data": b"image"})()

    def read_blob(self, key, version_id):
        self.calls.append(("blob", key, version_id))
        return b"document"


def test_inventory_reads_exact_referenced_object_versions():
    storage = Storage()
    screenshot = _payload(
        storage,
        {
            "kind": "screenshot",
            "key": "device/capture.jpg",
            "version_id": "image-v2",
        },
    )
    document = _payload(
        storage,
        {
            "kind": "invoice",
            "key": "invoices/one.dfenc",
            "version_id": "invoice-v4",
        },
    )

    assert screenshot == b"image"
    assert document == b"document"
    assert storage.calls == [
        ("screenshot", "device/capture.jpg", "image-v2"),
        ("blob", "invoices/one.dfenc", "invoice-v4"),
    ]
