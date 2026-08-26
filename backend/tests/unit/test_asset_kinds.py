"""The resource centre's kind classifier.

The filter, the icon and the preview mode all read `kind_of`, so the rules
that matter are the awkward ones: a generic MIME must not win over a
meaningful extension, and a meaningful MIME must win over a missing one.
"""
from api.asset_kinds import KINDS, kind_of


def test_specific_mime_wins():
    assert kind_of("image/png", "whatever.bin") == "image"
    assert kind_of("video/mp4", "clip") == "video"
    assert kind_of("audio/mpeg", "track") == "audio"
    assert kind_of("application/pdf", "report") == "document"


def test_generic_mime_falls_through_to_the_extension():
    # `file --brief --mime-type` hands back octet-stream for plenty of real
    # formats; trusting it would file every one of them under "other".
    assert kind_of("application/octet-stream", "clip.mp4") == "video"
    assert kind_of("application/octet-stream", "notes.md") == "document"
    assert kind_of("", "main.py") == "code"
    assert kind_of("binary/octet-stream", "bundle.zip") == "archive"


def test_extension_case_is_ignored():
    assert kind_of("", "PHOTO.JPG") == "image"


def test_unknown_stays_other():
    assert kind_of("", "blob") == "other"
    assert kind_of("application/x-made-up", "thing.qqq") == "other"


def test_mime_parameters_are_stripped():
    assert kind_of("text/markdown; charset=utf-8", "readme") == "document"


def test_every_answer_is_a_declared_kind():
    samples = [
        ("image/png", "a.png"),
        ("", "a.mp4"),
        ("application/zip", "a.zip"),
        ("text/plain", "a.txt"),
        ("", "a"),
    ]
    assert all(kind_of(mime, name) in KINDS for mime, name in samples)
