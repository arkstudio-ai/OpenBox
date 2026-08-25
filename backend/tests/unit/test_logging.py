"""OpenBox logger hierarchy must not duplicate child records."""

import io
import logging

from core.log import create_logger


def test_nested_openbox_loggers_emit_each_record_once():
    parent = create_logger("dedupe-proof")
    child = create_logger("dedupe-proof.child")
    parent_stream = io.StringIO()
    child_stream = io.StringIO()
    parent.handlers = [logging.StreamHandler(parent_stream)]
    child.handlers = [logging.StreamHandler(child_stream)]

    child.info("one computer timing")

    assert child_stream.getvalue().count("one computer timing") == 1
    assert parent_stream.getvalue() == ""
    assert child.propagate is False


def test_create_logger_is_idempotent():
    logger = create_logger("dedupe-idempotent")
    again = create_logger("dedupe-idempotent")

    assert again is logger
    assert len(logger.handlers) == 1
    assert logger.propagate is False
