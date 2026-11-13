import base64
import hashlib
from html.parser import HTMLParser
import http.client
import json
import re
import struct

import pytest

from freya.control_plane import build_local_control_plane
from freya.events import EventBus
from freya.gateway import server as gateway_server
from freya.gateway.server import Gateway, STATIC_FILES, content_security_policy
from freya.image.gallery import ImageGallery
from freya.spatial_avatar import library as library_module
from freya.spatial_avatar.config import AvatarConfigStore
from freya.spatial_avatar.geometry import single_display_layout
from freya.spatial_avatar.host import AvatarHost
from freya.spatial_avatar.library import AvatarLibrary, LibraryError, use_check
from freya.spatial_avatar.service import (
    AVATAR_CAPABILITIES,
    AVATAR_LIBRARY,
    AvatarService,
)
from freya.spatial_avatar.vrm import (
    AUTHOR_ONLY,
    EVERYONE,
    LICENSED,
    REQUIRED_BONES,
    UNKNOWN,
    VrmError,
    read_thumbnail,
    read_vrm,
)
from freya.window import WindowActions


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60


def glb(document, binary=b"", magic=0x46546C67, version=2) -> bytes:
    """A glTF binary file holding this description and binary data."""
    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(body), 0x4E4F534A) + body

    if binary:
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary

    return struct.pack("<III", magic, version, 12 + len(chunks)) + chunks


def model_parts(thumbnail=PNG, triangles=1200):
    """The non-avatar parts of a small model: one mesh and one image."""
    return {
        "asset": {"version": "2.0", "generator": "test"},
        "accessors": [{"count": triangles * 3}, {"count": triangles * 2}],
        "meshes": [{"primitives": [{"indices": 0, "attributes": {"POSITION": 1}}]}],
        "materials": [{}],
        "nodes": [{}, {}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
        "textures": [{"source": 0}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(thumbnail)}],
    }


def vrm1(meta=None, expressions=None, bones=REQUIRED_BONES, thumbnail=PNG, **parts) -> bytes:
    document = model_parts(thumbnail, **parts)
    document["extensions"] = {
        "VRMC_vrm": {
            "specVersion": "1.0",
            "meta": {
                "name": "Test Avatar",
                "authors": ["An Author"],
                "version": "1.2",
                "avatarPermission": "everyone",
                "commercialUsage": "personalNonProfit",
                "allowExcessivelyViolentUsage": False,
                "allowExcessivelySexualUsage": False,
                "allowRedistribution": False,
                "creditNotation": "required",
                "modification": "prohibited",
                "licenseUrl": "https://vrm.dev/licenses/1.0/",
                "thumbnailImage": 0,
                **(meta or {}),
            },
            "humanoid": {"humanBones": {bone: {"node": 0} for bone in bones}},
            "expressions": {
                "preset": {
                    name: {}
                    for name in (
                        expressions
                        if expressions is not None
                        else ("aa", "ih", "ou", "ee", "oh", "happy", "blink")
                    )
                },
                "custom": {"wink": {}},
            },
        }
    }

    return glb(document, thumbnail)


def vrm0(meta=None, thumbnail=PNG) -> bytes:
    document = model_parts(thumbnail)
    document["extensions"] = {
        "VRM": {
            "specVersion": "0.0",
            "meta": {
                "title": "Old Avatar",
                "author": "Someone",
                "allowedUserName": "Everyone",
                "violentUssageName": "Disallow",
                "sexualUssageName": "Disallow",
                "commercialUssageName": "Allow",
                "licenseName": "CC_BY",
                "texture": 0,
                **(meta or {}),
            },
            "humanoid": {
                "humanBones": [{"bone": bone, "node": 0} for bone in REQUIRED_BONES]
            },
            "blendShapeMaster": {
                "blendShapeGroups": [
                    {"presetName": preset, "name": preset.upper()}
                    for preset in ("a", "i", "u", "e", "o", "joy", "blink")
                ]
                + [{"presetName": "unknown", "name": "Custom Smile"}]
            },
        }
    }

    return glb(document, thumbnail)


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "avatar_models"
    folder.mkdir()

    return folder


