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
    require_key,
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

    def _wrap(self, master, dek, name="annotator.xalk"):
        from anylabeling.label_crypto import wrap_credential

        output = os.path.join(self.temp_dir.name, name)
        wrap_credential(
            master.path,
            output,
            "annotator-passphrase",
            dek_path=dek.path,
        )
        return output

    def test_cli_keygen_encrypt_and_batch_decrypt(self):
        from anylabeling.label_crypto import split_envelope

        root = Path(self.temp_dir.name)
        master_path = root / "master.key"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(keygen_main(["-o", str(master_path)]), 0)
        master = load_key_file(str(master_path))
        self.assertEqual(stdout.getvalue().strip(), master.key_id.hex())
        dek = self._key("project.key")
        wrapped = self._wrap(master, dek)

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
                    str(master_path),
                    str(plain_dir / "a.json"),
                    "-o",
                    str(single_out),
                ]
            ),
            0,
        )
        blob = single_out.read_bytes()
        self.assertTrue(blob.startswith(MAGIC))
        self.assertEqual(split_envelope(blob).key_id, dek.key_id)
        self.assertNotEqual(split_envelope(blob).key_id, master.key_id)
        single_plain = root / "one.json"
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    str(master_path),
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
        (enc_dir / "a.xalenc").write_bytes(
            encrypt_bytes((plain_dir / "a.json").read_bytes(), dek)
        )
        (enc_dir / "nested" / "b.xalenc").write_bytes(
            encrypt_bytes((nested / "b.json").read_bytes(), dek)
        )
        self.assertEqual(
            decrypt_main(
                ["-k", str(master_path), str(enc_dir), "-o", str(out_dir)]
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
        rejected = root / "nope.json"
        self.assertEqual(
            decrypt_main(
                ["-k", dek.path, str(single_out), "-o", str(rejected)]
            ),
            1,
        )
        self.assertFalse(rejected.exists())
        os.environ["XLABEL_PASSPHRASE"] = "annotator-passphrase"
        self.addCleanup(os.environ.pop, "XLABEL_PASSPHRASE", None)
        credential_out = root / "from-credential.json"
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    wrapped,
                    str(single_out),
                    "-o",
                    str(credential_out),
                ]
            ),
            1,
        )
        self.assertFalse(credential_out.exists())
        wrong_master = self._key("nope.key")
        wrong_out = root / "wrong-master.json"
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    wrong_master.path,
                    str(single_out),
                    "-o",
                    str(wrong_out),
                ]
            ),
            1,
        )
        self.assertFalse(wrong_out.exists())

    def test_failed_key_load_is_retried(self):
        key_path = os.path.join(self.temp_dir.name, "later.key")
        with self.assertRaises(LabelCryptoError):
            configure_key_file(key_path)
        with self.assertRaises(LabelCryptoError):
            require_key()

        material = generate_key_file(key_path)
        loaded = configure_key_file(key_path)
        self.assertEqual(loaded.key, material.key)

        clear_key_cache()
        appears = os.path.join(self.temp_dir.name, "appears.key")
        with self.assertRaises(LabelCryptoError):
            configure_key_file(appears)
        os.rename(key_path, appears)
        self.assertEqual(require_key().key_id, material.key_id)

    def test_successful_key_stays_cached_until_forced(self):
        material = self._key("cached.key")
        configure_key_file(material.path)
        Path(material.path).write_bytes(os.urandom(KEY_LEN))
        cached = configure_key_file(material.path)
        self.assertEqual(cached.key, material.key)
        forced = configure_key_file(material.path, force=True)
        self.assertEqual(len(forced.key), KEY_LEN)
        self.assertNotEqual(forced.key, material.key)

    def test_decrypt_and_encrypt_refuse_to_overwrite(self):
        root = Path(self.temp_dir.name)
        master = self._key("master.key")
        dek = self._key("dek.key")
        self._wrap(master, dek)
        source = root / "plain.json"
        source.write_text('{"keep":true}', encoding="utf-8")
        encrypted = root / "plain.xalenc"
        self.assertEqual(
            encrypt_main(
                ["-k", master.path, str(source), "-o", str(encrypted)]
            ),
            0,
        )
        self.assertEqual(
            encrypt_main(
                ["-k", master.path, str(source), "-o", str(encrypted)]
            ),
            1,
        )
        original = encrypted.read_bytes()
        destination = root / "plain-out.json"
        destination.write_text("leave-me", encoding="utf-8")
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    master.path,
                    str(encrypted),
                    "-o",
                    str(destination),
                ]
            ),
            1,
        )
        self.assertEqual(destination.read_text(encoding="utf-8"), "leave-me")
        self.assertEqual(encrypted.read_bytes(), original)

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

    def test_private_replace_succeeds_without_fchmod(self):
        import errno

        from anylabeling import label_crypto

        target = os.path.join(self.temp_dir.name, "keystore.bin")
        payload = b"wrapped-keystore-not-a-raw-key"
        with mock.patch.object(label_crypto.os, "fchmod", None, create=True):
            self.assertIsNone(getattr(label_crypto.os, "fchmod", None))
            label_crypto._write_private_replace(target, payload)

        self.assertEqual(Path(target).read_bytes(), payload)
        self.assertEqual(list(Path(self.temp_dir.name).glob(".xal_*.tmp")), [])
        mode = os.stat(target).st_mode
        if os.name == "nt":
            import stat

            self.assertTrue(mode & stat.S_IWRITE)
        else:
            self.assertEqual(mode & 0o777, 0o600)

        leaked = []

        def fail_fchmod(fd, mode):
            leaked.append(fd)
            raise AttributeError("module 'os' has no attribute 'fchmod'")

        missed = os.path.join(self.temp_dir.name, "not-written.bin")
        with mock.patch.object(
            label_crypto.os, "fchmod", fail_fchmod, create=True
        ):
            with self.assertRaises(AttributeError):
                label_crypto._write_private_replace(missed, b"secret")
        self.assertFalse(os.path.exists(missed))
        self.assertEqual(list(Path(self.temp_dir.name).glob(".xal_*.tmp")), [])
        self.assertEqual(len(leaked), 1)
        with self.assertRaises(OSError) as closed:
            os.read(leaked[0], 1)
        self.assertEqual(closed.exception.errno, errno.EBADF)


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
        self.assertIn(MISSING_KEY_MESSAGE, str(missing.exception))

        other = generate_key_file(
            os.path.join(self.temp_dir.name, "other.key")
        )
        configure_key_file(other.path)
        with self.assertRaises(LabelFileError) as wrong:
            LabelFile(encrypted)
        self.assertIn("different key", str(wrong.exception))


