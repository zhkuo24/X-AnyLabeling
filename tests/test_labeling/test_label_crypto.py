import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anylabeling.label_crypto import (
    ENCRYPTED_SUFFIX,
    HEADER_LEN,
    KEY_LEN,
    MAGIC,
    MISSING_KEY_MESSAGE,
    LabelCryptoError,
    clear_key_cache,
    configure_key_file,
    decrypt_bytes,
    decrypt_main,
    encrypt_bytes,
    encrypt_main,
    generate_key_file,
    keygen_main,
    load_key_file,
    metadata_path,
    split_envelope,
)
from anylabeling.views.labeling.label_file import (
    LabelFile,
    LabelFileError,
    serialize_label,
)


def _sample_shapes():
    return [
        {
            "label": "lesion",
            "points": [[10, 5], [30, 5], [30, 20], [10, 20]],
            "group_id": None,
            "description": "病灶",
            "shape_type": "rectangle",
            "flags": {},
            "mask": None,
        }
    ]


class TestLabelCrypto(unittest.TestCase):
    def setUp(self):
        clear_key_cache()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xal-crypto-")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(clear_key_cache)

    def _key(self, name="project.key"):
        return generate_key_file(os.path.join(self.temp_dir.name, name))

    def test_round_trip_and_metadata_omit_key_bytes(self):
        material = self._key()
        self.assertEqual(len(material.key), KEY_LEN)
        self.assertEqual(len(Path(material.path).read_bytes()), KEY_LEN)
        meta_text = Path(metadata_path(material.path)).read_text(
            encoding="utf-8"
        )
        meta = json.loads(meta_text)
        self.assertEqual(set(meta), {"key_id", "created_at"})
        self.assertEqual(meta["key_id"], material.key_id.hex())
        self.assertNotIn(material.key.hex(), meta_text)

        plaintext = '{"shapes":[{"label":"病灶"}]}'.encode("utf-8")
        blob = encrypt_bytes(plaintext, material)
        self.assertTrue(blob.startswith(MAGIC))
        parts = split_envelope(blob)
        self.assertEqual(parts.version, 1)
        self.assertEqual(parts.key_id, material.key_id)
        self.assertEqual(len(parts.nonce), 12)
        self.assertGreaterEqual(len(blob), HEADER_LEN + 16)
        self.assertEqual(decrypt_bytes(blob, material), plaintext)
        self.assertNotEqual(
            encrypt_bytes(plaintext, material),
            encrypt_bytes(plaintext, material),
        )

    def test_wrong_key_and_bad_tag_fail_closed(self):
        material = self._key("a.key")
        other = self._key("b.key")
        blob = encrypt_bytes(b'{"ok":true}', material)
        with self.assertRaises(LabelCryptoError) as wrong_key:
            decrypt_bytes(blob, other)
        self.assertNotIn(b'{"ok":true}'.decode(), str(wrong_key.exception))

        mutated = bytearray(blob)
        mutated[-1] ^= 0x01
        with self.assertRaises(LabelCryptoError) as bad_tag:
            decrypt_bytes(bytes(mutated), material)
        self.assertIn("authentication failed", str(bad_tag.exception))

        short = blob[:10]
        with self.assertRaises(LabelCryptoError):
            decrypt_bytes(short, material)

    def test_cli_keygen_encrypt_and_batch_decrypt(self):
        root = Path(self.temp_dir.name)
        key_path = root / "project.key"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(keygen_main(["-o", str(key_path)]), 0)
        material = load_key_file(str(key_path))
        self.assertEqual(stdout.getvalue().strip(), material.key_id.hex())

        plain_dir = root / "plain"
        enc_dir = root / "labels"
        out_dir = root / "restored"
        plain_dir.mkdir()
        first = {"imagePath": "a.png", "shapes": [{"label": "cat"}]}
        second = {"imagePath": "b.png", "shapes": []}
        (plain_dir / "a.json").write_text(json.dumps(first), encoding="utf-8")
        nested = plain_dir / "nested"
        nested.mkdir()
        (nested / "b.json").write_text(json.dumps(second), encoding="utf-8")

        single_out = root / "one.xalenc"
        self.assertEqual(
            encrypt_main(
                [
                    "-k",
                    str(key_path),
                    str(plain_dir / "a.json"),
                    "-o",
                    str(single_out),
                ]
            ),
            0,
        )
        self.assertTrue(single_out.read_bytes().startswith(MAGIC))
        single_plain = root / "one.json"
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    str(key_path),
                    str(single_out),
                    "-o",
                    str(single_plain),
                ]
            ),
            0,
        )
        self.assertEqual(
            json.loads(single_plain.read_text(encoding="utf-8")), first
        )

        enc_dir.mkdir()
        (enc_dir / "nested").mkdir()
        encrypt_bytes_a = encrypt_bytes(
            (plain_dir / "a.json").read_bytes(), material
        )
        encrypt_bytes_b = encrypt_bytes(
            (nested / "b.json").read_bytes(), material
        )
        (enc_dir / "a.xalenc").write_bytes(encrypt_bytes_a)
        (enc_dir / "nested" / "b.xalenc").write_bytes(encrypt_bytes_b)
        self.assertEqual(
            decrypt_main(
                ["-k", str(key_path), str(enc_dir), "-o", str(out_dir)]
            ),
            0,
        )
        self.assertEqual(
            json.loads((out_dir / "a.json").read_text(encoding="utf-8")),
            first,
        )
        self.assertEqual(
            json.loads(
                (out_dir / "nested" / "b.json").read_text(encoding="utf-8")
            ),
            second,
        )
        wrong_key = self._key("nope.key")
        rejected = root / "nope.json"
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    str(wrong_key.path),
                    str(single_out),
                    "-o",
                    str(rejected),
                ]
            ),
            1,
        )
        self.assertFalse(rejected.exists())

    def test_tool_script_prints_key_id(self):
        key_path = Path(self.temp_dir.name) / "from-tool.key"
        repo = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [
                sys.executable,
                str(repo / "tools" / "xlabel_keygen.py"),
                "-o",
                str(key_path),
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        material = load_key_file(str(key_path))
        self.assertEqual(result.stdout.strip(), material.key_id.hex())
        self.assertNotIn(material.key.hex(), result.stdout + result.stderr)


class TestLabelFileEncryption(unittest.TestCase):
    def setUp(self):
        clear_key_cache()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xal-labelfile-")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(clear_key_cache)

    def _image(self):
        from PIL import Image

        path = os.path.join(self.temp_dir.name, "image.png")
        Image.new("RGB", (2, 3), "white").save(path)
        return path

    def _save(self, filename, shapes=None):
        label = LabelFile()
        label.save(
            filename=filename,
            shapes=shapes if shapes is not None else _sample_shapes(),
            image_path="image.png",
            image_height=3,
            image_width=2,
            other_data={"description": "note"},
            flags={"reviewed": True},
        )
        return label

    def test_save_without_key_refuses_plaintext(self):
        filename = os.path.join(self.temp_dir.name, "image.json")
        with self.assertRaises(LabelFileError) as caught:
            self._save(filename)
        self.assertIn("Refusing to write plaintext", str(caught.exception))
        self.assertIn(MISSING_KEY_MESSAGE, str(caught.exception))
        self.assertEqual(os.listdir(self.temp_dir.name), [])

    def test_save_writes_xalenc_and_load_recovers_shapes(self):
        material = generate_key_file(
            os.path.join(self.temp_dir.name, "project.key")
        )
        configure_key_file(material.path)
        image_path = self._image()
        requested = os.path.splitext(image_path)[0] + ".json"
        legacy = requested
        Path(legacy).write_text('{"stale":true}\n', encoding="utf-8")
        label = self._save(requested)
        encrypted = os.path.splitext(image_path)[0] + ENCRYPTED_SUFFIX
        self.assertEqual(label.filename, encrypted)
        self.assertTrue(os.path.isfile(encrypted))
        self.assertFalse(os.path.exists(legacy))
        raw = Path(encrypted).read_bytes()
        self.assertTrue(raw.startswith(MAGIC))
        payload = json.loads(decrypt_bytes(raw, material).decode("utf-8"))
        self.assertEqual(payload["shapes"][0]["label"], "lesion")
        self.assertEqual(payload["description"], "note")
        self.assertEqual(payload["flags"]["reviewed"], True)
        self.assertEqual(
            decrypt_bytes(raw, material),
            serialize_label(payload),
        )

        loaded = LabelFile(encrypted, os.path.dirname(image_path))
        self.assertEqual(len(loaded.shapes), 1)
        self.assertEqual(loaded.shapes[0].label, "lesion")
        self.assertEqual(loaded.shapes[0].shape_type, "rectangle")
        self.assertEqual(len(loaded.shapes[0].points), 4)
        self.assertEqual(loaded.other_data["description"], "note")
        self.assertTrue(loaded.flags["reviewed"])

    def test_legacy_json_still_loads_when_key_is_configured(self):
        material = generate_key_file(
            os.path.join(self.temp_dir.name, "project.key")
        )
        configure_key_file(material.path)
        image_path = self._image()
        label_path = os.path.splitext(image_path)[0] + ".json"
        document = {
            "version": "test",
            "flags": {"reviewed": True},
            "shapes": _sample_shapes(),
            "imagePath": "image.png",
            "imageData": None,
            "imageHeight": 3,
            "imageWidth": 2,
            "description": "legacy",
        }
        Path(label_path).write_text(json.dumps(document), encoding="utf-8")
        loaded = LabelFile(label_path)
        self.assertEqual(loaded.shapes[0].label, "lesion")
        self.assertEqual(loaded.other_data["description"], "legacy")

    def test_encrypted_load_without_key_or_with_wrong_key_fails(self):
        material = generate_key_file(
            os.path.join(self.temp_dir.name, "project.key")
        )
        configure_key_file(material.path)
        image_path = self._image()
        encrypted = os.path.splitext(image_path)[0] + ENCRYPTED_SUFFIX
        self._save(encrypted)
        clear_key_cache()
        with self.assertRaises(LabelFileError) as missing:
            LabelFile(encrypted)
        self.assertIn("not configured", str(missing.exception))

        other = generate_key_file(
            os.path.join(self.temp_dir.name, "other.key")
        )
        configure_key_file(other.path)
        with self.assertRaises(LabelFileError) as wrong:
            LabelFile(encrypted)
        self.assertIn("different key", str(wrong.exception))


if __name__ == "__main__":
    unittest.main()