@pytest.fixture
def library(folder, tmp_path):
    return AvatarLibrary(folder, tmp_path / "avatar_library.json")


# -- reading a VRM file ------------------------------------------------


def test_vrm_1_description_is_read(folder):
    path = folder / "new.vrm"
    path.write_bytes(vrm1())

    info = read_vrm(path)

    assert info.spec == "1.0"
    assert info.name == "Test Avatar"
    assert info.authors == ["An Author"]
    assert info.version == "1.2"
    assert info.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info.size == path.stat().st_size
    assert info.visemes == ["aa", "ih", "ou", "ee", "oh"]
    assert {"happy", "blink", "wink"} <= set(info.expressions)
    assert info.missing_bones == []
    assert info.stats["triangles"] == 1200
    assert info.stats["vertices"] == 2400
    assert info.stats["meshes"] == 1
    assert info.warnings == []
    json.dumps(info.as_dict())


def test_vrm_1_terms_are_put_in_plain_words(folder):
    path = folder / "new.vrm"
    path.write_bytes(vrm1())

    licence = read_vrm(path).licence

    assert licence.who_may_use == EVERYONE
    assert licence.commercial_use.startswith("not allowed")
    assert licence.redistribution == "not allowed"
    assert licence.modification == "not allowed"
    assert licence.credit == "required"
    assert licence.violent_use == "not allowed"
    assert licence.licence_url == "https://vrm.dev/licenses/1.0/"
    summary = licence.summary()
    assert summary[0] == "Anyone may use this avatar."
    assert "Credit: required." in summary


def test_vrm_0_description_uses_the_same_names(folder):
    path = folder / "old.vrm"
    path.write_bytes(vrm0())

    info = read_vrm(path)

    assert info.spec == "0.x"
    assert info.name == "Old Avatar"
    assert info.authors == ["Someone"]
    # The old names for mouth shapes and moods are translated.
    assert info.visemes == ["aa", "ih", "ou", "ee", "oh"]
    assert "happy" in info.expressions and "Custom Smile" in info.expressions
    assert info.licence.who_may_use == EVERYONE
    assert info.licence.commercial_use == "allowed"
    assert info.licence.licence_name == "CC BY"
    assert info.licence.redistribution == "allowed under CC BY"
    assert info.thumbnail is not None


@pytest.mark.parametrize(
    "version, field, value, expected",
    [
        (1, "avatarPermission", "onlyAuthor", AUTHOR_ONLY),
        (1, "avatarPermission", "onlySeparatelyLicensedPerson", LICENSED),
        (1, "avatarPermission", "everyone", EVERYONE),
        (1, "avatarPermission", "whoever", UNKNOWN),
        (1, "avatarPermission", 7, UNKNOWN),
        (0, "allowedUserName", "OnlyAuthor", AUTHOR_ONLY),
        (0, "allowedUserName", "ExplicitlyLicensedPerson", LICENSED),
        (0, "allowedUserName", None, UNKNOWN),
    ],
)
def test_who_may_use_an_avatar(folder, version, field, value, expected):
    path = folder / "a.vrm"
    path.write_bytes((vrm1 if version else vrm0)(meta={field: value}))

    info = read_vrm(path)

    assert info.licence.who_may_use == expected
    assert use_check(info.licence.as_dict())["allowed"] is (expected == EVERYONE)

    if expected == UNKNOWN:
        assert any("does not state who may use" in w for w in info.warnings)


def test_shortcomings_are_warned_about(folder):
    path = folder / "partial.vrm"
    path.write_bytes(
        vrm1(expressions=("happy",), bones=("hips", "head"), triangles=300_000)
    )

    info = read_vrm(path)

    assert "leftHand" in info.missing_bones
    assert info.visemes == []
    assert len(info.warnings) == 3
    assert any("skeleton" in warning for warning in info.warnings)
    assert any("mouth shapes" in warning for warning in info.warnings)
    assert any("very detailed" in warning for warning in info.warnings)