class TestReviewFollowups(unittest.TestCase):
    def setUp(self):
        clear_key_cache()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xal-review-")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(clear_key_cache)

    def test_file_list_marks_decrypt_failure(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from anylabeling.views.labeling.label_widget import LabelingWidget

        material = generate_key_file(
            os.path.join(self.temp_dir.name, "project.key")
        )
        plain = json.dumps({"checked": True, "shapes": []}).encode("utf-8")
        encrypted = os.path.join(self.temp_dir.name, "frame.xalenc")
        Path(encrypted).write_bytes(encrypt_bytes(plain, material))
        legacy = os.path.join(self.temp_dir.name, "frame.json")
        Path(legacy).write_text(plain.decode("utf-8"), encoding="utf-8")
        missing = os.path.join(self.temp_dir.name, "absent.xalenc")
        clear_key_cache()
        host = object()
        self.assertFalse(LabelingWidget._label_file_checked(host, missing))
        self.assertEqual(
            LabelingWidget._label_file_checked(host, encrypted), "error"
        )
        self.assertTrue(LabelingWidget._label_file_checked(host, legacy))
        configure_key_file(material.path)
        self.assertTrue(LabelingWidget._label_file_checked(host, encrypted))

    def test_vqa_batch_reports_write_failure(self):
        from anylabeling.views.labeling.widgets.vqa_dialog import (
            VQADialog,
            update_label_document,
        )

        material = generate_key_file(
            os.path.join(self.temp_dir.name, "project.key")
        )
        document = {"vqaData": {}, "shapes": []}
        encrypted = os.path.join(self.temp_dir.name, "frame.xalenc")
        original = encrypt_bytes(
            json.dumps(document).encode("utf-8"), material
        )
        Path(encrypted).write_bytes(original)
        clear_key_cache()

        error = update_label_document(
            encrypted,
            lambda data: data["vqaData"].__setitem__("q", "a") or True,
        )
        self.assertIsNotNone(error)
        self.assertIn(encrypted, error)
        self.assertEqual(Path(encrypted).read_bytes(), original)

        class Host:
            def parent(self):
                return mock.Mock(
                    output_dir=self_dir,
                    _labels_in_directory=lambda _directory: [encrypted],
                )

            def tr(self, text):
                return text

        self_dir = self.temp_dir.name
        host = Host()
        host._report_label_write_failures = (
            lambda errors: VQADialog._report_label_write_failures(host, errors)
        )
        with mock.patch(
            "anylabeling.views.labeling.widgets.vqa_dialog.QMessageBox.critical"
        ) as critical:
            VQADialog.apply_to_all_label_files(
                host,
                lambda label_file: VQADialog.add_component_to_json_file(
                    host, label_file, "question", "yes"
                ),
            )
        critical.assert_called_once()
        self.assertIn("1", critical.call_args.args[2])
        self.assertIn("unchanged", critical.call_args.args[2])
        self.assertEqual(Path(encrypted).read_bytes(), original)


class TestEnvelopeRoles(unittest.TestCase):
    def setUp(self):
        clear_key_cache()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xal-roles-")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(clear_key_cache)
        self.passphrase = "annotator-passphrase"

    def _pair(self):
        root = self.temp_dir.name
        from anylabeling.label_crypto import wrap_credential

        master = generate_key_file(os.path.join(root, "master.key"))
        dek = generate_key_file(os.path.join(root, "project.key"))
        credential = os.path.join(root, "annotator.xalk")
        wrap_credential(
            master.path,
            credential,
            self.passphrase,
            dek_path=dek.path,
        )
        return master, dek, credential

    def test_legacy_xalenc_decrypts_with_master_not_raw_dek(self):
        from anylabeling.label_crypto import (
            keystore_path,
            split_envelope,
            wrap_main,
        )

        root = Path(self.temp_dir.name)
        legacy = generate_key_file(str(root / "project.key"))
        plaintext = '{"shapes":[{"label":"旧标注"}]}'.encode("utf-8")
        encrypted = root / "frame.xalenc"
        encrypted.write_bytes(encrypt_bytes(plaintext, legacy))
        self.assertEqual(split_envelope(encrypted.read_bytes()).key_id, legacy.key_id)

        master_path = root / "master.key"
        self.assertEqual(keygen_main(["-o", str(master_path)]), 0)
        master = load_key_file(str(master_path))
        passphrase_file = root / "pass.txt"
        passphrase_file.write_text(self.passphrase + "\n", encoding="utf-8")
        credential = root / "annotator.xalk"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(
                wrap_main(
                    [
                        "--master",
                        str(master_path),
                        "--dek",
                        legacy.path,
                        "-o",
                        str(credential),
                        "--passphrase-file",
                        str(passphrase_file),
                    ]
                ),
                0,
            )
        self.assertEqual(stdout.getvalue().strip(), legacy.key_id.hex())
        stored = credential.read_bytes()
        store = Path(keystore_path(str(master_path))).read_bytes()
        for secret in (legacy.key, master.key, self.passphrase.encode("utf-8")):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret.hex().encode("ascii"), stored)
            self.assertNotIn(secret, store)
            self.assertNotIn(secret.hex().encode("ascii"), store)

        restored = root / "frame.json"
        self.assertEqual(
            decrypt_main(
                ["-k", str(master_path), str(encrypted), "-o", str(restored)]
            ),
            0,
        )
        self.assertEqual(restored.read_bytes(), plaintext)
        os.remove(restored)
        self.assertEqual(
            decrypt_main(
                [
                    "-k",
                    str(master_path),
                    "--credential",
                    str(credential),
                    str(encrypted),
                    "-o",
                    str(restored),
                ]
            ),
            0,
        )
        self.assertEqual(restored.read_bytes(), plaintext)

        self.assertEqual(
            decrypt_main(
                ["-k", legacy.path, str(encrypted), "-o", str(root / "raw.json")]
            ),
            1,
        )
        self.assertFalse((root / "raw.json").exists())
        self.assertEqual(
            wrap_main(
                [
                    "--master",
                    str(master_path),
                    "--dek",
                    legacy.path,
                    "-o",
                    str(credential),
                    "--passphrase-file",
                    str(passphrase_file),
                ]
            ),
            1,
        )
        self.assertTrue(credential.is_file())

    def test_annotator_edits_labels_and_cannot_export_or_cli_decrypt(self):
        from anylabeling.label_crypto import (
            ROLE_ANNOTATOR,
            ROLE_ALGORITHM,
            TrainingExportDenied,
            configure_credential,
            configure_from_config,
            configure_master,
            current_role,
            training_export_allowed,
        )
        from anylabeling.views.labeling.label_converter import LabelConverter
        from anylabeling.views.labeling.utils.export import (
            export_yolo_annotation,
        )

        master, dek, credential = self._pair()
        os.remove(dek.path)
        os.remove(master.path)
        configure_credential(credential, self.passphrase)
        self.assertEqual(current_role(), ROLE_ANNOTATOR)
        self.assertFalse(training_export_allowed())

        from PIL import Image

        Image.new("RGB", (64, 48), "white").save(
            os.path.join(self.temp_dir.name, "image.png")
        )
        label_path = os.path.join(self.temp_dir.name, "image.json")
        label = LabelFile()
        shapes = _sample_shapes()
        label.save(
            filename=label_path,
            shapes=shapes,
            image_path="image.png",
            image_height=3,
            image_width=2,
            other_data={"description": "note"},
            flags={"reviewed": True},
        )
        encrypted = os.path.splitext(label_path)[0] + ENCRYPTED_SUFFIX
        blob = Path(encrypted).read_bytes()
        self.assertEqual(split_envelope(blob).key_id, dek.key_id)
        clear_key_cache()
        configure_credential(credential, self.passphrase)
        loaded = LabelFile(encrypted)
        self.assertEqual(loaded.shapes[0].label, "lesion")

        converter = LabelConverter()
        blocked = os.path.join(self.temp_dir.name, "blocked.txt")
        with self.assertRaises(TrainingExportDenied):
            converter.custom_to_yolo(encrypted, blocked, "hbb")
        self.assertFalse(os.path.exists(blocked))
        for name, args in (
            ("custom_to_voc", ("img.png", encrypted, self.temp_dir.name, "rectangle")),
            ("custom_to_coco", (["img.png"], self.temp_dir.name, self.temp_dir.name, "rectangle")),
            ("custom_to_dota", (encrypted, blocked)),
            ("custom_to_mask", (encrypted, blocked, {})),
            ("custom_to_mot", (self.temp_dir.name, self.temp_dir.name)),
            ("custom_to_mots", (self.temp_dir.name, self.temp_dir.name)),
            ("custom_to_odvg", (["img.png"], self.temp_dir.name, self.temp_dir.name)),
            ("custom_to_vlm_r1_ovd", (["img.png"], self.temp_dir.name, blocked)),
            ("custom_to_ppocr", ("img.png", encrypted, self.temp_dir.name, "rec")),
        ):
            with self.assertRaises(TrainingExportDenied):
                getattr(converter, name)(*args)

        class Host:
            def tr(self, text):
                return text

        with mock.patch(
            "anylabeling.views.labeling.utils.export.Popup"
        ) as popup:
            export_yolo_annotation(Host(), "hbb")
            from anylabeling.views.labeling.widgets.overview_dialog import (
                OverviewDialog,
            )

            with mock.patch(
                "anylabeling.views.labeling.widgets.overview_dialog."
                "QFileDialog.getExistingDirectory",
                side_effect=AssertionError("export dialog"),
            ):
                OverviewDialog.export_to_csv(Host())
        self.assertGreaterEqual(popup.call_count, 2)
        self.assertIn("annotator", popup.call_args.args[0])

        clear_key_cache()
        fresh_master = generate_key_file(
            os.path.join(self.temp_dir.name, "fake-master.key")
        )
        configure_from_config(
            {
                "label_master_key_file": fresh_master.path,
                "label_credential_file": credential,
            },
            passphrase=self.passphrase,
        )
        self.assertEqual(current_role(), ROLE_ANNOTATOR)
        self.assertFalse(training_export_allowed())

        clear_key_cache()
        from anylabeling.label_crypto import wrap_credential

        real_master = generate_key_file(
            os.path.join(self.temp_dir.name, "real-master.key")
        )
        legacy_copy = os.path.join(self.temp_dir.name, "project-again.key")
        Path(legacy_copy).write_bytes(dek.key)
        os.chmod(legacy_copy, 0o600)
        wrap_credential(
            real_master.path,
            os.path.join(self.temp_dir.name, "algorithm.xalk"),
            self.passphrase,
            dek_path=legacy_copy,
        )
        os.remove(legacy_copy)
        configure_master(real_master.path)
        self.assertEqual(current_role(), ROLE_ALGORITHM)
        self.assertTrue(training_export_allowed())
        allowed = os.path.join(self.temp_dir.name, "allowed.txt")
        converter.custom_to_yolo(encrypted, allowed, "hbb")
        self.assertTrue(os.path.isfile(allowed))

    def test_unconfigured_session_cannot_export(self):
        from anylabeling.label_crypto import (
            current_role,
            training_export_allowed,
        )
        from anylabeling.label_crypto import TrainingExportDenied
        from anylabeling.views.labeling.label_converter import LabelConverter

        clear_key_cache()
        self.assertIsNone(current_role())
        self.assertFalse(training_export_allowed())
        blocked = os.path.join(self.temp_dir.name, "unconfigured.txt")
        with self.assertRaises(TrainingExportDenied):
            LabelConverter().custom_to_yolo(blocked, blocked, "hbb")
        self.assertFalse(os.path.exists(blocked))

    def test_settings_reject_raw_key_and_legacy_path(self):
        from anylabeling.label_crypto import (
            LEGACY_KEY_MESSAGE,
            RAW_KEY_AS_CREDENTIAL,
            configure_credential,
            configure_from_config,
        )

        raw = generate_key_file(os.path.join(self.temp_dir.name, "project.key"))
        with self.assertRaises(LabelCryptoError) as caught:
            configure_credential(raw.path, self.passphrase)
        self.assertIn("raw 32-byte", str(caught.exception))
        self.assertIn(RAW_KEY_AS_CREDENTIAL, str(caught.exception))
        with self.assertRaises(LabelCryptoError) as legacy:
            configure_from_config({"label_key_file": raw.path})
        self.assertIn(LEGACY_KEY_MESSAGE, str(legacy.exception))
        self.assertIsNone(
            configure_from_config(
                {
                    "label_key_file": None,
                    "label_credential_file": None,
                    "label_master_key_file": None,
                }
            )
        )

    def test_wrong_passphrase_fails_closed(self):
        from anylabeling.label_crypto import configure_credential

        _master, _dek, credential = self._pair()
        with self.assertRaises(LabelCryptoError) as caught:
            configure_credential(credential, "not-the-passphrase")
        self.assertIn("authentication failed", str(caught.exception))
        self.assertNotIn(self.passphrase, str(caught.exception))

    def test_wrap_tool_script_prints_dek_id(self):
        root = Path(self.temp_dir.name)
        master = root / "master.key"
        dek = root / "project.key"
        credential = root / "annotator.xalk"
        passphrase = root / "pass.txt"
        repo = Path(__file__).resolve().parents[2]
        self.assertEqual(
            subprocess.run(
                [
                    sys.executable,
                    str(repo / "tools" / "xlabel_keygen.py"),
                    "-o",
                    str(master),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(
            subprocess.run(
                [
                    sys.executable,
                    str(repo / "tools" / "xlabel_keygen.py"),
                    "-o",
                    str(dek),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
            ).returncode,
            0,
        )
        passphrase.write_text(self.passphrase + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(repo / "tools" / "xlabel_wrap.py"),
                "--master",
                str(master),
                "--dek",
                str(dek),
                "-o",
                str(credential),
                "--passphrase-file",
                str(passphrase),
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        material = load_key_file(str(dek))
        self.assertEqual(result.stdout.strip(), material.key_id.hex())
        self.assertNotIn(material.key.hex(), result.stdout + result.stderr)
        self.assertNotIn(self.passphrase, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