def test_text_from_a_file_is_cleaned_and_cut(folder):
    path = folder / "odd.vrm"
    path.write_bytes(
        vrm1(meta={
            "name": "Nice\x00Name\n\t<script>alert(1)</script>" + "x" * 500,
            "authors": ["A" * 900, 5, None, "  Real   Author  "] + ["extra"] * 40,
            "licenseUrl": "u" * 5000,
        })
    )

    info = read_vrm(path)

    assert "\x00" not in info.name and "\n" not in info.name
    assert len(info.name) <= 200
    # Kept as text: it is shown as text, never as markup.
    assert "<script>" in info.name
    assert len(info.authors) <= 8
    assert "Real Author" in info.authors
    assert len(info.licence.licence_url) <= 500


@pytest.mark.parametrize(
    "content, problem",
    [
        (b"", "too short"),
        (b"hello, I am not a model", "not a VRM file"),
        (glb({}, magic=0x12345678), "not a VRM file"),
        (glb({}, version=1), "not supported"),
        (glb({"asset": {}}), "not a VRM avatar"),
        (glb({"extensions": {"VRM": "yes"}}), "not a VRM avatar"),
        (glb([1, 2, 3]), "could not be read"),
        (vrm1()[:40], "cut short"),
        (struct.pack("<III", 0x46546C67, 2, 10**9) + b"\x00" * 20, "cut short"),
        (
            struct.pack("<III", 0x46546C67, 2, 40)
            + struct.pack("<II", 2**31, 0x4E4F534A)
            + b"{}" * 10,
            "too large or damaged",
        ),
        (
            struct.pack("<III", 0x46546C67, 2, 28)
            + struct.pack("<II", 8, 0x004E4942)
            + b"\x00" * 8,
            "description is missing",
        ),
        (
            struct.pack("<III", 0x46546C67, 2, 28)
            + struct.pack("<II", 8, 0x4E4F534A)
            + b"{nope!!}",
            "could not be read",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_files_that_are_not_vrm_are_refused_with_a_reason(folder, content, problem):
    path = folder / "bad.vrm"
    path.write_bytes(content)

    with pytest.raises(VrmError, match=problem):
        read_vrm(path)


def test_deeply_nested_description_is_refused_not_crashed_on(folder):
    path = folder / "deep.vrm"
    nested = b"[" * 200_000 + b"]" * 200_000
    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, 20 + len(nested))
        + struct.pack("<II", len(nested), 0x4E4F534A)
        + nested
    )

    with pytest.raises(VrmError):
        read_vrm(path)


def test_oversized_file_is_refused_before_it_is_read(folder, monkeypatch):
    from freya.spatial_avatar import vrm

    path = folder / "big.vrm"
    path.write_bytes(vrm1())
    monkeypatch.setattr(vrm, "MAX_FILE_BYTES", 100)

    with pytest.raises(VrmError, match="larger than"):
        read_vrm(path)


def test_missing_file_is_an_error_not_a_crash(folder):
    with pytest.raises(VrmError):
        read_vrm(folder / "nothing.vrm")


# -- thumbnails --------------------------------------------------------


def test_thumbnail_is_read_from_inside_the_file(folder):
    path = folder / "a.vrm"
    path.write_bytes(vrm1())
    info = read_vrm(path)

    assert read_thumbnail(path, info.thumbnail) == ("image/png", PNG)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["bufferViews"][0].update(byteOffset=10**8),
        lambda d: d["bufferViews"][0].update(byteLength=10**9),
        lambda d: d["bufferViews"][0].update(byteLength=-4),
        lambda d: d["bufferViews"][0].update(buffer=1),
        lambda d: d["images"][0].update(bufferView=99),
        lambda d: d["images"][0].update(mimeType="image/svg+xml"),
        lambda d: d["images"][0].update(mimeType="text/html"),
        lambda d: d["images"][0].pop("bufferView"),
        lambda d: d["extensions"]["VRMC_vrm"]["meta"].update(thumbnailImage=42),
        lambda d: d["extensions"]["VRMC_vrm"]["meta"].update(thumbnailImage="0"),
        lambda d: d.update(images="none"),
    ],
)
def test_implausible_thumbnail_is_ignored(folder, change):
    raw = vrm1()
    length = struct.unpack("<I", raw[12:16])[0]
    document = json.loads(raw[20:20 + length])
    change(document)
    path = folder / "a.vrm"
    path.write_bytes(glb(document, PNG))

    info = read_vrm(path)

    assert info.thumbnail is None
    assert info.as_dict()["has_thumbnail"] is False


def test_thumbnail_that_is_not_the_image_it_claims_is_not_served(folder):
    script = b"<script>alert(1)</script>" + b" " * 40
    path = folder / "a.vrm"
    path.write_bytes(vrm1(thumbnail=script))
    info = read_vrm(path)

    # The file says it is a PNG; its bytes say otherwise.
    assert info.thumbnail is not None
    assert read_thumbnail(path, info.thumbnail) is None

    for location in (None, ("image/png",), ("text/html", 0, 10), ("image/png", "a", "b"),
                     ("image/png", 0, 10**9)):
        assert read_thumbnail(path, location) is None


def test_jpeg_thumbnail(folder):
    raw = vrm1(thumbnail=JPEG)
    length = struct.unpack("<I", raw[12:16])[0]
    document = json.loads(raw[20:20 + length])
    document["images"][0]["mimeType"] = "image/jpeg"
    path = folder / "a.vrm"
    path.write_bytes(glb(document, JPEG))

    assert read_thumbnail(path, read_vrm(path).thumbnail) == ("image/jpeg", JPEG)


# -- the library -------------------------------------------------------


def test_scan_finds_vrm_files_directly_in_the_folder_only(library, folder, tmp_path):
    (folder / "one.vrm").write_bytes(vrm1())
    (folder / "TWO.VRM").write_bytes(vrm0())
    (folder / "notes.txt").write_text("not a model")
    (folder / "model.glb").write_bytes(vrm1())
    (folder / "nested").mkdir()
    (folder / "nested" / "hidden.vrm").write_bytes(vrm1(meta={"name": "Nested"}))
    (folder / "broken.vrm").write_bytes(b"not a model")
    (tmp_path / "outside.vrm").write_bytes(vrm1(meta={"name": "Outside"}))

    library.scan()

    assert sorted(model["name"] for model in library.models()) == [
        "Old Avatar", "Test Avatar",
    ]
    assert library.errors() == [
        {"file": "broken.vrm", "error": library.errors()[0]["error"]}
    ]
    assert "VRM file" in library.errors()[0]["error"]


def test_scan_never_changes_the_model_files(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    (folder / "broken.vrm").write_bytes(b"not a model")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}

    library.scan()
    library.scan()

    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()} == before


def test_linked_file_from_outside_the_folder_is_ignored(library, folder, tmp_path):
    outside = tmp_path / "secret.vrm"
    outside.write_bytes(vrm1(meta={"name": "Outside"}))

    try:
        (folder / "link.vrm").symlink_to(outside)
    except OSError:
        pytest.skip("this account cannot create links")

    library.scan()

    assert library.models() == []


def test_unchanged_files_are_not_read_again(library, folder, monkeypatch):
    (folder / "one.vrm").write_bytes(vrm1())
    (folder / "two.vrm").write_bytes(vrm0())
    library.scan()
    read = []
    real = library_module.read_vrm
    monkeypatch.setattr(
        library_module, "read_vrm", lambda path: read.append(path.name) or real(path)
    )

    library.scan()
    assert read == []

    (folder / "two.vrm").write_bytes(vrm0(meta={"title": "Changed"}))
    library.scan()

    assert read == ["two.vrm"]
    assert "Changed" in [model["name"] for model in library.models()]


def test_identical_files_are_one_model_and_a_rename_keeps_it(library, folder):
    (folder / "a.vrm").write_bytes(vrm1())
    (folder / "copy.vrm").write_bytes(vrm1())
    library.scan()
    assert len(library.models()) == 1
    sha = library.models()[0]["sha256"]
    saved = library.add_profile("Mine", sha)

    (folder / "copy.vrm").unlink()
    (folder / "a.vrm").rename(folder / "renamed.vrm")
    library.scan()

    assert library.models()[0]["sha256"] == sha
    assert library.models()[0]["file_name"] == "renamed.vrm"
    assert library.profiles()[0]["model_present"] is True
    assert library.profiles()[0]["id"] == saved["id"]


def test_only_models_found_by_scanning_can_be_fetched(library, folder, tmp_path):
    (folder / "one.vrm").write_bytes(vrm1())
    (tmp_path / "secret.vrm").write_bytes(vrm1(meta={"name": "Outside"}))
    library.scan()
    sha = library.models()[0]["sha256"]
    outside = hashlib.sha256((tmp_path / "secret.vrm").read_bytes()).hexdigest()

    assert library.model_bytes(sha) == (folder / "one.vrm").read_bytes()
    assert library.thumbnail(sha) == ("image/png", PNG)

    for asked in (outside, "../secret.vrm", "one.vrm", "", None, 5, "a" * 64, sha.upper()):
        assert library.model_bytes(asked) is None
        assert library.thumbnail(asked) is None

    # A file removed after scanning is no longer served.
    (folder / "one.vrm").unlink()
    assert library.model_bytes(sha) is None


def test_model_too_large_to_preview_is_listed_but_not_sent(library, folder, monkeypatch):
    (folder / "one.vrm").write_bytes(vrm1())
    monkeypatch.setattr(library_module, "MAX_PREVIEW_BYTES", 10)
    library.scan()

    assert library.models()[0]["can_preview"] is False
    assert library.model_bytes(library.models()[0]["sha256"]) is None


def test_model_listing_does_not_reveal_where_things_are_in_the_file(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()

    assert "thumbnail" not in library.models()[0]
    assert library.models()[0]["has_thumbnail"] is True


# -- saved avatars -----------------------------------------------------


def test_avatar_from_a_model_anyone_may_use_needs_no_confirmation(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()
    sha = library.models()[0]["sha256"]

    saved = library.add_profile("  My   Freya ", sha)

    assert saved["name"] == "My Freya"
    assert saved["model_name"] == "Test Avatar"
    assert saved["licence_confirmed"] is None


@pytest.mark.parametrize("who", ["onlyAuthor", "onlySeparatelyLicensedPerson", "nobody-said"])
def test_restricted_model_needs_the_owners_statement(library, folder, who):
    (folder / "one.vrm").write_bytes(vrm1(meta={"avatarPermission": who}))
    library.scan()
    model = library.models()[0]
    assert model["use"]["allowed"] is False
    assert "I confirm" in model["use"]["confirmation"]

    for confirmed in (False, None, "yes", 1):
        with pytest.raises(LibraryError, match="restrict who may use"):
            library.add_profile("Mine", model["sha256"], confirmed=confirmed)

    assert library.profiles() == []

    saved = library.add_profile("Mine", model["sha256"], confirmed=True)

    # What was confirmed, and when, is kept with the avatar.
    assert saved["licence_confirmed"]["statement"] == model["use"]["confirmation"]
    assert saved["licence_confirmed"]["time"] > 0


def test_avatar_names_and_models_are_checked(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()
    sha = library.models()[0]["sha256"]
    library.add_profile("Mine", sha)

    for name, model in (("", sha), ("   ", sha), ("x" * 41, sha), ("mine", sha),
                        ("Other", "a" * 64), ("Other", None), ("Other", ["x"])):
        with pytest.raises(LibraryError):
            library.add_profile(name, model)

    assert len(library.profiles()) == 1


def test_avatars_and_the_chosen_one_survive_a_restart(library, folder, tmp_path):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()
    saved = library.add_profile("Mine", library.models()[0]["sha256"])
    library.use_profile(saved["id"])

    again = AvatarLibrary(folder, tmp_path / "avatar_library.json")
    again.scan()

    assert again.active_id == saved["id"]
    assert again.profiles()[0]["name"] == "Mine"
    assert again.profiles()[0]["model_present"] is True


def test_avatar_whose_model_file_has_gone_cannot_be_chosen(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()
    saved = library.add_profile("Mine", library.models()[0]["sha256"])

    (folder / "one.vrm").unlink()
    library.scan()

    assert library.profiles()[0]["model_present"] is False

    with pytest.raises(LibraryError, match="no longer in the avatar folder"):
        library.use_profile(saved["id"])

    library.use_profile(None)
    library.delete_profile(saved["id"])

    with pytest.raises(LibraryError):
        library.delete_profile(saved["id"])

    with pytest.raises(LibraryError):
        library.use_profile("nothing")


def test_deleting_the_chosen_avatar_clears_the_choice(library, folder):
    (folder / "one.vrm").write_bytes(vrm1())
    library.scan()
    saved = library.add_profile("Mine", library.models()[0]["sha256"])
    library.use_profile(saved["id"])

    library.delete_profile(saved["id"])

    assert library.active_id is None
    # Deleting an avatar never deletes the model file.
    assert (folder / "one.vrm").is_file()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"profiles": "none", "cache": 5}',
     '{"profiles": [{"id": "../x", "name": "a", "model": "b"}, 7], "active": "zz"}'],
)
def test_damaged_library_file_is_ignored(folder, tmp_path, content):
    path = tmp_path / "avatar_library.json"
    path.write_text(content, encoding="utf-8")

    library = AvatarLibrary(folder, path)
    library.scan()

    assert library.profiles() == []
    assert library.active_id is None


# -- through the broker, and from the window ---------------------------


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def service(plane, library, tmp_path):
    service = AvatarService(
        plane.broker,
        "local-owner",
        store=AvatarConfigStore(tmp_path / "avatar.json"),
        host=AvatarHost("null", log_path=tmp_path / "overlay.log"),
        probe=single_display_layout,
        library=library,
    )
    plane.register_avatar(service)
    yield service
    service.stop()


def test_library_is_a_separate_audited_permission(plane, service, folder):
    (folder / "one.vrm").write_bytes(vrm1())

    assert AVATAR_LIBRARY in AVATAR_CAPABILITIES
    assert plane.broker.exists(AVATAR_LIBRARY)
    # Describing the tab reads no files; only an explicit scan does.
    assert service.describe()["library"]["models"] == []

    answer = service.handle({"action": "library-scan"})
    assert answer["error"] is None
    assert [m["name"] for m in answer["state"]["library"]["models"]] == ["Test Avatar"]

    rows = plane.store.execute(
        "SELECT kind, outcome FROM audit_event WHERE capability = ?", (AVATAR_LIBRARY,)
    ).fetchall()
    assert ("outcome", "succeeded") in [tuple(row) for row in rows]

    # Withdrawing it stops the library and nothing else.
    rules = {rule["capability"]: rule["rule_id"] for rule in plane.policy.list_rules()}
    plane.policy.remove_rule(rules[AVATAR_LIBRARY])

    assert "not authorized" in service.handle({"action": "library-scan"})["error"].lower()
    assert service.handle({"action": "show"})["error"] is None


def test_library_has_its_own_policy_seed(plane, service):
    seeds = {row[0] for row in plane.store.execute("SELECT name FROM policy_seed").fetchall()}

    assert {"alpha-avatar", "alpha-avatar-library"} <= seeds


def test_saving_choosing_and_deleting_an_avatar_from_the_window(service, folder):
    (folder / "free.vrm").write_bytes(vrm1())
    (folder / "mine.vrm").write_bytes(vrm1(meta={"name": "Private", "avatarPermission": "onlyAuthor"}))
    models = {m["name"]: m for m in service.handle({"action": "library-scan"})["state"]["library"]["models"]}

    refused = service.handle({"action": "library-save", "value": {
        "name": "Hers", "model": models["Private"]["sha256"],
    }})
    assert "restrict who may use" in refused["error"]

    confirmed = service.handle({"action": "library-save", "value": {
        "name": "Hers", "model": models["Private"]["sha256"], "confirmed": True,
    }})
    assert confirmed["error"] is None
    saved = confirmed["state"]["library"]["profiles"][0]
    assert saved["licence_confirmed"]["statement"].startswith("Only its author")

    used = service.handle({"action": "library-use", "value": {"avatar": saved["id"]}})
    assert used["state"]["library"]["active_profile"] == saved["id"]

    cleared = service.handle({"action": "library-use", "value": {"avatar": None}})
    assert cleared["state"]["library"]["active_profile"] is None

    deleted = service.handle({"action": "library-delete", "value": {"avatar": saved["id"]}})
    assert deleted["state"]["library"]["profiles"] == []


@pytest.mark.parametrize(
    "message",
    [
        {"action": "library-format-disk"},
        {"action": "library-save"},
        {"action": "library-save", "value": "everything"},
        {"action": "library-save", "value": {"name": "X", "model": "../../secrets"}},
        {"action": "library-use", "value": {"avatar": ["a"]}},
        {"action": "library-delete", "value": {"avatar": "nothing"}},
    ],
)
def test_bad_library_requests_are_refused_with_a_message(service, message):
    answer = service.handle(message)

    assert answer["error"]
    assert answer["state"]["library"]["profiles"] == []


# -- the gateway and the page ------------------------------------------


@pytest.fixture
def gateway(service, tmp_path):
    actions = WindowActions(
        say=lambda text: None,
        generate_image=lambda prompt: None,
        set_muted=lambda muted: None,
        interrupt=lambda: None,
        status=lambda: {},
        avatar_state=service.describe,
        avatar_request=service.handle,
        avatar_file=service.file,
    )
    gateway = Gateway(EventBus(), ImageGallery(tmp_path / "images"), actions, port=0)
    gateway.start()
    yield gateway
    gateway.stop()


def get(gateway, path, cookie=None):
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=10)
    connection.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    response = connection.getresponse()
    body = response.read()
    connection.close()

    return response, body


def sign_in(gateway):
    path = gateway.new_window_url().split(f":{gateway.port}", 1)[1]
    response, _ = get(gateway, path)

    return response.getheader("Set-Cookie").split(";", 1)[0]


def test_models_and_thumbnails_are_served_only_to_the_signed_in_window(
    gateway, service, folder
):
    (folder / "one.vrm").write_bytes(vrm1())
    sha = service.handle({"action": "library-scan"})["state"]["library"]["models"][0]["sha256"]
    cookie = sign_in(gateway)

    model, body = get(gateway, f"/avatar-models/{sha}", cookie)
    picture, image = get(gateway, f"/avatar-thumbnails/{sha}", cookie)
    stranger, _ = get(gateway, f"/avatar-models/{sha}")

    assert model.status == 200
    assert model.getheader("Content-Type") == "model/gltf-binary"
    assert model.getheader("X-Content-Type-Options") == "nosniff"
    assert body == (folder / "one.vrm").read_bytes()
    assert (picture.status, picture.getheader("Content-Type"), image) == (200, "image/png", PNG)
    assert stranger.status != 200

    for path in (
        "/avatar-models/", "/avatar-models/" + "a" * 64, f"/avatar-models/{sha}.vrm",
        "/avatar-models/one.vrm", "/avatar-models/..%2F..%2Fpyproject.toml",
        f"/avatar-thumbnails/{sha}/../x", "/avatar-thumbnails/",
    ):
        response, _ = get(gateway, path, cookie)
        assert response.status == 404, path


def test_only_listed_library_files_are_served(gateway):
    cookie = sign_in(gateway)

    for path, (name, content_type) in STATIC_FILES.items():
        response, body = get(gateway, path, cookie)
        assert response.status == 200, path
        assert response.getheader("Content-Type") == content_type
        assert len(body) > 0

    for path in (
        "/vendor/", "/vendor/README.txt", "/vendor/three/LICENSE",
        "/vendor/three/../../app.js", "/vendor/three/addons/loaders/Missing.js",
        "/vendor/three/three.module.js.map",
    ):
        response, _ = get(gateway, path, cookie)
        assert response.status == 404, path

    stranger, _ = get(gateway, "/vendor/three/three.module.js")
    assert stranger.status != 200


def test_policy_allows_the_import_map_by_hash_and_nothing_looser(gateway):
    cookie = sign_in(gateway)
    page, body = get(gateway, "/", cookie)
    policy = page.getheader("Content-Security-Policy")
    class InlineScripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []
            self.in_inline_script = False

        def handle_starttag(self, tag, attrs):
            if tag == "script" and not any(name == "src" for name, _ in attrs):
                self.in_inline_script = True
                self.scripts.append("")

        def handle_data(self, data):
            if self.in_inline_script:
                self.scripts[-1] += data

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_inline_script = False

    parser = InlineScripts()
    parser.feed(body.decode("utf-8"))
    inline = parser.scripts

    # The page has exactly one inline script: the import map.
    assert len(inline) == 1
    assert json.loads(inline[0])["imports"]["three"] == "/vendor/three/three.module.js"

    digest = base64.b64encode(hashlib.sha256(inline[0].encode("utf-8")).digest()).decode()
    directives = dict(part.strip().split(" ", 1) for part in policy.split(";") if " " in part.strip())

    assert directives["script-src"] == f"'self' 'sha256-{digest}'"
    assert directives["default-src"] == "'none'"
    assert directives["img-src"] == "'self' blob:"
    assert directives["connect-src"] == "'self' blob:"
    assert directives["style-src"] == "'self'"
    assert directives["media-src"] == "'self'"
    # The page may frame its own sandboxed media document (answer_media,
    # which carries its own policy and the sandbox directive) and the
    # one video host, and nothing else.
    assert directives["frame-src"] == "'self' https://www.youtube-nocookie.com"
    assert policy == content_security_policy()

    for loose in ("unsafe-inline", "unsafe-eval", "data:", "http:", "https:", "*"):
        assert loose not in policy.replace("https://www.youtube-nocookie.com", ""), loose

    # Every place the import map points to is a file the gateway serves.
    for target in json.loads(inline[0])["imports"].values():
        assert any(path.startswith(target) for path in STATIC_FILES), target


def test_bundled_libraries_are_the_published_files(gateway):
    """
    The third-party files match the checksums recorded beside them,
    and carry their licences.
    """
    from importlib import resources

    vendor = resources.files("freya.gateway") / "static" / "vendor"
    recorded = dict(
        reversed(line.split()) for line in (vendor / "README.txt").read_text().splitlines()
        if re.match(r"^\s+[0-9a-f]{64}\s", line)
    )

    assert len(recorded) == 9

    for name, digest in recorded.items():
        data = vendor.joinpath(*name.split("/")).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name

    assert "MIT" in (vendor / "three" / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in (vendor / "three-vrm" / "LICENSE").read_text(encoding="utf-8")


def test_page_shows_model_details_as_text_only():
    from importlib import resources

    static = resources.files("freya.gateway") / "static"
    script = (static / "app.js").read_text(encoding="utf-8")
    viewer = (static / "avatar-view.js").read_text(encoding="utf-8")
    page = (static / "index.html").read_text(encoding="utf-8")

    for name in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert name not in script, name
        assert name not in viewer, name

    for control in ('id="library-scan"', 'id="avatar-canvas"', 'id="preview-terms"',
                    'id="preview-confirm"', 'id="library-avatars"'):
        assert control in page, control

    # The page says plainly that the desktop avatar is still the placeholder.
    assert "still the placeholder" in page
